# SPDX-License-Identifier: MIT
"""What atomesh refuses for an ATOM gRPC worker before planning: media input, Responses
and Messages, none of which reaches ATOM; the same requests without media are served."""

import json
import urllib.error
import urllib.request

import pytest

from tests.atom_grpc.fake.record import RECORD, EngineRecord

CODE = "X-Mesh-Error-Code"  # the error code atomesh gives each refusal
CHAT, GENERATE = "/v1/chat/completions", "/generate"
PROMPT = "ba be bi"  # fixture words
URL = "https://example.com/a"
IMAGE = {"type": "image_url", "image_url": {"url": URL + ".png"}}
VIDEO = {"type": "video_url", "video_url": {"url": URL + ".mp4"}}
# atomesh's chat has no audio part, so its parse refuses one for every backend
AUDIO = {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}}


def chat(*parts: dict) -> dict:
    content = [{"type": "text", "text": PROMPT}, *parts]
    return {"max_tokens": 4, "messages": [{"role": "user", "content": content}]}


def generate(**media: str) -> dict:
    return {"text": PROMPT, "sampling_params": {"max_new_tokens": 4}, **media}


MEDIA = (400, "unsupported_media")
UNSERVED = (501, "unsupported_api")
MESSAGES = {"max_tokens": 4, "messages": [{"role": "user", "content": PROMPT}]}
REFUSALS = {  # each request but its model, and atomesh's status and error code for it
    "chat image": (CHAT, chat(IMAGE), MEDIA),
    "chat video": (CHAT, chat(VIDEO), MEDIA),
    "chat audio": (CHAT, chat(AUDIO), (400, "invalid_request")),
    "generate image": (GENERATE, generate(image_data=URL), MEDIA),
    "generate video": (GENERATE, generate(video_data=URL), MEDIA),
    "generate audio": (GENERATE, generate(audio_data=URL), MEDIA),
    "responses": ("/v1/responses", {"input": PROMPT}, UNSERVED),
    "messages": ("/v1/messages", MESSAGES, UNSERVED),
}
SERVED = {"chat": (CHAT, chat()), "generate": (GENERATE, generate())}


def atom_model(router) -> str:
    """The model of atomesh's one worker, which it found to be ATOM over gRPC."""
    status, listing = router.get("/workers")
    assert status == 200, listing
    (worker,) = listing["workers"]
    assert (worker["connection_mode"], worker["runtime_type"]) == ("gRPC", "atom")
    return worker["model_id"]


def send(router, path: str, body: dict) -> tuple[int, str | None]:
    """POST `body` to atomesh: its status and error code, None when it gives none."""
    request = urllib.request.Request(
        router.url + path,
        data=json.dumps({"model": atom_model(router)} | body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as reply:
            reply.read()
            return reply.status, reply.headers.get(CODE)
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get(CODE)


def admitted(instance) -> int:
    """How many requests the instance's engine has admitted."""
    return len(EngineRecord(instance.workdir / RECORD).requests)


@pytest.mark.topology("single")
@pytest.mark.parametrize("case", REFUSALS)
def test_atomesh_refuses_what_atom_grpc_workers_do_not_serve(topology, case):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    path, body, answer = REFUSALS[case]
    before = admitted(instance)
    assert send(router, path, body) == answer
    assert admitted(instance) == before


@pytest.mark.topology("single")
@pytest.mark.parametrize("route", SERVED)
def test_the_same_request_without_media_is_served(topology, route):
    router, instance = topology.routers["mesh"], topology.instances["single"]
    path, body = SERVED[route]
    before = admitted(instance)
    assert send(router, path, body) == (200, None)
    assert admitted(instance) == before + 1
