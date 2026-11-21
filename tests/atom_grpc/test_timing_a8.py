# SPDX-License-Identifier: MIT
"""The a8 case's measures: each path's topology, aiperf's records, a lean engine record,
extra TTFT and ITL per request, and the criteria with their rerun rule."""

import json
import time
from collections import Counter

import grpc
import pytest

from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from tests.atom_grpc.fake.instance import FakeInstance, free_port
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.fake.serve import COUNTS_ENV, LEAN_ENV
from tests.atom_grpc.harness.launch import load
from tests.atom_grpc.test_grpc_generate import WAIT_S, generate_request, prompt_ids
from tests.atom_grpc.test_grpc_session import GREEDY, SessionCall, submit
from tests.atom_grpc.timing import a8

STARTUP_S = 1500


def test_each_path_runs_p_three_times_and_d_four_under_its_own_router(tmp_path):
    (p, d), (router,) = load(a8.TOPOLOGY)
    prefill, decode = [f"p{k}" for k in range(3)], [f"d{k}" for k in range(4)]
    model = tmp_path / "model"
    for path in a8.PATHS:
        file, ports = a8.path_topology(path, tmp_path / path, model, "law")
        specs, (mine,) = load(file)
        http, session = path == "http", path == "session"
        assert (mine.name, mine.grpc, mine.prefill, mine.decode) == (
            path, not http, prefill, decode,
        )  # fmt: skip
        flags = [*router.args, "--model-path", str(model)]
        assert mine.args == flags + session * ["--atom-grpc-session"]
        assert [spec.name for spec in specs] == prefill + decode
        assert ports == {"p0": 17300, "p1": 17310, "p2": 17320}
        for spec in specs:
            template = p if spec.name in prefill else d
            counts = str(tmp_path / path / spec.name / "grpc_counts.jsonl")
            env = template.fake["env"] | {LEAN_ENV: "1", COUNTS_ENV: counts}
            assert spec.fake == template.fake | {"law": "law", "env": env}
            assert (spec.dp, spec.cores) == (template.dp, template.cores)
            assert spec.grpc != http
            args = [a for a in spec.args if "handshake_port" not in a]
            want = [a for a in template.args if "handshake_port" not in a]
            assert args == want + http * ["--api-server-count", "4"]
    plan = a8.plan_cores(list(range(37, 128)), specs)
    assert [len(plan[k]) for k in ("instances", "atomesh", "aiperf")] == [57, 16, 16]
    assert plan["driver"] == [126, 127]
    with pytest.raises(ValueError, match="none to the driver"):
        a8.plan_cores(list(range(89)), specs)


def test_only_the_grpc_paths_routers_take_the_forwarded_flags(tmp_path, monkeypatch):
    def router_args(path: str) -> list[str]:
        file, _ = a8.path_topology(path, tmp_path / path, tmp_path / "model", "law")
        return load(file)[1][0].args

    monkeypatch.delenv(a8.GRPC_ROUTER_ARGS_ENV, raising=False)
    unset = {path: router_args(path) for path in a8.PATHS}
    monkeypatch.setenv(a8.GRPC_ROUTER_ARGS_ENV, "")
    assert {path: router_args(path) for path in a8.PATHS} == unset
    flags = "--fastokens-models '*' --fastokens-segment-cache-mb 512"
    monkeypatch.setenv(a8.GRPC_ROUTER_ARGS_ENV, flags)
    extra = ["--fastokens-models", "*", "--fastokens-segment-cache-mb", "512"]
    assert router_args("http") == unset["http"]
    for path in a8.GRPC_PATHS:
        assert router_args(path) == unset[path] + extra


def record(meta: dict, metrics: dict, error=None) -> str:
    units = {"time_to_first_token": "ms", "inter_token_latency": "ms"}
    shown = {k: {"value": v, "unit": units.get(k, "tokens")} for k, v in metrics.items()}  # fmt: skip
    meta = {"benchmark_phase": "profiling", "was_cancelled": False} | meta
    return json.dumps({"metadata": meta, "metrics": shown, "error": error}) + "\n"


