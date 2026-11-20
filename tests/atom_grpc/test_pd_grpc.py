# SPDX-License-Identifier: MIT
"""ATOM's P/D flow through atomesh's gRPC mode on a fake 1P1D, each case against its
HTTP P/D mode over the same P and D: what P and D are given, how each request ends."""

import concurrent.futures
import http.client
import json
import re
import socket
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import grpc
import psutil
import pytest
import tomllib
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.model_engine.arg_utils import EngineArgs
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from tests.atom_grpc.fake.ids import IdLaw, fingerprint
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.topologies import TOPOLOGIES, group_members

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
TOPOLOGY = {"relay": "1p1d_relay", "chunked": "1p1d"}  # P's transfer mode decides which
MODES = [pytest.param(m, marks=pytest.mark.topology(t)) for m, t in TOPOLOGY.items()]
PATHS = ("http", "grpc")  # each topology's two routers; HTTP P/D is the reference
SYLLABLES = [c + v for c in "bcdfghjklmnprstvwxyz" for v in "aeiou"]  # fixture words
FAULTS = {"fail": "zo zo zo", "timeout": "zu zu zu"}  # what D's fault rules match first
PREFILL = {"do_remote_decode": True, "do_remote_prefill": False}  # a relay P's params
BLOCK = EngineArgs.block_size  # no instance sets its own
LONG = 1000 * BLOCK + 1  # words that P's priced prefill takes seconds over
MAX_TOKENS = 50000  # far past where a cancelled request stops
SLACK_S = 1.0  # a gRPC request ends no later than the same one over HTTP plus this
WAIT_S = 120


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def fakes(mode: str) -> tuple[dict, dict]:
    """P's and D's fake configs in `mode`'s topology."""
    path = TOPOLOGIES / f"{TOPOLOGY[mode]}.toml"
    p, d = tomllib.loads(path.read_text())["instance"]
    return p["fake"], d["fake"]


def words(count: int, lead: int, prefix: str = "") -> str:
    """`count` fixture words from syllable `lead` on, after `prefix`; prompts with other
    leads share no first block."""
    text = " ".join(SYLLABLES[(lead + i) % len(SYLLABLES)] for i in range(count))
    return f"{prefix} {text}" if prefix else text


def completion(model: str, prompt: str, max_tokens: int, stream=False) -> dict:
    body = {"model": model, "prompt": prompt, "max_tokens": max_tokens}
    return body | {"temperature": 0, "stream": stream}


def model_info(instance) -> pb.GetModelInfoResponse:
    with grpc.insecure_channel(instance.grpc) as channel:
        return pb_grpc.AtomEngineStub(channel).GetModelInfo(pb.GetModelInfoRequest())


def kv_transfer_info(instance) -> dict:
    """`/kv_transfer_info` as GetServerInfo carries it."""
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        info = stub.GetServerInfo(pb.GetServerInfoRequest())
    return json.loads(info.kv_transfer_info_json)


def generate_request(prompt, kv: dict, stream: bool, max_tokens: int):
    return pb.GenerateRequest(
        request_id=f"gen-{uuid.uuid4().hex}",
        prompt_token_ids=prompt,
        sampling=v1.SamplingParams(max_tokens=max_tokens, temperature=0),
        kv_transfer_params_json=json.dumps(kv),
        stream=stream,
    )


@dataclass
class Reply:
    """A request through a router: its status, its replies (a stream's data events in
    order, else its body) and when its first line and its end reached the client."""

    status: int
    replies: list
    first: float
    end: float


def start(router, body: dict) -> http.client.HTTPConnection:
    """`body` sent to the router's completions route, nothing read yet."""
    url = urllib.parse.urlsplit(router.url)
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=600)
    headers = {"Content-Type": "application/json"}
    connection.request("POST", "/v1/completions", json.dumps(body), headers)
    return connection


def close(connection: http.client.HTTPConnection) -> None:
    """The client goes away."""
    connection.sock.shutdown(socket.SHUT_RDWR)
    connection.close()


