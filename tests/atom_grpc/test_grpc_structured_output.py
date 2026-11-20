# SPDX-License-Identifier: MIT
"""A chat's StructuredOutput over a real channel: Generate is admitted, and
PrepareStructuredOutput answers, with the structured fields the HTTP chat admits."""

import json
import uuid
from dataclasses import asdict
from pathlib import Path

import grpc
import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from atom.sampling_params import RequiredChannel
from tests.atom_grpc.fake.record import RECORD, EngineRecord

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
STRUCTURED = (
    "required_channel",
    "json_schema",
    "structured_tool_format",
    "structured_reasoning_end",
)
SAMPLING = {"max_tokens": 8, "temperature": 0}
STOP = "你好"  # a fixture word
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "answer", "schema": SCHEMA},
}


def tool(name: str) -> dict:
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}}
    return {"type": "function", "function": {"name": name, "parameters": parameters}}


def user(content: str) -> dict:
    return {"role": "user", "content": content}


# Each case: its instance, the chat, and what the HTTP chat admits it with, in brief.
CASES = {
    "kimi-k3-required-channel": (
        "k3",
        {  # its one tool declared by a message, as Kimi-K3's dynamic tools are
            "messages": [
                {"role": "system", "content": "ba", "tools": [tool("get_weather")]},
                user("be bi"),
            ],
            "tool_choice": "required",
        },
        {"channel": True, "schema": None, "tools": None, "reasoning_end": ""},
    ),
    "dsv41-structured-tool-format": (
        "v41",
        {  # the format names the top-level tools, not those a message declares
            "messages": [
                {"role": "system", "content": "bo", "tools": [tool("lookup")]},
                user("bu ca"),
            ],
            "tools": [tool("get_weather")],
            "response_format": RESPONSE_FORMAT,
        },
        {
            "channel": False,
            "schema": SCHEMA,
            "tools": ["get_weather"],
            "reasoning_end": "</think>",
        },
    ),
    "unconstrained": (
        "v41",  # DSML holds tool_choice required to no channel
        {
            "messages": [user("ce ci")],
            "tools": [tool("get_weather")],
            "tool_choice": "required",
        },
        {"channel": False, "schema": None, "tools": None, "reasoning_end": ""},
    ),
}


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


def model_info(instance) -> pb.GetModelInfoResponse:
    with grpc.insecure_channel(instance.grpc) as channel:
        return pb_grpc.AtomEngineStub(channel).GetModelInfo(pb.GetModelInfoRequest())


def structured_output(chat: dict, info, tokenizer) -> pb.StructuredOutput:
    """What atomesh sends for `chat`: its fields as JSON, and whether its prompt,
    rendered with GetModelInfo's template, ends in a marker GetModelInfo lists."""
    prompt = tokenizer.apply_chat_template(
        chat["messages"],
        tools=chat.get("tools"),
        chat_template=info.chat_template,
        tokenize=False,
        add_generation_prompt=True,
    )
    carried = pb.StructuredOutput(
        chat_template_kwargs_json=info.default_chat_template_kwargs_json,
        prompt_opens_reasoning=prompt.rstrip().endswith(
            tuple(info.reasoning_open_markers)
        ),
    )
    for name in ("tools", "tool_choice", "response_format"):
        if name in chat:
            setattr(carried, f"{name}_json", json.dumps(chat[name]))
    message_tools = [message.get("tools") for message in chat["messages"]]
    if any(tools is not None for tools in message_tools):
        carried.message_tools_json = json.dumps(message_tools)
    return carried


def generate(instance, prompt, carried, stops=()) -> list[pb.GenerateResponse]:
    request = pb.GenerateRequest(
        request_id=f"structured-{uuid.uuid4().hex}",
        prompt_token_ids=prompt,
        sampling=v1.SamplingParams(**SAMPLING),
        stop_token_sequences=[v1.TokenIds(ids=ids) for ids in stops],
        structured_output=carried,
    )
    with grpc.insecure_channel(instance.grpc) as channel:
        return list(pb_grpc.AtomEngineStub(channel).Generate(request, timeout=600))


