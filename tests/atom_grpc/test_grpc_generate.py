# SPDX-License-Identifier: MIT
"""Generate and Abort over a real channel to a fake instance: admitted, streamed, ended
and refused as `/v1/completions` on the same instance treats the same request."""

import json
import re
import time
import urllib.request
import uuid
from itertools import accumulate
from pathlib import Path

import grpc
import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.topologies import Topology

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
STOP = "你好"  # a fixture word that no law id is, so it ends no request
MAX_TOKENS = 50000  # far past where an ended request stops
WAIT_S = 120


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def prompt_ids(lead: int) -> list[int]:
    """A prompt whose first block no other test's prompt shares."""
    return [1, lead, *range(8, 38)]


def generate_request(
    prompt,
    stream=False,
    stops=(),
    kv_transfer_params=None,
    data_parallel_rank=None,
    request_id="",
    **sampling,
) -> pb.GenerateRequest:
    """A GenerateRequest from the fields a `/v1/completions` body names alike."""
    request = pb.GenerateRequest(
        request_id=request_id or f"gen-{uuid.uuid4().hex}",
        prompt_token_ids=prompt,
        sampling=v1.SamplingParams(**sampling),
        stop_token_sequences=[v1.TokenIds(ids=ids) for ids in stops],
        stream=stream,
    )
    if kv_transfer_params is not None:
        request.kv_transfer_params_json = json.dumps(kv_transfer_params)
    if data_parallel_rank is not None:
        request.data_parallel_rank = data_parallel_rank
    return request


def generate(target: str, prompt, **fields) -> list[pb.GenerateResponse]:
    """Every message of one Generate call, over a fresh channel."""
    with grpc.insecure_channel(target) as channel:
        call = pb_grpc.AtomEngineStub(channel).Generate(
            generate_request(prompt, **fields), timeout=600
        )
        return list(call)


def kinds(messages) -> list[str]:
    return [message.WhichOneof("response") for message in messages]


def metrics(instance) -> dict[str, float]:
    """The instance's unlabelled `atom:` gauges and counters from /metrics."""
    with urllib.request.urlopen(instance.url + "/metrics", timeout=60) as reply:
        lines = reply.read().decode().splitlines()
    found = (re.fullmatch(r"(atom:\w+) (\S+)", line) for line in lines)
    return {match[1]: float(match[2]) for match in found if match}


def wait_for(condition, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"no {what} within {WAIT_S} s")
        time.sleep(0.2)


def holds_blocks(instance) -> bool:
    return metrics(instance)["atom:kv_cache_blocks_used"] > 0


def idle(instance) -> bool:
    found = metrics(instance)
    return (
        found["atom:kv_cache_blocks_used"] == 0 and found["atom:requests_running"] == 0
    )


@pytest.mark.topology("single")
def test_a_stream_sends_each_engine_step_as_one_chunk_and_matches_the_unary_call(
    topology,
):
    (instance,) = topology.instances.values()
    prompt, fields = prompt_ids(40), {"max_tokens": 40, "temperature": 0}
    cold = generate(instance.grpc, prompt, stream=True, **fields)
    warm = generate(instance.grpc, prompt, stream=True, **fields)
    (unary,) = generate(instance.grpc, prompt, stream=False, **fields)
    assert kinds(cold) == ["chunk"] * (len(cold) - 1) + ["complete"]

    chunks, complete = [m.chunk for m in cold[:-1]], cold[-1].complete
    ids = [list(chunk.token_ids) for chunk in chunks]
    request, *_ = EngineRecord(instance.workdir / RECORD).with_prompt(prompt)
    steps = [step.ids for step in request.steps]
    assert ids[:-1] == steps[: len(ids) - 1]  # one engine step, one chunk
    assert steps[len(ids) - 1][: len(ids[-1])] == ids[-1]  # cut at max_tokens
    assert max(len(step) for step in ids) > 1  # an MTP step's ids ride together
    assert [c.completion_tokens for c in chunks] == list(accumulate(map(len, ids)))
    assert {c.prompt_tokens for c in chunks} == {len(prompt)}
    assert (complete.finish_reason, complete.cached_tokens) == ("max_tokens", 0)
    assert (complete.prompt_tokens, complete.completion_tokens) == (len(prompt), 40)
    assert not complete.output_ids and not complete.HasField("kv_transfer_params_json")

    complete = warm[-1].complete
    assert complete.cached_tokens > 0  # the cold call's prompt blocks
    assert {m.chunk.cached_tokens for m in warm[:-1]} == {complete.cached_tokens}
    flat = [token for step in ids for token in step]
    assert [t for m in warm[:-1] for t in m.chunk.token_ids] == flat
    assert unary.WhichOneof("response") == "complete"
    assert list(unary.complete.output_ids) == flat
    complete.output_ids[:] = flat
    assert unary.complete == complete