def test_aiperf_records_give_profiled_requests_and_their_outcomes(tmp_path):
    times = {"request_start_ns": 10**9, "request_end_ns": 3 * 10**9}
    lengths = {"input_sequence_length": 640, "output_sequence_length": 20}
    timed = {"time_to_first_token": 250.0, "inter_token_latency": 7.5}
    export = tmp_path / "profile_export.jsonl"
    export.write_text(
        record(times | {"benchmark_phase": "warmup"}, timed | lengths)
        + record(times, timed | lengths)
        + record(times | {"was_cancelled": True}, {})
        + record(times | {"context_overflow_skip": True}, {})
        + record(times, {}, {"code": 503, "type": "prepare_queue_full", "message": "m"})
    )
    found = a8.clients(export)
    assert found[0] == pytest.approx(a8.Client(1.0, 3.0, 0.25, 0.0075, 640, 20, "ok"))
    outcomes = [client.outcome for client in found]
    assert outcomes == ["ok", "cancelled", "refused", "503 prepare_queue_full: m"]
    assert [c.outcome for c in a8.clients(export, "warmup")] == ["ok"]
    assert a8.clients(tmp_path / "none.jsonl") == []  # aiperf wrote none


def test_extra_times_take_p_and_d_time_off_and_leave_out_unsure_matches():
    client = a8.Client(100.0, 110.0, 3.0, 0.010, 640, 21, "ok")
    kv = {"remote_handshake_port": 17310, "transfer_id": 5}
    p = a8.Seq(100.1, 640, 1, {}, 101.1, 101.1, None)  # 1 s in P
    d = a8.Seq(101.5, 640, 21, kv, 102.0, 102.1, True)  # 0.5 s to D's first step
    prefills = {(17310, 5): p}
    ttfts, itls, left = a8.extras([client], prefills, [d])
    assert ttfts == [pytest.approx(1.5)] and left == 0  # 3 s less 1 s and 0.5 s
    assert itls == [pytest.approx(0.010 - 0.1 / 20)]
    late = d._replace(admit=103.5)  # after the client's first token: no request's
    assert a8.extras([client], prefills, [late])[2] == 1
    assert a8.extras([client], prefills, [d, d._replace(kv={})])[2] == 1  # two Ds
    assert a8.extras([client], {}, [d])[2] == 1  # no P


def test_a_failure_is_71s_when_its_window_holds_a_whole_block_receive_d_lost():
    failed = [a8.Client(at, at + 10, *[None] * 4, "x") for at in (10, 30)]
    lost = [15.0, 16.0]  # both within the first request's: only one is its
    assert a8.relay_failures(failed, lost) == 1 and lost == [16.0]
    assert a8.relay_failures(failed, [35.0, 12.0]) == 2


