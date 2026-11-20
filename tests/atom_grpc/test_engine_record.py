# SPDX-License-Identifier: MIT
"""The engine record of a fake instance: what ATOM's admission handed its engine and
what each step emitted, read back from its workdir; recording changes no id."""

import json
import time
from dataclasses import fields
from pathlib import Path

import pytest
from transformers import AutoTokenizer

from atom.sampling_params import SamplingParams
from tests.atom_grpc.fake.instance import FakeInstance
from tests.atom_grpc.fake.record import RECORD, EngineRecord

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
ARGS = ["-tp", "2", "--enable-dp-attention", "--method", "mtp"]
ARGS += ["--num-speculative-tokens", "3"]
STARTUP_S = 1500  # one deadline for both instances; a loaded host takes minutes each
STOP = "你好"  # a fixture word that no law id is, so it ends no request
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


@pytest.fixture(scope="module")
def instances(tmp_path_factory):
    """One DP 2 MTP instance writing the engine record, and the same one not."""
    fake = {"acceptance_length": 3.51, "step_s": 0.001}
    started = {
        name: FakeInstance(
            tmp_path_factory.mktemp(name), FIXTURE, ARGS, fake, record=record
        )
        for name, record in (("recorded", True), ("unrecorded", False))
    }
    try:
        for instance in started.values():
            instance.start()
        deadline = time.monotonic() + STARTUP_S
        for instance in started.values():
            instance.wait_ready(deadline)
        yield started
    finally:
        for instance in started.values():
            instance.stop()


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def completion(prompt: list[int], rank: int, max_tokens: int) -> dict:
    return {
        "prompt_token_ids": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stop": [STOP],
        "data_parallel_rank": rank,
    }


def test_a_completion_leaves_its_admission_steps_and_stop_encode_in_the_record(
    instances, tokenizer
):
    recorded = instances["recorded"]
    calls_before = len(EngineRecord(recorded.record).tokenizer)
    prompt = [1, *range(8, 30)]
    body = completion(prompt, 1, 20) | {"kv_transfer_params": {"transfer_id": "t31"}}
    sent = time.time()
    status, reply = recorded.post("/v1/completions", body)
    answered = time.time()
    assert status == 200, reply
    record = EngineRecord(recorded.record)
    (request,) = record.with_prompt(prompt)

    assert request.prompt_ids == prompt
    assert sent < request.time < answered
    engine_fields = {f.name for f in fields(SamplingParams)}
    assert set(request.sampling) == engine_fields - {"n", "max_tokens_is_default"}
    given = {"temperature": 0, "max_tokens": 20, "stop_strings": [STOP]}
    assert {name: request.sampling[name] for name in given} == given
    assert request.sampling["json_schema"] is None
    assert request.stop_token_sequences == [
        tokenizer.encode(STOP, add_special_tokens=False)
    ]
    assert request.kv_transfer_params == {"transfer_id": "t31"}
    assert request.data_parallel_rank == 1

    times = [step.time for step in request.steps]
    assert request.time < times[0] and times == sorted(times)
    assert {step.dp_rank for step in request.steps} == {1}
    assert max(len(step.ids) for step in request.steps) > 1  # MTP verifies
    choice = reply["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["text"] == tokenizer.decode(request.output_ids[:20])

    calls = record.tokenizer[calls_before:]
    assert [call["inputs"] for call in calls if call["kind"] == "encode"] == [[STOP]]


@pytest.mark.parametrize("rank", [0, 1])
def test_recording_changes_no_id_the_engine_emits(instances, rank):
    prompt = [1, 40 + rank, *range(50, 70)]
    body = completion(prompt, rank, 60)
    replies = [
        instance.post("/v1/completions", body) for instance in instances.values()
    ]
    (recorded_status, recorded), (status, unrecorded) = replies
    assert recorded_status == status == 200, replies
    assert recorded["choices"] == unrecorded["choices"]  # equal text is equal ids
    assert not (instances["unrecorded"].workdir / RECORD).exists()


def test_a_chat_with_a_response_format_schema_is_admitted(instances):
    body = {
        "messages": [{"role": "user", "content": "ba be bi"}],
        "max_tokens": 12,
        "temperature": 0,
        "return_token_ids": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": SCHEMA},
        },
    }
    replies = {}
    for name, instance in instances.items():
        status, replies[name] = instance.post("/v1/chat/completions", body)
        assert status == 200, replies[name]
    assert replies["recorded"]["choices"] == replies["unrecorded"]["choices"]
    record = EngineRecord(instances["recorded"].record)
    (request,) = record.with_prompt(replies["recorded"]["prompt_token_ids"])
    assert json.loads(request.sampling["json_schema"]) == SCHEMA