@pytest.mark.topology("single")
def test_generate_returns_the_ids_completions_gets_and_calls_no_tokenizer(
    topology, tokenizer
):
    (instance,) = topology.instances.values()
    prompt, stop_ids = prompt_ids(41), tokenizer.encode(STOP, add_special_tokens=False)
    fields = {"max_tokens": 30, "temperature": 0}
    body = {"prompt_token_ids": prompt, "stop": [STOP], **fields}
    status, reply = instance.post("/v1/completions", body)
    assert status == 200, reply
    path = instance.workdir / RECORD
    calls = len(EngineRecord(path).tokenizer)
    # atomesh sends the stop strings with their ids; ATOM must not encode them
    strings = v1.StopStrings(many=v1.StringList(items=[STOP]))
    stream = generate(
        instance.grpc, prompt, stops=[stop_ids], stop_strings=strings, **fields
    )
    record = EngineRecord(path)
    assert record.tokenizer[calls:] == []

    assert kinds(stream) == ["complete"]
    ids = list(stream[0].complete.output_ids)
    over_http, over_grpc = record.with_prompt(prompt)
    assert len(ids) == 30 and stream[0].complete.finish_reason == "max_tokens"
    assert over_http.output_ids[:30] == over_grpc.output_ids[:30] == ids
    text = tokenizer.decode(ids, skip_special_tokens=True)
    assert reply["choices"][0]["text"] == text
    stops = over_http.stop_token_sequences
    assert over_grpc.stop_token_sequences == stops == [stop_ids]
    assert over_grpc.sampling == over_http.sampling | {"stop_strings": None}


@pytest.mark.topology("single")
def test_a_stop_id_ends_generate_where_its_string_ends_completions(topology, tokenizer):
    (instance,) = topology.instances.values()
    prompt, body = prompt_ids(42), {"max_tokens": 12, "temperature": 0}
    (free,) = generate(instance.grpc, prompt, **body)
    ids = list(free.complete.output_ids)
    stop = tokenizer.decode([ids[5]])
    assert tokenizer.encode(stop, add_special_tokens=False) == [ids[5]]
    status, reply = instance.post(
        "/v1/completions", {"prompt_token_ids": prompt, "stop": [stop], **body}
    )
    assert status == 200, reply
    # kept, the empty sequence would end the request at its first token
    (stopped,) = generate(instance.grpc, prompt, stops=[[], [ids[5]]], **body)
    complete = stopped.complete
    assert (complete.finish_reason, list(complete.output_ids)) == (
        "stop_sequence",
        ids[:6],
    )
    choice = reply["choices"][0]
    text = tokenizer.decode(ids[:6], skip_special_tokens=True)
    assert (choice["finish_reason"], choice["text"]) == ("stop", text)


