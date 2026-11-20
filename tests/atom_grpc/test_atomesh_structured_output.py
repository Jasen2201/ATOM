# SPDX-License-Identifier: MIT
"""A chat sent through atomesh to an ATOM gRPC worker is admitted with the structured
fields ATOM's HTTP chat admits the same chat with, on structured.toml's instances."""

import json

import pytest

from tests.atom_grpc.fake.record import RECORD, EngineRecord

STRUCTURED = (
    "required_channel",
    "json_schema",
    "structured_tool_format",
    "structured_reasoning_end",
)
SAMPLING = {"max_tokens": 8, "temperature": 0}
# Each transport's router suffix, and a word its chats end with: a prompt of their own.
TRANSPORTS = {"generate": ("mesh", ""), "session": ("session", " hu")}
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
        {
            "messages": [user("ga ge")],
            "tools": [tool("get_weather")],
            "tool_choice": "required",
        },
        {"channel": True, "schema": None, "tools": None, "reasoning_end": ""},
    ),
    "dsv41-structured-tool-format": (
        "v41",
        {  # the format names the top-level tools, not those a message declares, which
            # atomesh's chat protocol keeps on a developer message only
            "messages": [
                {"role": "developer", "content": "go", "tools": [tool("lookup")]},
                user("gu ha"),
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
            "messages": [user("he hi")],
            "tools": [tool("get_weather")],
            "tool_choice": "required",
        },
        {"channel": False, "schema": None, "tools": None, "reasoning_end": ""},
    ),
}


def outcome(fields: dict) -> dict:
    tool_format = fields["structured_tool_format"]
    schema = fields["json_schema"]
    return {
        "channel": fields["required_channel"] is not None,
        "schema": schema and json.loads(schema),
        "tools": tool_format and [t["name"] for t in json.loads(tool_format)["tools"]],
        "reasoning_end": fields["structured_reasoning_end"],
    }


@pytest.mark.topology("structured")
@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("case", CASES)
def test_atomesh_admits_a_chat_with_the_structured_fields_the_http_chat_admits(
    topology, case, transport
):
    name, chat, expected = CASES[case]
    suffix, word = TRANSPORTS[transport]
    *earlier, last = chat["messages"]
    chat = chat | {"messages": [*earlier, user(last["content"] + word)]}
    instance, router = topology.instances[name], topology.routers[f"{name}-{suffix}"]
    body, route = chat | SAMPLING, "/v1/chat/completions"
    status, reply = instance.post(route, body | {"return_token_ids": True})
    assert status == 200, reply
    prompt = reply["prompt_token_ids"]
    status, listing = router.get("/workers")
    assert status == 200, listing
    (worker,) = listing["workers"]
    status, reply = router.post(route, body | {"model": worker["model_id"]})
    assert status == 200, reply

    # atomesh renders the prompt the HTTP chat renders, so both admissions hold it.
    record = EngineRecord(instance.workdir / RECORD)
    over_http, over_atomesh = record.with_prompt(prompt)
    admitted = {field: over_http.sampling[field] for field in STRUCTURED}
    assert outcome(admitted) == expected
    assert {field: over_atomesh.sampling[field] for field in STRUCTURED} == admitted
