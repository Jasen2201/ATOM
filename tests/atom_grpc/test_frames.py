# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""The Session's engine_v1 codec: both halves round-trip every message, buffers follow
their refs, and a SubmitBatch decodes to what /v1/completions builds for its request."""

import asyncio
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import frames
from atom.entrypoints.openai import api_server
from atom.entrypoints.openai.protocol import DEFAULT_MAX_TOKENS, CompletionRequest
from atom.model_engine.llm_engine import InputOutputProcessor
from atom.model_engine.request import RequestOutput
from atom.model_engine.zmq_protocol import engine_v1_pb2 as pb
from atom.sampling_params import RequiredChannel, SamplingParams

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
VERSION = frames.PROTOCOL_VERSION
# A P instance's kv_transfer_params_output shape, with every JSON kind.
KV = {
    "do_remote_prefill": True,
    "remote_block_ids": [3, 4, 5],
    "remote_engine_id": "p0",
    "remote_port": 7100,
    "ratio": 0.5,
    "first_token_id": None,
    "draft_token_ids": [],
    "endpoint": {"host": "10.0.0.2", "ports": [1, -2]},
    "extra": {},
}
# What a request that sets no sampling field reads as.
DEFAULTS = SamplingParams(
    max_tokens=DEFAULT_MAX_TOKENS, max_tokens_is_default=True, logprobs=False
)


def le32(*ids: int) -> bytes:
    return struct.pack(f"<{len(ids)}i", *ids)


def ref(frame: int, nbytes: int, offset: int = 0) -> pb.BufferRef:
    return pb.BufferRef(frame=frame, offset=offset, nbytes=nbytes)


def submit(specs, *data: bytes, version: int = VERSION) -> list[bytes]:
    """A SubmitBatch built by hand: `specs`, then `data` as frames 1, 2, ..."""
    envelope = pb.Envelope(protocol_version=version, submit=pb.SubmitBatch())
    envelope.submit.requests.extend(specs)
    return [envelope.SerializeToString(), *data]


def test_submit_batch_and_abort_round_trip():
    channel = RequiredChannel(after=(1, 2), opening=(3, 4, 5), opens_at=2, end_ids=(9,))
    sampling = SamplingParams(
        temperature=0.6,
        top_k=20,
        top_p=0.95,
        max_tokens=77,
        ignore_eos=True,
        stop_strings=["Hello world", "zu"],
        logprobs=True,
        top_logprobs=2,
        required_channel=channel,
        structured_reasoning_end="</think>",
    )
    structured = SamplingParams(
        stop_strings="zu",
        logprobs=3,
        max_tokens_is_default=True,
        json_schema='{"type": "object"}',
        structured_tool_format="kimi_k3",
    )
    requests = [
        frames.SubmitRequest(
            seq_id=(1 << 24) - 1,
            prompt_token_ids=[0, 5, 70000, 2**31 - 1],
            sampling_params=sampling,
            stop_token_sequences=[[5, 6], [107]],
            kv_transfer_params=KV,
            request_id="cmpl-1",
            data_parallel_rank=3,
            dp_session_id="s1",
            dp_parent_session_id="s0",
        ),
        frames.SubmitRequest(
            seq_id=0, prompt_token_ids=[1], sampling_params=structured
        ),
    ]
    assert frames.decode_client(frames.encode_submit(requests)) == ("submit", requests)
    ids = [0, 7, 2**64 - 1]
    assert frames.decode_client(frames.encode_abort(ids)) == ("abort", ids)


def test_ready_stream_completions_and_rejected_round_trip():
    assert frames.decode_server(frames.encode_ready(4096)) == ("ready", 4096)
    assert frames.decode_server(frames.encode_ready()) == ("ready", None)
    records = [(-0.5, 1, 21, -0.5, 22, -1.25)]  # (logprob, rank, id, logprob, ...)
    step = [
        (5, RequestOutput(5, [11, 12, 13, 14], False, num_cached_tokens=64)),
        (6, RequestOutput(6, [], True, "abort")),
        (7, RequestOutput(7, [21], True, "stop_21", KV, 3, records)),
        (8, RequestOutput(8, [31, 32], False, logprobs=[(-0.125, 1), (-0.25, 2)])),
        (9, RequestOutput(9, [], False, logprobs=[])),
    ]
    assert frames.decode_server(frames.encode_stream(step)) == ("stream", step)
    assert frames.decode_server(frames.encode_stream([])) == ("stream", [])
    done = [
        frames.Completion(7, [21, 22], "stop_21", 12, 4, 1.5, 1.75, 2.0, [-0.5, -1.0]),
        frames.Completion(6, [], "abort", kv_transfer_params_output=KV),
        frames.Completion(5, [11], "eos", external_request_id="cmpl-5"),
    ]
    decoded = frames.decode_server(frames.encode_completions(done))
    assert decoded == ("completions", done)
    refused = [
        frames.Rejection(3, "max_tokens must be at least 1", frames.INVALID),
        frames.Rejection(4, "the KV pool is full", pb.RejectedRequest.OVERLOADED),
        frames.Rejection(5, "the engine failed", pb.RejectedRequest.INTERNAL),
    ]
    decoded = frames.decode_server(frames.encode_rejected(refused))
    assert decoded == ("rejected", refused)


def test_buffers_are_read_through_their_refs_in_shared_or_separate_frames():
    shared = le32(1, 2, 3).ljust(64, b"\0") + le32(4, 5)  # the second 64-byte aligned
    specs = [
        pb.RequestSpec(seq_id=1, prompt_token_ids=ref(1, 8, offset=64)),
        pb.RequestSpec(seq_id=2, prompt_token_ids=ref(2, 4)),
        pb.RequestSpec(seq_id=3, prompt_token_ids=ref(1, 12)),
    ]
    _, items = frames.decode_client(submit(specs, shared, le32(6)))
    prompts = [(r.seq_id, r.prompt_token_ids) for r in items]
    assert prompts == [(1, [4, 5]), (2, [6]), (3, [1, 2, 3])]
    # The client half reads refs alike: a completion's ids and logprobs in one frame.
    item = pb.Completion(
        seq_id=9,
        completion_token_ids=ref(1, 4, offset=8),
        logprobs=ref(1, 8, offset=16),
    )
    envelope = pb.Envelope(
        protocol_version=VERSION, completions=pb.Completions(items=[item])
    )
    data = le32(0, 0, 42, 0) + struct.pack("<d", 0.25)
    _, [done] = frames.decode_server([envelope.SerializeToString(), data])
    assert (done.completion_token_ids, done.logprobs) == ([42], [0.25])


def test_data_frames_are_little_endian():
    request = frames.SubmitRequest(1, [1, 258], SamplingParams())
    assert frames.encode_submit([request])[1:] == [bytes([1, 0, 0, 0, 2, 1, 0, 0])]
    done = frames.Completion(1, [258], "eos", logprobs=[-0.5])
    assert frames.encode_completions([done])[1:] == [le32(258), struct.pack("<d", -0.5)]
    step = [(1, RequestOutput(1, [258], False, logprobs=[(-0.5, 1, 258, -0.75)]))]
    floats, ints = struct.pack("<2d", -0.5, -0.75), le32(1, 258)
    assert frames.encode_stream(step)[1:] == [floats, ints]


@pytest.mark.parametrize("version", [0, VERSION + 1])
def test_a_message_of_another_protocol_version_is_refused(version):
    spec = pb.RequestSpec(seq_id=1, prompt_token_ids=ref(1, 4))
    with pytest.raises(frames.FrameError, match="protocol version"):
        frames.decode_client(submit([spec], le32(5), version=version))
    ready = pb.Envelope(protocol_version=version, ready=pb.Ready(max_pool_tokens=8))
    with pytest.raises(frames.FrameError, match="protocol version"):
        frames.decode_server([ready.SerializeToString()])


def test_a_request_the_session_cannot_read_is_rejected_alone():
    unreadable = {
        2: {"prompt_token_ids": ref(1, 8, offset=4)},  # overruns its frame
        3: {"prompt_token_ids": ref(3, 4)},  # a frame the message lacks
        4: {"prompt_token_ids": ref(0, 4)},  # frame 0 is the Envelope
        5: {"prompt_token_ids": ref(1, 6)},  # not whole int32 values
        6: {"prompt_token_ids": ref(2, 4)},  # a negative id
        7: {"multimodal_data": pb.Value(null_value=True)},
        8: {"sibling_index": 1, "needs_independent_noise": True},
        9: {"kv_transfer_params": pb.Value(string_value="not a map")},
        10: {"kv_transfer_params": pb.Value(pickled_value=ref(1, 8))},  # never loaded
        11: {"kv_transfer_params": pb.Value(tuple_value=pb.ValueList())},
        12: {"sampling": pb.SamplingParams(top_p=2.0)},  # SamplingParams refuses it
    }
    specs = [pb.RequestSpec(seq_id=1, prompt_token_ids=ref(1, 8))] + [
        pb.RequestSpec(**{"seq_id": n, "prompt_token_ids": ref(1, 8), **fields})
        for n, fields in unreadable.items()
    ]
    _, items = frames.decode_client(submit(specs, le32(7, 8), le32(-1)))
    assert items[0] == frames.SubmitRequest(1, [7, 8], DEFAULTS)
    rejected = [(type(item), item.seq_id, item.code) for item in items[1:]]
    assert rejected == [(frames.Rejection, n, frames.INVALID) for n in unreadable]
    # A message that is no Session request is refused whole.
    for body in ({"shutdown": pb.Shutdown()}, {"ready": pb.Ready()}, {}):
        envelope = pb.Envelope(protocol_version=VERSION, **body)
        with pytest.raises(frames.FrameError):
            frames.decode_client([envelope.SerializeToString()])
    with pytest.raises(frames.FrameError):
        frames.decode_client([])
    with pytest.raises(frames.FrameError):
        frames.decode_client([b"\xff\xff"])
    with pytest.raises(frames.FrameError):
        frames.decode_server(submit([]))


def test_kv_transfer_params_travel_as_their_json():
    sent = {"ids": (1, 2), "nested": [{"a": None, "b": False}], "f": 1.0, "i": 1}
    step = [(1, RequestOutput(1, [5], True, "eos", sent))]
    [(_, output)] = frames.decode_server(frames.encode_stream(step))[1]
    # json.dumps tells 1 from 1.0 and from false, as == does not.
    assert json.dumps(output.kv_transfer_params_output) == json.dumps(sent)
    for unsent in ({"x": b"\0"}, {1: "x"}, {"x": {1, 2}}):
        done = frames.Completion(1, [], "eos", kv_transfer_params_output=unsent)
        with pytest.raises(TypeError, match="not JSON-shaped"):
            frames.encode_completions([done])


# ------------------------------------------------- parity with /v1/completions --

SAMPLING_FIELDS = ("temperature", "top_k", "top_p", "max_tokens", "ignore_eos", "n")
BODIES = [
    {"prompt_token_ids": [5, 6, 7]},  # every default, max_tokens included
    {
        "prompt_token_ids": [8, 9],
        "max_tokens": 77,
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
        "ignore_eos": True,
        "stop": ["Hello world", "zu"],
        "kv_transfer_params": KV,
    },
    {"prompt_token_ids": [10], "max_tokens": 5, "temperature": 0, "top_k": 0, "n": 0},
]


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


@pytest.fixture
def completions_route(monkeypatch):
    """/v1/completions with the engine call stubbed: returns what the route hands it."""
    handed = []

    async def generate_async(prompt, sampling_params, _request_id, **kwargs):
        handed.append((prompt, sampling_params, kwargs["kv_transfer_params"]))
        yield {
            "text": "",
            "finish_reason": "eos",
            "num_tokens_input": len(prompt),
            "num_tokens_output": 0,
        }

    monkeypatch.setattr(api_server, "engine", None)
    monkeypatch.setattr(api_server, "model_name", "m")
    monkeypatch.setattr(api_server, "_request_logger", None)
    monkeypatch.setattr(api_server, "generate_async", generate_async)

    def call(body):
        request = CompletionRequest.model_validate_json(json.dumps(body))
        asyncio.run(api_server.completions(request, None))
        return handed.pop()

    return call


def json_value(obj) -> pb.Value:
    """`obj` as a client turning JSON into engine_v1 writes it."""
    kinds = {
        bool: "bool_value",
        int: "int_value",
        float: "float_value",
        str: "string_value",
    }
    if obj is None:
        return pb.Value(null_value=True)
    if type(obj) in kinds:
        return pb.Value(**{kinds[type(obj)]: obj})
    if isinstance(obj, list):
        return pb.Value(list_value=pb.ValueList(items=[json_value(i) for i in obj]))
    entries = [
        pb.MapEntry(key=pb.Value(string_value=k), value=json_value(v))
        for k, v in obj.items()
    ]
    return pb.Value(map_value=pb.ValueMap(entries=entries))


def client_submit(body: dict, tokenizer) -> list[bytes]:
    """The SubmitBatch a gRPC client sends for `body`: only the fields the body sets,
    stop strings with their ids, and each buffer in a frame of its own."""
    stops = body.get("stop", [])
    sampling = pb.SamplingParams(
        **{k: v for k, v in body.items() if k in SAMPLING_FIELDS}
    )
    if stops:
        sampling.stop_strings.many.items.extend(stops)
    spec = pb.RequestSpec(
        seq_id=7,
        prompt_token_ids=ref(1, 4 * len(body["prompt_token_ids"])),
        sampling=sampling,
        stop_token_sequences=[
            pb.TokenIds(ids=tokenizer.encode(s, add_special_tokens=False))
            for s in stops
        ],
    )
    if "kv_transfer_params" in body:
        spec.kv_transfer_params.CopyFrom(json_value(body["kv_transfer_params"]))
    return submit([spec], le32(*body["prompt_token_ids"]))


@pytest.mark.parametrize("body", BODIES)
def test_a_decoded_submit_gives_what_the_http_route_builds(
    body, completions_route, tokenizer
):
    prompt, sampling_params, kv_transfer_params = completions_route(body)
    # The HTTP path's stop ids: preprocess tokenizes the route's stop strings.
    config = SimpleNamespace(
        hf_config=SimpleNamespace(model_type="deepseek_v3"), speculative_config=None
    )
    seq = InputOutputProcessor(config, tokenizer, 16).preprocess(
        prompt, sampling_params, kv_transfer_params=kv_transfer_params
    )
    _, [request] = frames.decode_client(client_submit(body, tokenizer))
    assert request.prompt_token_ids == prompt
    assert request.sampling_params == sampling_params
    assert request.stop_token_sequences == [list(s) for s in seq.stop_token_sequences]
    assert request.kv_transfer_params == kv_transfer_params
