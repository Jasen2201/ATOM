# SPDX-License-Identifier: MIT
"""Stop strings through atomesh to an ATOM gRPC worker end a request where ATOM's HTTP
routes end it, with the same text, on stops.toml's instances."""

import json
import urllib.request

import pytest

ROUTES = {"chat": "/v1/chat/completions", "completions": "/v1/completions"}
FIELDS = ("reasoning_content", "content", "text")
# Each chat case: its instance, behind the router "<instance>-mesh", and the field it fills.
CHATS = {
    "in-reasoning": ("think", "reasoning_content"),
    "in-content": ("plain", "content"),
}


def events(target, route: str, body: dict) -> list[dict]:
    """`target`'s replies to `body`: the response, or a stream's data events in order."""
    if not body["stream"]:
        status, reply = target.post(ROUTES[route], body)
        assert status == 200, reply
        return [reply]
    body = body | {"stream_options": {"include_usage": True}}
    call = urllib.request.Request(
        target.url + ROUTES[route],
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(call, timeout=600) as reply:
        lines = reply.read().decode().splitlines()
    data = [line[6:] for line in lines if line.startswith("data: ")]
    return [json.loads(event) for event in data if event != "[DONE]"]


def outcome(target, route: str, body: dict) -> dict:
    """The request as `target` answers `body`: each text field, a stream's deltas joined
    and an empty one None, the finish_reason and the completion tokens."""
    found, finish, usage = dict.fromkeys(FIELDS, ""), None, None
    for event in events(target, route, body):
        usage = event.get("usage") or usage
        for choice in event["choices"]:
            piece = choice.get("message") or choice.get("delta") or choice
            for field in FIELDS:
                found[field] += piece.get(field) or ""
            finish = choice["finish_reason"] or finish
    found = {field: text or None for field, text in found.items()}
    return found | {"finish": finish, "tokens": usage["completion_tokens"]}


def model(router) -> str:
    """The model id `router` lists its one worker under."""
    status, listing = router.get("/workers")
    assert status == 200, listing
    (worker,) = listing["workers"]
    return worker["model_id"]


def check_stops(instance, router, route: str, body: dict, field: str) -> None:
    """`body` stopped by a string its output writes across two words, so with ids other
    than its own encoding, and by a later word: atomesh answers as ATOM's HTTP route."""
    free = outcome(instance, route, body | {"stream": False})
    words = free[field].split()  # an id a word
    across, word = f"{words[4][-1]} {words[5][0]}", words[12]
    body = body | {"stop": [across, word]}
    over_http = outcome(instance, route, body)
    assert across in over_http[field]  # it ends nothing; the word ends it at its own id
    assert over_http[field] == " ".join(words[:13])
    assert (over_http["finish"], over_http["tokens"]) == ("stop", 13)
    assert outcome(router, route, body) == over_http


@pytest.mark.topology("stops")
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
@pytest.mark.parametrize("case", CHATS)
def test_a_stop_string_ends_a_chat_through_atomesh_where_atom_http_chat_ends_it(
    topology, case, stream
):
    name, field = CHATS[case]
    instance, router = topology.instances[name], topology.routers[f"{name}-mesh"]
    chat = [{"role": "user", "content": "ba be bi"}]
    body = {
        "model": model(router),
        "messages": chat,
        "max_tokens": 40,
        "stream": stream,
    }
    check_stops(instance, router, "chat", body, field)


@pytest.mark.topology("stops")
def test_a_stop_string_ends_a_completion_through_atomesh_where_atom_http_ends_it(
    topology,
):
    instance, router = topology.instances["plain"], topology.routers["plain-mesh"]
    body = {"model": model(router), "prompt": "ba be bi", "max_tokens": 40}
    check_stops(instance, router, "completions", body | {"stream": False}, "text")