def test_slices_percentiles_and_windowed_samples():
    done = [a8.Client(0, end, 1, 0.01, 10, 300, "ok") for end in (1, 299, 300, 1799)]
    assert a8.slices(done, 0) == [2.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    assert a8.p50_p90(list(range(1, 12))) == (6, 10)
    ticks = [(0, {7: 0}), (5, {7: 100}), (10, {7: 300}), (15, {7: 900})]
    assert a8.cpu_shares(ticks, 4, 11, 100) == {7: 0.4}  # 200 ticks over 5 s
    count = f'{a8.STAGES}_count{{stage="encode"}}'
    total = count.replace("_count", "_sum")
    metrics = [(0, {count: 1, total: 0.5}), (5, {count: 5, total: 1.3})]
    assert a8.stage_means(metrics, 0, 5) == {"encode": (4, pytest.approx(0.2))}
    with pytest.raises(ValueError, match="1 samples"):
        a8.cpu_shares(ticks, 4, 6, 100)


def run(path: str, ttft: tuple, **fields) -> a8.Run:
    base = {
        "atomesh": 4.0, "stages": {}, "queue_errors": 0.0, "front_ends": {"p0 0": 0.5},
        "messages": {}, "itl": (0.0, 0.0), "completed": 10, "left_out": 0,
        "slices": [100.0] * 6, "outcomes": Counter(ok=10), "relay_failed": 0, "prompt": 1000.0,
    }  # fmt: skip
    return a8.Run(path, ttft=ttft, **(base | fields))


def test_the_criteria_hold_on_both_grpc_paths_and_a_close_ttft_runs_both_again():
    runs = {"http": [run("http", (0.5, 0.9))], "grpc": [run("grpc", (0.2, 0.4))]}
    outcomes = Counter({"ok": 8, "refused": 1, "None InvalidInferenceResultError": 1})
    runs["session"] = [run("session", (0.49, 0.6), outcomes=outcomes, relay_failed=1)]
    assert all(ok for _, ok in a8.verdicts(runs))
    assert not a8.close(runs["grpc"][0], runs["http"][0])
    assert a8.close(runs["session"][0], runs["http"][0])  # p50 within 5%
    runs["http"].append(run("http", (0.4, 0.9)))
    runs["session"].append(run("session", (0.49, 0.6)))
    failed = {text for text, ok in a8.verdicts(runs) if not ok}
    assert failed == {
        "session: extra TTFT p50 490.0 ms below HTTP's 450.0 ms (means of 2 and 2 runs)"
    }
    worse = run("grpc", (0.2, 0.4), atomesh=13.0, slices=[100.0] * 5 + [94.0])
    worse = worse._replace(outcomes=Counter(ok=9, cancelled=1), prompt=1021.0)
    runs = {"http": runs["http"][:1], "grpc": [worse], "session": runs["session"][:1]}
    failed = {text.split(": ")[1] for text, ok in a8.verdicts(runs) if not ok}
    assert failed == {
        "atomesh's CPU and its prepare queue",
        "the last slice's throughput",
        "no failure or cancel but ATOM's refusals and #71's",
        "mean prompt length",
    }
    aborted = {"http": [None], "grpc": [None], "session": runs["session"]}  # no window
    failed = {text.split(": ")[1] for text, ok in a8.verdicts(aborted) if not ok}
    assert failed >= {"aiperf's profiling window", "mean prompt length"}
    assert not a8.close(runs["session"][0], None)


@pytest.fixture(scope="module")
def counted(tmp_path_factory):
    """A fake instance with gRPC, its record lean and its gRPC messages counted."""
    workdir = tmp_path_factory.mktemp("counted")
    counts, port = workdir / "grpc_counts.jsonl", free_port()
    fake = {"step_s": 0.001, "env": {LEAN_ENV: "1", COUNTS_ENV: str(counts)}}
    args = ["--method", "mtp", "--num-speculative-tokens", "3"]
    args += ["--grpc-port", str(port)]
    instance = FakeInstance(workdir, a8.FIXTURE, args, fake)
    try:
        instance.start()
        instance.wait_ready(time.monotonic() + STARTUP_S)
        yield instance, f"127.0.0.1:{port}", counts
    finally:
        instance.stop()


def test_grpc_messages_are_counted_as_sent_and_a_lean_record_has_no_prompt(counted):
    instance, target, counts = counted
    streamed, sessioned = prompt_ids(70), prompt_ids(71)
    with grpc.insecure_channel(target) as channel:
        request = generate_request(streamed, stream=True, max_tokens=12)
        stub = pb_grpc.AtomEngineStub(channel)
        replies = list(stub.Generate(request, timeout=WAIT_S))
    with SessionCall(target) as session:
        session.send(submit({3: (sessioned, {"max_tokens": 9} | GREEDY)}))
        session.until_ended([3])
        session.end()
        read = 2 + len(session.batches) + len(list(session.call))  # Ready, Completions
    want = {"generate_in": 1, "generate_out": len(replies), "session_in": 1}
    want["session_out"] = read

    def totals() -> dict:
        lines = counts.read_text().splitlines()  # whole lines: one write each
        return json.loads(lines[-1]) if lines else {}

    deadline = time.monotonic() + WAIT_S
    while any(totals().get(name) != n for name, n in want.items()):
        assert time.monotonic() < deadline, (totals(), want)
        time.sleep(1)
    record = EngineRecord(instance.workdir / RECORD)
    seqs = a8.engine_seqs(instance.workdir / RECORD)
    assert len(record.requests) == 2
    for request in record.requests.values():
        assert request.prompt_ids is None and request.prompt_hash_chain is None
        assert request.num_prompt_tokens == len(streamed)
        seq, steps = seqs[request.seq_id], request.steps
        assert seq.first == steps[0].time
        reached = [sum(len(s.ids) for s in steps[: k + 1]) for k in range(len(steps))]
        less_one = request.sampling["max_tokens"] - 1  # a D's first id is its P's
        assert seq.last == next(s.time for s, n in zip(steps, reached) if n >= less_one)