def send(router, body: dict) -> Reply:
    connection = start(router, body)
    response, lines = connection.getresponse(), []
    while line := response.readline():
        lines.append((time.time(), line.decode()))
    end = time.time()
    connection.close()
    if response.status != 200 or not body["stream"]:
        text = "".join(line for _, line in lines)
        return Reply(response.status, [json.loads(text)], lines[0][0], end)
    events = [(t, line[6:]) for t, line in lines if line.startswith("data: ")]
    events = [(t, json.loads(e)) for t, e in events if e.strip() != "[DONE]"]
    return Reply(200, [e for _, e in events], events[0][0], end)


def answer(reply: Reply) -> tuple[list[str], str | None]:
    """The words of its text pieces in order, each piece split alone (#84), and its
    finish_reason."""
    found, finish = [], None
    for event in reply.replies:
        for choice in event["choices"]:
            found += choice["text"].split()
            finish = choice["finish_reason"] or finish
    return found, finish


def admitted(instance, prompt: list[int]) -> list:
    """The instance's admissions of `prompt` in its engine record, in order."""
    return EngineRecord(instance.workdir / RECORD).with_prompt(prompt)


def metrics(instance) -> dict[str, float]:
    """The instance's unlabelled `atom:` gauges and counters from /metrics."""
    with urllib.request.urlopen(instance.url + "/metrics", timeout=60) as reply:
        lines = reply.read().decode().splitlines()
    found = (re.fullmatch(r"(atom:\w+) (\S+)", line) for line in lines)
    return {match[1]: float(match[2]) for match in found if match}


def loads(router) -> dict[str, int]:
    """atomesh's in-flight count of each of its workers."""
    status, listing = router.get("/workers")
    assert status == 200, listing
    return {worker["url"]: worker["load"] for worker in listing["workers"]}


def retries(router) -> float:
    """atomesh's retries so far, from its Prometheus listener."""
    argv = psutil.Process(router.pid).cmdline()
    port = argv[argv.index("--prometheus-port") + 1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=60) as r:
        text = r.read().decode()
    found = re.findall(r"^mesh_worker_retries_total\{.*\} (\S+)$", text, re.MULTILINE)
    return sum(map(float, found))


def wait_for(condition, what: str) -> float:
    """When `condition` was first seen to hold."""
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"no {what} within {WAIT_S} s")
        time.sleep(0.1)
    return time.time()


def quiet(d) -> bool:
    """D holds no block and no request; a lost abort can leave P a waiting one (#92)."""
    found = metrics(d)
    states = ("running", "waiting", "parked_kv_load", "deferred_free")
    held = [found[f"atom:requests_{state}"] for state in states]
    return found["atom:kv_cache_blocks_used"] == 0 and not any(held)


def unloaded(topology) -> bool:
    return not any(any(loads(topology.routers[path]).values()) for path in PATHS)


def settled(p) -> bool:
    """P's fake GPU has ended every step given it; until then P's gauges are stale."""
    return all(f.time < time.time() for f in EngineRecord(p.workdir / RECORD).forwards)