def end_by_disconnect(instance, prompt) -> None:
    body = {"prompt_token_ids": prompt, "max_tokens": MAX_TOKENS, "stream": True}
    request = urllib.request.Request(
        instance.url + "/v1/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as reply:
        reply.readline()
        wait_for(lambda: holds_blocks(instance), "blocks held")


def end_by_cancel(instance, prompt) -> None:
    request = generate_request(prompt, stream=True, max_tokens=MAX_TOKENS)
    with grpc.insecure_channel(instance.grpc) as channel:
        call = pb_grpc.AtomEngineStub(channel).Generate(request)
        next(call)
        wait_for(lambda: holds_blocks(instance), "blocks held")
        call.cancel()


def end_by_abort(instance, prompt) -> None:
    request = generate_request(prompt, stream=True, max_tokens=MAX_TOKENS)
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        call = stub.Generate(request)
        next(call)
        wait_for(lambda: holds_blocks(instance), "blocks held")
        stub.Abort(pb.AbortRequest(request_id=request.request_id))
        *_, last = call
        for request_id in (request.request_id, "never-sent"):  # finished, unknown
            stub.Abort(pb.AbortRequest(request_id=request_id))
    assert last.complete.finish_reason == "aborted"


@pytest.mark.topology("single")
@pytest.mark.parametrize(
    "lead, end",
    [(43, end_by_disconnect), (44, end_by_cancel), (45, end_by_abort)],
    ids=["http-disconnect", "grpc-cancel", "grpc-abort"],
)
def test_an_ended_request_stops_and_its_blocks_return_to_the_free_pool(
    topology, lead, end
):
    (instance,) = topology.instances.values()
    prompt = prompt_ids(lead)
    wait_for(lambda: idle(instance), "idle engine")
    end(instance, prompt)
    wait_for(lambda: idle(instance), "return of every block")
    (request,) = EngineRecord(instance.workdir / RECORD).with_prompt(prompt)
    assert len(request.output_ids) < MAX_TOKENS


CHUNKED = {"do_remote_prefill": True, "chunked_transfer": True, "remote_tp_size": 2}
REFUSALS = {  # what each refusal says, and its prompt and fields given max_model_len
    "max_tokens below 1": (
        "max_tokens must be at least 1",
        lambda limit: ([1, 8, 9], {"max_tokens": 0}),
    ),
    "max_tokens above the cap": (
        "for this deployment",
        lambda limit: ([1, 8, 9], {"max_tokens": limit + 1}),
    ),
    "empty prompt": ("is empty", lambda limit: ([], {"max_tokens": 4})),
    "past max_model_len": (
        "maximum context length",
        lambda limit: ([1, 8, 9], {"max_tokens": limit - 2}),
    ),
    "past the KV pool": (
        "KV cache holds at most",
        lambda limit: ([1] * (limit - 1), {"max_tokens": 1}),
    ),
    "top_p out of range": (
        "top_p must be in range",
        lambda limit: ([1, 8, 9], {"top_p": 0.0}),
    ),
    "top_k out of range": ("top_k must be", lambda limit: ([1, 8, 9], {"top_k": -2})),
    "chunked receive from a wider P": (
        "Chunked P/D requires",
        lambda limit: ([1, 8, 9], {"kv_transfer_params": CHUNKED}),
    ),
}


def refusals(instance, prompt, fields) -> list[tuple[int, str]]:
    """(status, message) from `/v1/completions`, then from Generate, unary and streamed."""
    status, body = instance.post(
        "/v1/completions", {"prompt_token_ids": prompt, **fields}
    )
    assert status != 200, body
    answers = [(status, json.loads(body)["error"]["message"])]
    for stream in (False, True):
        messages = generate(instance.grpc, prompt, stream=stream, **fields)
        assert kinds(messages) == ["error"]
        answers.append((messages[0].error.http_status, messages[0].error.message))
    return answers


@pytest.mark.topology("single")
@pytest.mark.parametrize("case", REFUSALS)
def test_generate_refuses_with_the_status_and_message_of_completions(topology, case):
    (instance,) = topology.instances.values()
    with grpc.insecure_channel(instance.grpc) as channel:
        info = pb_grpc.AtomEngineStub(channel).GetModelInfo(pb.GetModelInfoRequest())
    says, build = REFUSALS[case]
    http, unary, streamed = refusals(instance, *build(info.max_context_length))
    assert http[0] == 400 and says in http[1]
    assert http == unary == streamed


@pytest.mark.topology("single")
def test_n_above_1_is_refused(topology):
    (instance,) = topology.instances.values()
    (message,) = generate(instance.grpc, [1, 8, 9], max_tokens=4, n=2, temperature=1)
    assert message.error.http_status == 400


def test_an_out_of_range_dp_rank_is_refused_as_completions_refuses_it(
    tmp_path_factory, topologies
):
    topologies.down()  # one topology up at a time
    scratch = tmp_path_factory.mktemp("dp2")  # short: ZMQ ipc sockets live in it
    path = scratch / "dp2.toml"
    args = '["-tp", "2", "--enable-dp-attention"]'
    path.write_text(
        f'[[instance]]\nname = "dp2"\nmodel = "{FIXTURE}"\nargs = {args}\n'
        "fake = {}\ngrpc = true\n"
    )
    launch = Topology(path, scratch)
    try:
        launch.wait_ready()
        (instance,) = launch.instances.values()
        for rank in (2, -1):
            fields = {"max_tokens": 4, "data_parallel_rank": rank}
            http, unary, streamed = refusals(instance, [1, 8, 9], fields)
            refused = f"Invalid data_parallel_rank={rank}; global_engine_count=2"
            assert http == unary == streamed == (400, refused)
    finally:
        left = launch.stop()
    assert left == {}
