# SPDX-License-Identifier: MIT
"""The Session through atomesh: each engine step's message split per request, a router's
requests in one seq_id window, drops ending only their own, another version refused."""

import concurrent.futures
import http.client
import json
import os
import signal
import socket
import time
import urllib.parse
from pathlib import Path

import grpc
import pytest
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.entrypoints.grpc import frames
from atom.entrypoints.grpc.session import SEQ_IDS
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.atomesh import Atomesh, RouterSpec, atomesh_bin
from tests.atom_grpc.test_atomesh_single import (
    WAIT_S,
    idle,
    model_info,
    wait_for,
    words,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
TOKENS = 300  # dozens of steps, which concurrent requests share
KEPT_TOKENS = 3000  # steps that run on well past a drop
MAX_TOKENS = 50000  # far past where a dropped request stops


@pytest.fixture(scope="module")
def tokenizer():
    return AutoTokenizer.from_pretrained(FIXTURE)


class Generation:
    """A streaming `/generate` of `text` to `model` through `router`: `read` takes its
    chunks' ids into `chunks`, and `drop` is its client going away."""

    def __init__(self, router, model: str, text: str, max_new_tokens: int):
        address = urllib.parse.urlsplit(router.url)
        self.connection = http.client.HTTPConnection(
            address.hostname, address.port, timeout=600
        )
        sampling = {"max_new_tokens": max_new_tokens, "temperature": 0}
        body = {"model": model, "text": text, "sampling_params": sampling}
        body["stream"] = True
        headers = {"Content-Type": "application/json"}
        self.connection.request("POST", "/generate", json.dumps(body), headers)
        self.response = self.connection.getresponse()
        assert self.response.status == 200, self.response.read()
        self.chunks: list[list[int]] = []

    def read(self, count: int | None = None) -> "Generation":
        """Until `count` chunks are in, or to the stream's end."""
        while count is None or len(self.chunks) < count:
            line = self.response.readline()
            if not line or line.strip() == b"data: [DONE]":
                return self
            if line.startswith(b"data: "):
                event = json.loads(line[6:])
                assert "error" not in event, event
                if event["meta_info"]["finish_reason"] is None:  # not its last event
                    self.chunks.append(event["output_ids"])
        return self

    def drop(self) -> None:
        self.connection.sock.shutdown(socket.SHUT_RDWR)
        self.connection.close()


def admitted(instance, tokenizer, text: str):
    """The one request the instance admitted with `text`'s ids."""
    record = EngineRecord(instance.workdir / RECORD)
    (request,) = record.with_prompt(tokenizer.encode(text, add_special_tokens=False))
    return request


def window(request) -> int:
    """The seq_id window of the request's Session; 0 holds the API process's ids."""
    return request.seq_id // SEQ_IDS


@pytest.mark.topology("single")
def test_atomesh_splits_each_engine_step_of_its_session_into_each_requests_chunk(
    topology, tokenizer
):
    router, instance = topology.routers["mesh-session"], topology.instances["single"]
    model = model_info(instance).served_model_name
    texts = [words(16, lead) for lead in (20, 22, 24, 26)]
    with concurrent.futures.ThreadPoolExecutor(len(texts)) as pool:
        runs = list(
            pool.map(lambda text: Generation(router, model, text, TOKENS).read(), texts)
        )

    requests = [admitted(instance, tokenizer, text) for text in texts]
    for run, request in zip(runs, requests):
        chunks, steps = run.chunks, [step.ids for step in request.steps]
        assert chunks[:-1] == steps[: len(chunks) - 1]  # one engine step, one chunk
        assert steps[len(chunks) - 1][: len(chunks[-1])] == chunks[-1]  # cut at TOKENS
        assert sum(map(len, chunks)) == TOKENS
    # steps that carried every request, each sent as one message and split by atomesh
    assert set.intersection(*({step.time for step in r.steps} for r in requests))
    assert len({window(request) for request in requests}) == 1
    assert window(requests[0]) > 0


@pytest.mark.topology("single")
def test_a_dropped_client_aborts_its_request_alone_on_the_session_it_shares(
    topology, tokenizer
):
    router, instance = topology.routers["mesh-session"], topology.instances["single"]
    model = model_info(instance).served_model_name
    texts = {"dropped": words(16, 30), "kept": words(16, 32)}
    wait_for(lambda: idle(instance, router), "idle instance and router")
    # Each has run a step, so no abort overtakes its admission (#92).
    dropped = Generation(router, model, texts["dropped"], MAX_TOKENS).read(1)
    kept = Generation(router, model, texts["kept"], KEPT_TOKENS).read(1)
    dropped.drop()
    dropped_at = time.time()
    kept.read()
    wait_for(lambda: idle(instance, router), "every block and load back")

    gone, ran = (admitted(instance, tokenizer, texts[k]) for k in ("dropped", "kept"))
    assert gone.aborted is not None and len(gone.output_ids) < MAX_TOKENS
    assert ran.aborted is None and sum(map(len, kept.chunks)) == KEPT_TOKENS
    assert max(step.time for step in ran.steps) > dropped_at
    assert window(gone) == window(ran)  # one Session


@pytest.mark.topology("single")
def test_two_routers_sessions_take_disjoint_seq_ids_and_a_dropped_one_ends_its_own(
    topology, tokenizer, tmp_path
):
    instance = topology.instances["single"]
    kept_router = topology.routers["mesh-session"]
    model = model_info(instance).served_model_name
    spec = RouterSpec("dropped", ["single"], args=["--atom-grpc-session"])
    urls, ranks = {"single": f"grpc://{instance.grpc}"}, {"single": 1}
    dropped_router = Atomesh(tmp_path, spec, urls, ranks, atomesh_bin())
    texts = {
        "dropped": [words(16, 40), words(16, 42)],
        "kept": [words(16, 44), words(16, 46)],
    }
    dropped_router.start()
    try:
        dropped_router.wait_ready(time.monotonic() + WAIT_S)
        wait_for(lambda: idle(instance, kept_router), "idle instance and router")
        runs = {  # each has run a step: running at the engine
            "dropped": [
                Generation(dropped_router, model, text, MAX_TOKENS).read(1)
                for text in texts["dropped"]
            ],
            "kept": [
                Generation(kept_router, model, text, KEPT_TOKENS).read(1)
                for text in texts["kept"]
            ],
        }
        os.killpg(dropped_router.proc.pid, signal.SIGKILL)  # its Session ends with it
        dropped_at = time.time()
        for run in runs["kept"]:
            run.read()
    finally:
        dropped_router.stop()
    wait_for(lambda: idle(instance, kept_router), "every block back")

    requests = {
        name: [admitted(instance, tokenizer, text) for text in group]
        for name, group in texts.items()
    }
    windows = {name: {window(r) for r in group} for name, group in requests.items()}
    assert len(windows["dropped"]) == len(windows["kept"]) == 1
    assert windows["dropped"] != windows["kept"]
    for request in requests["dropped"]:
        assert request.aborted is not None and len(request.output_ids) < MAX_TOKENS
    for run, request in zip(runs["kept"], requests["kept"]):
        assert request.aborted is None and sum(map(len, run.chunks)) == KEPT_TOKENS
        assert max(step.time for step in request.steps) > dropped_at


class OtherVersion(pb_grpc.AtomEngineServicer):
    """An instance's own answers, but each Session's Ready in the next protocol version;
    `sessions` counts the Session calls."""

    def __init__(self, model: pb.GetModelInfoResponse, info: pb.GetServerInfoResponse):
        self.model, self.info, self.sessions = model, info, 0

    def HealthCheck(self, request, context):
        return pb.HealthCheckResponse(healthy=True)

    def GetModelInfo(self, request, context):
        return self.model

    def GetServerInfo(self, request, context):
        return self.info

    def Session(self, request_iterator, context):
        self.sessions += 1
        ready = frames.encode_ready(0)
        envelope = v1.Envelope.FromString(ready[0])
        envelope.protocol_version = frames.PROTOCOL_VERSION + 1
        yield pb.Frames(frames=[envelope.SerializeToString(), *ready[1:]])


@pytest.mark.topology("single")
def test_atomesh_refuses_a_session_whose_ready_has_another_protocol_version(
    topology, tmp_path
):
    instance = topology.instances["single"]
    with grpc.insecure_channel(instance.grpc) as channel:
        stub = pb_grpc.AtomEngineStub(channel)
        model = stub.GetModelInfo(pb.GetModelInfoRequest(), timeout=60)
        info = stub.GetServerInfo(pb.GetServerInfoRequest(), timeout=60)
    other = OtherVersion(model, info)
    server = grpc.server(concurrent.futures.ThreadPoolExecutor(4))
    pb_grpc.add_AtomEngineServicer_to_server(other, server)
    urls = {"other": f"grpc://127.0.0.1:{server.add_insecure_port('127.0.0.1:0')}"}
    spec = RouterSpec("other", ["other"], args=["--atom-grpc-session"])
    router = Atomesh(tmp_path, spec, urls, {"other": 1}, atomesh_bin())
    server.start()
    try:
        router.start()
        router.wait_ready(time.monotonic() + WAIT_S)
        body = {"model": model.served_model_name, "text": words(8, 50)}
        body["sampling_params"] = {"max_new_tokens": 4}
        replies = [router.post("/generate", body) for _ in range(2)]
    finally:
        router.stop()
        server.stop(None)

    version = frames.PROTOCOL_VERSION
    refused = f"protocol version {version + 1}, expected {version}"
    for status, reply in replies:
        assert status == 503 and refused in reply, reply
    assert other.sessions >= len(replies)  # each attempt refused at its Session's Ready