def prepare(instance, carried, stops=()) -> pb.PrepareStructuredOutputResponse:
    request = pb.PrepareStructuredOutputRequest(
        structured_output=carried,
        sampling=v1.SamplingParams(**SAMPLING),
        stop_token_sequences=[v1.TokenIds(ids=ids) for ids in stops],
    )
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        return stub.PrepareStructuredOutput(request, timeout=600)


def admitted_as(sampling: v1.SamplingParams, eos_ids) -> dict:
    """PrepareStructuredOutput's fields as an admission records them: the engine holds
    a required channel back from its EOS ids too, whichever path admits it."""
    channel = None
    if sampling.HasField("required_channel"):
        c = sampling.required_channel
        held = RequiredChannel(
            tuple(c.after), tuple(c.opening), c.opens_at, tuple(c.end_ids)
        ).ending_on(eos_ids)
        channel = json.loads(json.dumps(asdict(held)))
    optional = ("json_schema", "structured_tool_format")
    return {
        "required_channel": channel,
        **{name: getattr(sampling, name) or None for name in optional},
        "structured_reasoning_end": sampling.structured_reasoning_end,
    }


def outcome(fields: dict) -> dict:
    tool_format = fields["structured_tool_format"]
    return {
        "channel": fields["required_channel"] is not None,
        "schema": fields["json_schema"] and json.loads(fields["json_schema"]),
        "tools": tool_format and [t["name"] for t in json.loads(tool_format)["tools"]],
        "reasoning_end": fields["structured_reasoning_end"],
    }


@pytest.mark.topology("structured")
@pytest.mark.parametrize("case", CASES)
def test_generate_and_prepare_give_the_structured_fields_the_http_chat_admits(
    topology, tokenizer, case
):
    name, chat, expected = CASES[case]
    instance = topology.instances[name]
    info = model_info(instance)
    body = chat | SAMPLING | {"return_token_ids": True}
    status, reply = instance.post("/v1/chat/completions", body)
    assert status == 200, reply
    prompt, path = reply["prompt_token_ids"], instance.workdir / RECORD
    calls = len(EngineRecord(path).tokenizer)
    carried = structured_output(chat, info, tokenizer)
    (complete,) = generate(instance, prompt, carried)
    prepared = prepare(instance, carried)
    record = EngineRecord(path)
    assert record.tokenizer[calls:] == []  # neither call encodes or decodes

    over_http, over_grpc = record.with_prompt(prompt)
    admitted = {field: over_http.sampling[field] for field in STRUCTURED}
    assert outcome(admitted) == expected
    assert complete.WhichOneof("response") == "complete"
    assert {field: over_grpc.sampling[field] for field in STRUCTURED} == admitted
    assert prepared.WhichOneof("response") == "sampling"
    assert admitted_as(prepared.sampling, info.eos_token_ids) == admitted
    if expected["channel"]:  # as the chat derives it: the engine adds its EOS ids
        ends = admitted["required_channel"]["end_ids"]
        assert list(prepared.sampling.required_channel.end_ids) != ends


@pytest.mark.topology("structured")
def test_a_stop_id_sequence_counts_as_a_stop_string_against_a_response_format(
    topology, tokenizer
):
    instance = topology.instances["v41"]
    info = model_info(instance)
    chat = {"messages": [user("da de")], "response_format": RESPONSE_FORMAT}
    body = chat | SAMPLING | {"stop": [STOP]}
    status, body = instance.post("/v1/chat/completions", body)
    refused = (status, json.loads(body)["error"]["message"])
    assert refused[0] == 400 and "custom stop strings" in refused[1]

    carried = structured_output(chat, info, tokenizer)
    stops = [tokenizer.encode(STOP, add_special_tokens=False)]
    prompt = tokenizer.apply_chat_template(
        chat["messages"],
        chat_template=info.chat_template,
        add_generation_prompt=True,
        return_dict=False,
    )
    (message,) = generate(instance, prompt, carried, stops)
    answer = prepare(instance, carried, stops)
    assert (message.error.http_status, message.error.message) == refused
    assert (answer.error.http_status, answer.error.message) == refused