@pytest.mark.topology("1p1d_relay")
def test_a_prefill_generate_ends_with_the_kv_transfer_params_completions_returns(
    topology, tokenizer
):
    p = topology.instances["p"]
    prompt = tokenizer.encode(words(40, lead=0), add_special_tokens=False)
    warm = p.post("/v1/completions", {"prompt_token_ids": prompt, "max_tokens": 1})
    assert warm[0] == 200, warm  # P's cache as warm for each call below
    body = {"prompt_token_ids": prompt, "kv_transfer_params": PREFILL, "max_tokens": 1}
    status, reply = p.post("/v1/completions", body | {"temperature": 0})
    assert status == 200, reply
    calls = {}
    with grpc.insecure_channel(p.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        for stream in (True, False):
            request = generate_request(prompt, PREFILL, stream, max_tokens=1)
            calls[stream] = list(stub.Generate(request, timeout=600))
    streamed, (unary,) = calls[True], calls[False]
    assert [m.WhichOneof("response") for m in streamed] == ["chunk", "complete"]
    first = list(streamed[0].chunk.token_ids)
    assert list(unary.complete.output_ids) == first and len(first) == 1

    # P's own params for each call: the transfer is its request, the blocks its own
    _, over_http, *over_grpc = admitted(p, prompt)
    sent = [reply["kv_transfer_params"]]
    for complete in (streamed[-1].complete, unary.complete):
        assert (complete.finish_reason, complete.completion_tokens) == ("max_tokens", 1)
        sent.append(json.loads(complete.kv_transfer_params_json))
    assert [kv["transfer_id"] for kv in sent] == [over_http.seq_id] + [
        r.seq_id for r in over_grpc
    ]
    blocks = {len(kv.pop("remote_block_ids")) for kv in sent}
    assert blocks == {len(prompt) // BLOCK + 1}
    assert sent[0]["first_token_id"] == first[0]
    for kv in sent:
        del kv["transfer_id"]
    assert sent[1] == sent[2] == sent[0]


@pytest.mark.topology("1p1d_relay")
@pytest.mark.parametrize("end", ["cancel", "abort"])
def test_a_generate_waiting_on_its_receive_ends_and_d_holds_its_blocks_till_reported(
    topology, tokenizer, end
):
    p, d = topology.instances["p"], topology.instances["d"]
    prompt = tokenizer.encode(words(23, lead=2 + len(end)), add_special_tokens=False)
    wait_for(lambda: quiet(d), "quiet D")
    with grpc.insecure_channel(p.grpc) as channel:
        request = generate_request(prompt, PREFILL, stream=False, max_tokens=1)
        (prefilled,) = pb_grpc.AtomEngineStub(channel).Generate(request, timeout=600)
    info = kv_transfer_info(p)
    kv = json.loads(prefilled.complete.kv_transfer_params_json)
    kv |= {"remote_dp_size": info["dp_size"], "remote_tp_size": info["tp_size"]}
    kv |= {"remote_dp_rank": kv["dp_rank"], "fake_fault": "timeout"}
    with grpc.insecure_channel(d.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        request = generate_request(prompt, kv, stream=True, max_tokens=8)
        call = stub.Generate(request)
        wait_for(lambda: metrics(d)["atom:requests_parked_kv_load"] == 1, "a parked D")
        if end == "cancel":
            call.cancel()
        else:
            stub.Abort(pb.AbortRequest(request_id=request.request_id))
            *_, last = call
            assert last.complete.finish_reason == "aborted"
        held = wait_for(
            lambda: metrics(d)["atom:requests_deferred_free"] == 1, "a hold"
        )
    freed = wait_for(lambda: metrics(d)["atom:requests_deferred_free"] == 0, "a free")

    (decode,) = admitted(d, prompt)
    receive = decode.receive
    assert not receive["ok"] and decode.steps == []
    assert receive["seconds"] == pytest.approx(fakes("relay")[0]["transfer_timeout_s"])
    assert decode.aborted < held < receive["time"] <= freed


@pytest.mark.parametrize("mode", MODES)
def test_relay_and_chunked_complete_and_d_answers_only_once_p_has_finished(
    topology, tokenizer, mode
):
    p, d = topology.instances["p"], topology.instances["d"]
    model, text = model_info(p).served_model_name, words(40, lead=10)
    prompt = tokenizer.encode(text, add_special_tokens=False)
    law = IdLaw.for_model(FIXTURE, {tokenizer.eos_token_id})
    expected = [tokenizer.decode([i]) for i in law.ids(fingerprint(prompt), 0, 12)]
    for path in PATHS:
        reply = send(topology.routers[path], completion(model, text, 12, stream=True))
        assert reply.status == 200, reply.replies
        assert answer(reply) == (expected, "length")  # P's first token, then D's
        prefill = admitted(p, prompt)[-1]
        assert reply.first >= prefill.steps[0].time  # P's output step had ended
    chunked = [
        r.kv_transfer_params.get("chunked_transfer") for r in admitted(d, prompt)
    ]
    assert chunked == ([True, True] if mode == "chunked" else [None, None])
    wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.parametrize("mode", MODES)
def test_p_and_d_are_given_the_same_params_rank_and_sampling_over_both_paths(
    topology, tokenizer, mode
):
    p, d = topology.instances["p"], topology.instances["d"]
    model, text = model_info(p).served_model_name, words(40, lead=20)
    prompt = tokenizer.encode(text, add_special_tokens=False)
    warm = p.post("/v1/completions", {"prompt_token_ids": prompt, "max_tokens": 1})
    assert warm[0] == 200, warm  # P's cache as warm for both paths
    for path in PATHS:
        reply = send(topology.routers[path], completion(model, text, 12))
        assert reply.status == 200, reply.replies
    _, *prefills = admitted(p, prompt)
    decodes = admitted(d, prompt)
    info = kv_transfer_info(p)  # what the gRPC router read from GetServerInfo

    def form(kv: dict) -> dict:
        """`kv` with its transfer id by type and P's block ids by count: each request
        has its own."""
        shown = {}
        if "transfer_id" in kv:
            shown["transfer_id"] = type(kv["transfer_id"])
        if "remote_block_ids" in kv:
            shown["remote_block_ids"] = len(kv["remote_block_ids"])
        return kv | shown

    for over_http, over_grpc in (prefills, decodes):
        assert over_grpc.sampling == over_http.sampling
        assert over_grpc.data_parallel_rank == over_http.data_parallel_rank
        assert form(over_grpc.kv_transfer_params) == form(over_http.kv_transfer_params)
    assert [r.sampling["max_tokens"] for r in (*prefills, *decodes)] == [1, 1, 12, 12]
    for prefill, decode in zip(prefills, decodes):  # each path's own transfer
        kv = decode.kv_transfer_params
        assert kv["remote_tp_size"] == info["tp_size"]
        if mode == "chunked":
            assert kv.items() >= info["chunked_transfer"].items()
            assert kv["transfer_id"] == prefill.kv_transfer_params["transfer_id"]
            assert re.fullmatch(r"xfer-[0-9a-f-]{36}", kv["transfer_id"])
        else:  # Mooncake's relay transfer id is P's request id
            assert kv["transfer_id"] == prefill.seq_id
    wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("fault", FAULTS)
def test_a_failed_or_timed_out_receive_ends_as_over_http_and_holds_d_blocks(
    topology, tokenizer, mode, fault
):
    p, d = topology.instances["p"], topology.instances["d"]
    model, (p_fake, d_fake) = model_info(p).served_model_name, fakes(mode)
    hold = float(d_fake["env"]["ATOM_PD_FAILED_WRITE_HOLD_S"])
    lead = 60 + 2 * [*FAULTS].index(fault)
    texts = {path: words(20, lead + i, FAULTS[fault]) for i, path in enumerate(PATHS)}
    wait_for(lambda: quiet(d), "quiet D")
    with concurrent.futures.ThreadPoolExecutor(len(PATHS)) as pool:
        sent = time.time()
        calls = {
            path: pool.submit(send, topology.routers[path], completion(model, text, 8))
            for path, text in texts.items()
        }
        wait_for(lambda: metrics(d)["atom:kv_cache_blocks_used"] > 0, "D's blocks")
        freed = wait_for(lambda: metrics(d)["atom:kv_cache_blocks_used"] == 0, "a free")
        replies = {path: call.result() for path, call in calls.items()}

    ended = {}
    for path, reply in replies.items():
        assert reply.status == 200, reply.replies
        (body,) = reply.replies
        assert body["usage"]["completion_tokens"] == 0  # D aborted it
        ended[path] = (reply.status, answer(reply)[1], reply.end - sent)
        prompt = tokenizer.encode(texts[path], add_special_tokens=False)
        (decode,) = admitted(d, prompt)
        receive = decode.receive
        assert not receive["ok"] and decode.steps == []
        failed_after = p_fake["transfer_timeout_s"] if fault == "timeout" else 0.0
        assert receive["seconds"] == pytest.approx(failed_after)
        assert receive["end"] + hold <= receive["time"] <= min(freed, reply.end)
    assert ended["grpc"][:2] == ended["http"][:2] == (200, "stop")
    assert ended["grpc"][2] <= ended["http"][2] + SLACK_S
    wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.parametrize("mode", MODES)
def test_a_cancel_while_d_waits_for_its_kv_keeps_its_blocks_till_the_transfer_reports(
    topology, tokenizer, mode
):
    p, d = topology.instances["p"], topology.instances["d"]
    model = model_info(p).served_model_name
    texts = {path: words(20, 70 + i, FAULTS["timeout"]) for i, path in enumerate(PATHS)}
    wait_for(lambda: quiet(d), "quiet D")
    clients = [
        start(topology.routers[path], completion(model, text, 8, stream=True))
        for path, text in texts.items()
    ]
    wait_for(lambda: metrics(d)["atom:requests_parked_kv_load"] == 2, "parked D")
    for client in clients:
        close(client)
    held = wait_for(lambda: metrics(d)["atom:requests_deferred_free"] == 2, "a hold")
    freed = wait_for(lambda: metrics(d)["atom:requests_deferred_free"] == 0, "a free")
    for text in texts.values():
        (decode,) = admitted(d, tokenizer.encode(text, add_special_tokens=False))
        assert not decode.receive["ok"] and decode.steps == []
        assert decode.aborted < held < decode.receive["time"] <= freed
    wait_for(lambda: unloaded(topology), "load on no worker")


def cancel_while_d_decodes(topology, tokenizer, path: str, lead: int) -> None:
    """A client cancel once D streams: P had let go of its load, and D aborts."""
    p, d = topology.instances["p"], topology.instances["d"]
    router = topology.routers[path]
    text = words(40, lead)
    prompt = tokenizer.encode(text, add_special_tokens=False)
    body = completion(model_info(p).served_model_name, text, MAX_TOKENS, stream=True)
    client = start(router, body)
    response = client.getresponse()
    assert response.status == 200 and response.readline().startswith(b"data: ")
    wait_for(lambda: any(r.steps for r in admitted(d, prompt)), "D decoding")
    p_url, d_url = router.workers  # its prefill worker, then its decode worker
    assert loads(router) == {p_url: 0, d_url: 1}  # P's load ended with its prefill
    close(client)
    wait_for(lambda: admitted(d, prompt)[0].aborted, "the cancel at D")
    (prefill,), (decode,) = admitted(p, prompt), admitted(d, prompt)
    assert prefill.aborted is None and decode.aborted is not None
    assert len(decode.output_ids) < MAX_TOKENS


@pytest.mark.parametrize("mode", MODES)
def test_a_client_cancel_while_d_decodes_aborts_d_after_p_let_go_of_its_load(
    topology, tokenizer, mode
):
    for i, path in enumerate(PATHS):
        cancel_while_d_decodes(topology, tokenizer, path, lead=40 + i)
        wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.topology("1p1d")
def test_a_chunked_d_refusal_cancels_p_and_ends_as_over_http(topology, tokenizer):
    p, d = topology.instances["p"], topology.instances["d"]
    info = model_info(p)
    text = words(8, lead=32)
    prompt = tokenizer.encode(text, add_special_tokens=False)
    # D refuses the context; P, sent max_tokens 1, takes it and waits for D
    body = completion(info.served_model_name, text, info.max_context_length - 2)
    routers = [topology.routers[path] for path in PATHS]
    before = [retries(router) for router in routers]
    statuses = [send(router, body).status for router in routers]
    assert statuses == [400, 400] and [retries(r) for r in routers] == before
    assert admitted(d, prompt) == []
    prefills = admitted(p, prompt)  # a cancel before admission leaves none
    assert all(r.aborted is not None and r.steps == [] for r in prefills)
    wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.parametrize("mode", MODES)
def test_a_p_failure_sends_no_d_in_relay_and_aborts_d_in_chunked_as_over_http(
    topology, tokenizer, mode
):
    p, d = topology.instances["p"], topology.instances["d"]
    blocks = fakes(mode)[0]["num_kvcache_blocks"]
    text = words(blocks * BLOCK + BLOCK // 2, lead=30)  # past P's KV pool, not D's
    prompt = tokenizer.encode(text, add_special_tokens=False)
    body = completion(model_info(p).served_model_name, text, 8)
    routers = [topology.routers[path] for path in PATHS]
    before = [retries(router) for router in routers]
    statuses = [send(router, body).status for router in routers]
    assert statuses == [400, 400] and [retries(r) for r in routers] == before
    assert admitted(p, prompt) == []
    decodes = admitted(d, prompt)  # chunked: a cancel before admission leaves none
    if mode == "relay":
        assert decodes == []
    else:
        assert decodes and all(r.aborted and r.steps == [] for r in decodes)
    wait_for(lambda: unloaded(topology), "load on no worker")


def cancel_while_p_prefills(topology, tokenizer, path: str, lead: int) -> list:
    """A client cancel once P admitted a prompt its law prefills for seconds; D's
    admissions of it, each aborted unrun."""
    p, d = topology.instances["p"], topology.instances["d"]
    text = words(LONG, lead)
    prompt = tokenizer.encode(text, add_special_tokens=False)
    wait_for(lambda: settled(p), "P's steps to end")
    body = completion(model_info(p).served_model_name, text, 8, stream=True)
    client = start(topology.routers[path], body)
    wait_for(lambda: admitted(p, prompt), "P's admission")
    close(client)
    wait_for(lambda: admitted(p, prompt)[0].aborted, "the cancel at P")
    (prefill,) = admitted(p, prompt)
    # The cancel reached P as it waited for D or before its prefill's output; a prefill
    # already dispatched still completes, and in relay keeps its blocks (#93).
    assert prefill.steps == [] or prefill.aborted < prefill.steps[0].time
    decodes = admitted(d, prompt)
    assert all(r.aborted is not None and r.steps == [] for r in decodes)
    wait_for(lambda: settled(p), "P's steps to end")
    return decodes


@pytest.mark.parametrize("mode", MODES)
def test_a_client_cancel_while_p_prefills_aborts_p_and_d_as_over_http(
    topology, tokenizer, mode
):
    for i, path in enumerate(PATHS):
        decodes = cancel_while_p_prefills(topology, tokenizer, path, lead=50 + i)
        if mode == "relay":
            assert decodes == []  # P never answered, so no D was sent
        wait_for(lambda: unloaded(topology), "load on no worker")


@pytest.mark.topology("1p1d")
def test_the_launch_command_starts_1p1d_with_atomesh_serves_and_leaves_no_process(
    topology, topologies
):
    assert "ready: " in topology.tail()
    for instance in topology.instances.values():
        assert instance.get("/health") == (200, {"status": "ok"})
    for router in topology.routers.values():
        status, listing = router.get("/workers")
        assert status == 200, listing
        healthy = [w["url"] for w in listing["workers"] if w["is_healthy"]]
        assert sorted(healthy) == sorted(router.workers)
    p = topology.instances["p"]
    body = completion(model_info(p).served_model_name, words(8, lead=80), 8)
    reply = send(topology.routers["grpc"], body)
    assert reply.status == 200 and reply.replies[0]["usage"]["completion_tokens"] == 8
    groups = [*topology.groups(), *topology.groups("routers")]
    assert all(group_members(group) for group in groups)
    topologies.down()  # fails when the launcher failed or left a process
    assert topology.proc.returncode == 0
    assert [pid for group in groups for pid in group_members(group)] == []
