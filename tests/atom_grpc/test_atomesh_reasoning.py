# SPDX-License-Identifier: MIT
"""A chat whose output ends inside the reasoning answers through atomesh with the text
fields ATOM's HTTP chat answers it with, on stops.toml's "think" instance."""

import pytest

from tests.atom_grpc.test_atomesh_stops import events, model

FIELDS = ("reasoning_content", "content")


def fields(target, body: dict) -> dict:
    """The text fields `target`'s answer to `body` carries, a stream's deltas joined; a
    field that no message or delta carries is absent."""
    found = {}
    for event in events(target, "chat", body):
        for choice in event["choices"]:
            part = choice.get("message") or choice["delta"]
            for field in FIELDS:
                if part.get(field) is not None:
                    found[field] = found.get(field, "") + part[field]
    return found


@pytest.mark.topology("stops")
@pytest.mark.parametrize("stream", [False, True], ids=["unary", "stream"])
def test_a_chat_ending_inside_the_reasoning_carries_the_content_atom_http_chat_does(
    topology, stream
):
    instance, router = topology.instances["think"], topology.routers["think-mesh"]
    chat = [{"role": "user", "content": "ba be bi"}]
    body = {"model": model(router), "messages": chat, "max_tokens": 4, "stream": stream}
    over_http = fields(instance, body)
    assert over_http["content"] == "" and over_http["reasoning_content"]
    assert fields(router, body) == over_http
