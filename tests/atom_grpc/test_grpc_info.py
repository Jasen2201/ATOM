# SPDX-License-Identifier: MIT
"""A1.7 and A1.8: over a real channel, a fake instance's info RPCs answer as its HTTP
routes do, and an instance started without --grpc-port has no gRPC listener."""

import asyncio
import contextlib
import json
import socket
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import grpc
import psutil
import pytest
from fastapi.testclient import TestClient
from transformers import AutoTokenizer

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.entrypoints.grpc.server import (
    THREAD_NAME,
    AtomEngineServicer,
    start_grpc_server,
)
from atom.entrypoints.openai import api_server
from atom.entrypoints.openai.reasoning import _REASONING_OPEN_MARKERS
from tests.atom_grpc.harness.topologies import Topology

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "deepseek_v3_mtp"
ENGINES = {  # what /health reads of the engine
    "absent": None,
    "failed": SimpleNamespace(core_mgr=SimpleNamespace(is_healthy=lambda: False)),
    "healthy": SimpleNamespace(core_mgr=SimpleNamespace(is_healthy=lambda: True)),
}


def call(target: str, method: str, request, timeout: float = 60):
    """One unary AtomEngine call over a fresh channel to `target`."""
    with grpc.insecure_channel(target) as channel:
        return getattr(pb_grpc.AtomEngineStub(channel), method)(
            request, timeout=timeout
        )


