# SPDX-License-Identifier: MIT
"""The Session over a real channel to a fake instance: one message per engine step, each
Session's seq ids in a window of their own, ended and refused as Generate's requests."""

import queue
import time
from pathlib import Path

import grpc
import pytest
from fastapi import HTTPException

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.entrypoints.grpc import frames
from atom.entrypoints.grpc.server import _rejected
from atom.entrypoints.grpc.session import SEQ_IDS
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from atom.sampling_params import SamplingParams
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.fake.serve import FIRST_SEQ_ID_ENV
from tests.atom_grpc.harness.topologies import Topology
from tests.atom_grpc.test_grpc_generate import (
    MAX_TOKENS,
    REFUSALS,
    WAIT_S,
    generate_request,
    holds_blocks,
    idle,
    prompt_ids,
    wait_for,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
INVALID = v1.RejectedRequest.INVALID
OVERLOADED = v1.RejectedRequest.OVERLOADED
INTERNAL = v1.RejectedRequest.INTERNAL
GREEDY = {"temperature": 0}


def submit(requests: dict[int, tuple[list[int], dict]]) -> list[bytes]:
    """A SubmitBatch of {seq_id: (prompt, fields)}: engine_v1 SamplingParams fields as
    Generate's tests name them, invalid ones included, `kv_transfer_params` and `stops`.
    """
    specs, samplings = [], []
    for seq_id, (prompt, fields) in requests.items():
        fields = dict(fields)
        stops, kv = fields.pop("stops", []), fields.pop("kv_transfer_params", None)
        specs.append(frames.SubmitRequest(seq_id, prompt, SamplingParams(), stops, kv))
        samplings.append(v1.SamplingParams(**fields))
    message = frames.encode_submit(specs)
    envelope = v1.Envelope.FromString(message[0])
    for spec, sampling in zip(envelope.submit.requests, samplings):
        spec.sampling.CopyFrom(sampling)
    return [envelope.SerializeToString(), *message[1:]]


class SessionCall:
    """One Session over its own channel, past its Ready: `send` a message, `read` the
    next one decoded, each StreamBatch kept in `batches`; `drop` and `end` end it."""

    def __init__(self, target: str):
        self.channel = grpc.insecure_channel(target)
        self.sends = queue.SimpleQueue()
        stub = pb_grpc.AtomEngineStub(self.channel)
        self.call = stub.Session(iter(self.sends.get, None), timeout=5 * WAIT_S)
        self.batches = []
        assert self.read()[0] == "ready"

    def send(self, message: list[bytes]) -> None:
        self.sends.put(pb.Frames(frames=message))

    def read(self) -> tuple[str, object]:
        kind, body = frames.decode_server(list(next(self.call).frames))
        if kind == "stream":
            self.batches.append(body)
        return kind, body

    def until_ended(self, seq_ids) -> dict:
        """Each Completion, read on until every one of `seq_ids` ended."""
        ended = {}
        while not set(seq_ids) <= ended.keys():
            kind, body = self.read()
            if kind == "completions":
                ended |= {item.seq_id: item for item in body}
            elif kind != "stream":
                raise AssertionError(f"{kind}: {body}")
        return ended

    def drop(self, *_) -> None:
        self.call.cancel()

    def end(self, *_) -> None:
        self.sends.put(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.drop()
        self.channel.close()


def streamed(batches, seq_id: int) -> list[int]:
    """Every id `seq_id`'s StreamBatch entries carried, in order."""
    outputs = (out for batch in batches for i, out in batch if i == seq_id)
    return [token for out in outputs for token in out.output_tokens]


def end_by_abort(session: SessionCall, seq_id: int) -> None:
    session.send(frames.encode_abort([seq_id]))
    ended = session.until_ended([seq_id])[seq_id]
    assert ended.leave_reason == "aborted"
    assert ended.completion_token_ids == streamed(session.batches, seq_id)


@pytest.mark.topology("single")
def test_a_session_sends_ready_then_one_message_per_engine_step_in_its_window(topology):
    (instance,) = topology.instances.values()
    lengths = {0: 20, 1: 21, 7: 22}
    prompts = dict(zip(lengths, map(prompt_ids, (60, 61, 62))))
    with SessionCall(instance.grpc) as session:
        fields = {i: {"max_tokens": n} | GREEDY for i, n in lengths.items()}
        session.send(submit({i: (prompts[i], fields[i]) for i in lengths}))
        ended, batches = session.until_ended(lengths), session.batches

    record = EngineRecord(instance.workdir / RECORD)
    admitted = {i: record.with_prompt(prompts[i])[0] for i in lengths}
    base = admitted[0].seq_id
    assert base >= SEQ_IDS and base % SEQ_IDS == 0  # a window above the API's own ids
    assert {i: r.seq_id for i, r in admitted.items()} == {i: base | i for i in lengths}
    steps = {i: [step for step in r.steps if step.ids] for i, r in admitted.items()}
    taken, times = dict.fromkeys(lengths, 0), []
    for batch in batches:
        ended_at = set()
        for i, output in batch:
            step = steps[i][taken[i]]
            taken[i] += 1
            assert step.ids[: len(output.output_tokens)] == output.output_tokens
            ended_at.add(step.time)
        assert len(ended_at) == 1  # one engine step's outputs, all in one message
        times.append(ended_at.pop())
    assert times == sorted(set(times))  # and each step's in one message
    for i, n in lengths.items():
        ids = ended[i].completion_token_ids
        assert ids == streamed(batches, i) == admitted[i].output_ids[:n]
        assert ended[i].leave_reason == "max_tokens"
        assert ended[i].num_prompt_tokens == len(prompts[i])


@pytest.mark.topology("single")
def test_two_sessions_get_disjoint_seq_ids_and_dropping_one_ends_only_its_own(topology):
    (instance,) = topology.instances.values()
    wait_for(lambda: idle(instance), "idle engine")
    lengths = {"dropped": MAX_TOKENS, "kept": 3000}
    prompts = {"dropped": [prompt_ids(63), prompt_ids(64)]}
    prompts["kept"] = [prompt_ids(65), prompt_ids(66)]
    with SessionCall(instance.grpc) as dropped, SessionCall(instance.grpc) as kept:
        for name, session in (("dropped", dropped), ("kept", kept)):
            fields = {"max_tokens": lengths[name]} | GREEDY
            session.send(submit({i: (p, fields) for i, p in enumerate(prompts[name])}))
            assert session.read()[0] == "stream"  # running
        dropped.drop()
        dropped_at = time.time()
        ended = kept.until_ended((0, 1))
    wait_for(lambda: idle(instance), "return of every block")

    record = EngineRecord(instance.workdir / RECORD)
    admitted = {k: [record.with_prompt(p)[0] for p in v] for k, v in prompts.items()}
    windows = {k: {r.seq_id - i for i, r in enumerate(v)} for k, v in admitted.items()}
    assert len(windows["dropped"]) == len(windows["kept"]) == 1  # ids 0, 1 in each
    assert windows["dropped"] != windows["kept"]
    assert all(len(r.output_ids) < MAX_TOKENS for r in admitted["dropped"])
    for item in ended.values():
        assert item.leave_reason == "max_tokens"
        assert len(item.completion_token_ids) == lengths["kept"]
    assert max(s.time for r in admitted["kept"] for s in r.steps) > dropped_at


@pytest.mark.topology("single")
@pytest.mark.parametrize(
    "lead, end",
    [(67, end_by_abort), (68, SessionCall.drop), (69, SessionCall.end)],
    ids=["abort", "cancelled-call", "client-side-ended"],
)
def test_an_ended_session_request_stops_and_its_blocks_return_to_the_free_pool(
    topology, lead, end
):
    (instance,) = topology.instances.values()
    prompt = prompt_ids(lead)
    wait_for(lambda: idle(instance), "idle engine")
    with SessionCall(instance.grpc) as session:
        session.send(submit({5: (prompt, {"max_tokens": MAX_TOKENS})}))
        wait_for(lambda: holds_blocks(instance), "blocks held")
        end(session, 5)
    wait_for(lambda: idle(instance), "return of every block")
    (request,) = EngineRecord(instance.workdir / RECORD).with_prompt(prompt)
    assert len(request.output_ids) < MAX_TOKENS


@pytest.mark.topology("single")
def test_a_refused_request_comes_back_rejected_invalid_with_generates_message(topology):
    (instance,) = topology.instances.values()
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        limit = stub.GetModelInfo(pb.GetModelInfoRequest()).max_context_length
        cases = [build(limit) for _, build in REFUSALS.values()]
        cases.append(([1, 8, 9], {"max_tokens": 4, "n": 2, "temperature": 1}))
        answers = []
        for prompt, fields in cases:
            (answer,) = stub.Generate(
                generate_request(prompt, **fields), timeout=WAIT_S
            )
            assert answer.error.http_status == 400
            answers.append((INVALID, answer.error.message))
    structured = {"json_schema": '{"type": "object"}', "max_tokens": 4, "stops": [[10]]}
    cases += [([1, 8, 9], structured), ([1, 8, 9], {"max_tokens": 4})]
    stop = "Structured output does not support ignore_eos or custom stop strings"
    answers += [(INVALID, stop), (INVALID, f"seq_id {SEQ_IDS} is not below {SEQ_IDS}")]
    seq_ids = [*range(100, 100 + len(cases) - 1), SEQ_IDS]
    with SessionCall(instance.grpc) as session:
        session.send(submit({1: (prompt_ids(70), {"max_tokens": MAX_TOKENS})}))
        again = {1: ([1, 8, 9], {"max_tokens": 4})}  # while 1 is in flight
        session.send(submit(dict(zip(seq_ids, cases)) | again))
        kind, refused = session.read()
        while kind != "rejected":
            kind, refused = session.read()
        end_by_abort(session, 1)
    by_id = {item.seq_id: (item.code, item.reason) for item in refused}
    assert [by_id[i] for i in seq_ids] == answers
    assert by_id[1] == (INVALID, "seq_id 1 is already in flight")


@pytest.mark.topology("single")
def test_a_submit_batch_past_grpcs_default_4_mib_is_read(topology):
    (instance,) = topology.instances.values()
    prompt = [1] * 600_000  # 2.4 MB a request, each refused before preprocess
    with SessionCall(instance.grpc) as session:
        session.send(submit({i: (prompt, {"max_tokens": 0}) for i in (0, 1)}))
        kind, refused = session.read()
    assert kind == "rejected"
    reason = "max_tokens must be at least 1, got 0"
    assert refused == [frames.Rejection(i, reason, INVALID) for i in (0, 1)]


@pytest.mark.parametrize(
    "error, code",
    [
        (ValueError("a value the routes refuse"), INVALID),
        (HTTPException(status_code=400, detail="refused"), INVALID),
        (HTTPException(status_code=503, detail="not now"), OVERLOADED),
        (HTTPException(status_code=500, detail="failed"), INTERNAL),
        (RuntimeError("failed"), INTERNAL),
    ],
    ids=["value-error", "http-400", "http-503", "http-500", "other-error"],
)
def test_a_refusal_comes_back_with_the_code_atomesh_reads_as_its_status(error, code):
    message = getattr(error, "detail", None) or str(error)
    assert _rejected(3, error) == frames.Rejection(3, message, code)


def test_the_api_process_ids_wrap_below_an_open_sessions_window(
    tmp_path_factory, topologies
):
    topologies.down()  # one topology up at a time
    scratch = tmp_path_factory.mktemp("wrap")  # short: ZMQ ipc sockets live in it
    path = scratch / "wrap.toml"
    path.write_text(
        f'[[instance]]\nname = "wrap"\nmodel = "{FIXTURE}"\nargs = []\ngrpc = true\n'
        f'fake = {{ env = {{ {FIRST_SEQ_ID_ENV} = "{SEQ_IDS - 2}" }} }}\n'
    )
    launch = Topology(path, scratch)
    try:
        launch.wait_ready()
        (instance,) = launch.instances.values()
        held, prompts = prompt_ids(71), [prompt_ids(lead) for lead in (72, 73, 74)]
        with SessionCall(instance.grpc) as session:
            session.send(submit({0: (held, {"max_tokens": MAX_TOKENS})}))
            assert session.read()[0] == "stream"
            with grpc.insecure_channel(instance.grpc) as channel:
                stub = pb_grpc.AtomEngineStub(channel)
                for prompt in prompts:  # the API process's own ids, from 2^24 - 2
                    request = generate_request(prompt, max_tokens=4, **GREEDY)
                    (answer,) = stub.Generate(request, timeout=WAIT_S)
                    assert answer.WhichOneof("response") == "complete"
            record = EngineRecord(instance.workdir / RECORD)
            ids = [record.with_prompt(prompt)[0].seq_id for prompt in prompts]
            assert ids == [SEQ_IDS - 2, SEQ_IDS - 1, 0]
            assert record.with_prompt(held)[0].seq_id == SEQ_IDS  # window 0's seq_id 0
            end_by_abort(session, 0)
    finally:
        left = launch.stop()
    assert left == {}
