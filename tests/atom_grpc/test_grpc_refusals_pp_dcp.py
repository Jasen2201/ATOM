# SPDX-License-Identifier: MIT
"""Generate refuses a chunked receive to a D with PP 2, or with DCP on a per-request-state
model, with the status and message `/v1/completions` answers on the same D."""

import json
import uuid

import grpc
import pytest

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from tests.atom_grpc.fake.record import RECORD, EngineRecord

PROMPT = [1, 8, *range(9, 39)]  # past one block-table entry on both D's
CHUNKED = {"do_remote_prefill": True, "chunked_transfer": True, "remote_tp_size": 1}
REFUSALS = {  # each D's refusal; its TP is a multiple of P's, so only PP or DCP refuses
    "pp2": "Chunked P/D requires D PP=1 and D TP a multiple of positive P TP",
    "dcp2": "Chunked P/D does not send per-request state to a DCP decode",
}
WAIT_S = 120


def generate(target: str, stream: bool, **fields) -> list[pb.GenerateResponse]:
    """Every message of one Generate call for PROMPT, over a fresh channel."""
    request = pb.GenerateRequest(
        request_id=f"refusal-{uuid.uuid4().hex}",
        prompt_token_ids=PROMPT,
        sampling=v1.SamplingParams(max_tokens=8),
        stream=stream,
    )
    if "kv_transfer_params" in fields:
        request.kv_transfer_params_json = json.dumps(fields["kv_transfer_params"])
    with grpc.insecure_channel(target) as channel:
        return list(pb_grpc.AtomEngineStub(channel).Generate(request, timeout=WAIT_S))


def kinds(messages) -> list[str]:
    return [message.WhichOneof("response") for message in messages]


@pytest.mark.topology("refusals")
@pytest.mark.parametrize("name", REFUSALS)
def test_each_d_serves_a_plain_request_through_its_engine(topology, name):
    instance = topology.instances[name]
    messages = generate(instance.grpc, stream=False)
    assert kinds(messages) == ["complete"], messages
    complete = messages[0].complete
    assert (complete.finish_reason, len(complete.output_ids)) == ("max_tokens", 8)
    (request,) = EngineRecord(instance.workdir / RECORD).with_prompt(PROMPT)
    assert request.output_ids[:8] == list(complete.output_ids)


@pytest.mark.topology("refusals")
@pytest.mark.parametrize("name", REFUSALS)
def test_a_chunked_receive_answers_the_400_of_completions(topology, name):
    instance = topology.instances[name]
    body = {"prompt_token_ids": PROMPT, "max_tokens": 8, "kv_transfer_params": CHUNKED}
    status, reply = instance.post("/v1/completions", body)
    assert status == 400, reply
    answers = [(status, json.loads(reply)["error"]["message"])]
    for stream in (False, True):
        messages = generate(instance.grpc, stream, kv_transfer_params=CHUNKED)
        assert kinds(messages) == ["error"], messages
        answers.append((messages[0].error.http_status, messages[0].error.message))
    assert answers == [(400, REFUSALS[name])] * 3