async def health_check(api) -> pb.HealthCheckResponse:
    """HealthCheck over a real channel to an in-process server answering from `api`."""
    server = grpc.aio.server()
    pb_grpc.add_AtomEngineServicer_to_server(AtomEngineServicer(api, "unused"), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.AtomEngineStub(channel)
            return await stub.HealthCheck(pb.HealthCheckRequest(), timeout=60)
    finally:
        await server.stop(None)


def started_with(pid: int, flag: str) -> str:
    """The value process `pid` was given for `flag` on its command line."""
    argv = psutil.Process(pid).cmdline()
    return argv[argv.index(flag) + 1]


def answering_ports(pid: int) -> set[int]:
    """The TCP ports process `pid` listens on where AtomEngine's HealthCheck answers."""
    connections = psutil.Process(pid).net_connections("tcp")
    answering = set()
    for port in {c.laddr.port for c in connections if c.status == psutil.CONN_LISTEN}:
        with contextlib.suppress(grpc.RpcError):  # not a gRPC server, or no AtomEngine
            call(f"127.0.0.1:{port}", "HealthCheck", pb.HealthCheckRequest(), 10)
            answering.add(port)
    return answering


def thread_names(pid: int) -> set[str]:
    """The names /proc gives the threads of process `pid`."""
    return {
        comm.read_text().strip() for comm in Path(f"/proc/{pid}/task").glob("*/comm")
    }


def port_of(address: str) -> int:
    return int(address.rpartition(":")[2])


@pytest.mark.topology("single")
def test_get_model_info_answers_what_the_instance_serves(topology):
    (instance,) = topology.instances.values()
    info = call(instance.grpc, "GetModelInfo", pb.GetModelInfoRequest())
    model = started_with(instance.pid, "--model")
    assert (info.model_path, info.tokenizer_path) == (model, model)
    status, models = instance.get("/v1/models")
    assert status == 200, models
    (card,) = models["data"]
    served = started_with(instance.pid, "--served-model-name")
    assert served != model
    assert info.served_model_name == card["id"] == served
    assert info.max_context_length == card["context_window"]
    generation = json.loads((Path(model) / "generation_config.json").read_text())
    assert list(info.eos_token_ids) == [generation["eos_token_id"]]
    assert info.HasField("chat_template")
    assert info.chat_template == AutoTokenizer.from_pretrained(model).chat_template
    defaults = json.loads(started_with(instance.pid, "--default-chat-template-kwargs"))
    assert defaults
    assert json.loads(info.default_chat_template_kwargs_json) == defaults
    assert _REASONING_OPEN_MARKERS
    assert list(info.reasoning_open_markers) == list(_REASONING_OPEN_MARKERS)


@pytest.mark.topology("single")
def test_get_server_info_carries_the_kv_transfer_info_and_server_info_routes(topology):
    (instance,) = topology.instances.values()
    info = call(instance.grpc, "GetServerInfo", pb.GetServerInfoRequest())
    assert info.protocol_version == pb.PROTOCOL_VERSION_CURRENT
    kv_transfer_info = json.loads(info.kv_transfer_info_json)
    assert instance.get("/kv_transfer_info") == (200, kv_transfer_info)
    assert instance.get("/server_info") == (200, json.loads(info.server_info_json))


@pytest.mark.topology("single")
def test_health_check_serves_while_health_answers_200(topology):
    (instance,) = topology.instances.values()
    reply = call(instance.grpc, "HealthCheck", pb.HealthCheckRequest())
    assert (reply.healthy, reply.message) == (True, "")
    assert instance.get("/health") == (200, {"status": "ok"})


@pytest.mark.topology("single")
def test_only_grpc_port_answers_grpc_served_by_a_named_thread_of_the_api_server(
    topology,
):
    (instance,) = topology.instances.values()
    grpc_port = int(started_with(instance.pid, "--grpc-port"))
    assert port_of(instance.grpc) == grpc_port
    assert answering_ports(instance.pid) == {grpc_port}
    assert THREAD_NAME in thread_names(instance.pid)


@pytest.mark.parametrize("hub", [None, [], [2]], ids=["no-hub", "hub", "dead-worker"])
@pytest.mark.parametrize("engine", ENGINES)
def test_health_check_is_not_serving_exactly_when_health_answers_503(
    monkeypatch, engine, hub
):
    monkeypatch.setattr(api_server, "engine", ENGINES[engine])
    workers = None if hub is None else SimpleNamespace(dead_workers=lambda: hub)
    monkeypatch.setattr(api_server, "_frontend_hub", workers)
    http = TestClient(api_server.app).get("/health")  # no lifespan: no model loads
    reply = asyncio.run(health_check(api_server))
    assert reply.healthy == (http.status_code == 200)
    assert reply.message == ("" if reply.healthy else http.json()["detail"])


def test_a_taken_grpc_port_fails_the_start_with_its_bind_error():
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        args = Namespace(host="127.0.0.1", grpc_port=port, model="unused")
        with pytest.raises(RuntimeError, match="Failed to bind"):
            start_grpc_server(api_server, args)


def test_grpc_port_is_refused_with_more_than_one_api_server(monkeypatch, capsys):
    def past_the_checks(args):
        raise AssertionError("main() went past its argument checks")

    monkeypatch.setattr(api_server, "_setup_frontend", past_the_checks)
    argv = ["api_server", "--grpc-port", "50051", "--api-server-count", "2"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as refused:
        api_server.main()
    assert refused.value.code == 2
    assert "--grpc-port needs --api-server-count 1" in capsys.readouterr().err


def test_without_grpc_port_no_grpc_listener_starts(tmp_path_factory, topologies):
    topologies.down()  # one topology up at a time
    scratch = tmp_path_factory.mktemp("http")  # short: ZMQ ipc sockets live in it
    path = scratch / "http.toml"
    path.write_text(
        f'[[instance]]\nname = "http"\nmodel = "{FIXTURE}"\nargs = []\nfake = {{}}\n'
    )
    launch = Topology(path, scratch)
    try:
        launch.wait_ready()
        (instance,) = launch.instances.values()
        assert instance.grpc is None
        assert "--grpc-port" not in psutil.Process(instance.pid).cmdline()
        assert answering_ports(instance.pid) == set()
        assert THREAD_NAME not in thread_names(instance.pid)
    finally:
        left = launch.stop()
    assert left == {}
