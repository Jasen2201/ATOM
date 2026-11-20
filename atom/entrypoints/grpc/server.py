# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""`atom.grpc.v1.AtomEngine` beside the HTTP server, in the process that owns the
engine: a `grpc.aio` server on its own asyncio loop, in one named daemon thread."""

import asyncio
import json
import threading
from argparse import Namespace
from concurrent.futures import Future
from types import ModuleType

import grpc
import setproctitle

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.entrypoints.openai.reasoning import _REASONING_OPEN_MARKERS

# Also its name in /proc, which threads it starts keep unless they name their own.
THREAD_NAME = "AtomGrpcServer"


class AtomEngineServicer(pb_grpc.AtomEngineServicer):
    """Answers from `api`, the API server module running the engine, as its routes
    do; every RPC not defined here answers UNIMPLEMENTED."""

    def __init__(self, api: ModuleType, model_path: str):
        self.api = api
        self.model_path = model_path  # --model, which the tokenizer loads from

    async def HealthCheck(self, request, context):
        problem = self.api.health_problem()
        return pb.HealthCheckResponse(healthy=problem is None, message=problem or "")

    async def GetModelInfo(self, request, context):
        api, config = self.api, self.api.engine.config
        return pb.GetModelInfoResponse(
            model_path=self.model_path,
            tokenizer_path=self.model_path,
            served_model_name=api.model_name,
            max_context_length=config.max_model_len,
            eos_token_ids=config.end_token_ids,
            chat_template=api.tokenizer.chat_template,
            default_chat_template_kwargs_json=json.dumps(
                api.default_chat_template_kwargs
            ),
            reasoning_open_markers=_REASONING_OPEN_MARKERS,
        )

    async def GetServerInfo(self, request, context):
        return pb.GetServerInfoResponse(
            protocol_version=pb.PROTOCOL_VERSION_CURRENT,
            kv_transfer_info_json=json.dumps(self.api.kv_transfer_info_body()),
            server_info_json=json.dumps(self.api.server_info_body()),
        )


def start_grpc_server(api: ModuleType, args: Namespace) -> None:
    """Serve AtomEngine on `--host`:`--grpc-port`; return once it listens, or raise
    why it cannot."""
    host = f"[{args.host}]" if ":" in args.host else args.host  # an IPv6 address
    address, servicer = f"{host}:{args.grpc_port}", AtomEngineServicer(api, args.model)
    listening = Future()
    threading.Thread(
        target=_run, args=(address, servicer, listening), name=THREAD_NAME, daemon=True
    ).start()
    listening.result()


def _run(address: str, servicer: AtomEngineServicer, listening: Future) -> None:
    setproctitle.setthreadtitle(THREAD_NAME)
    asyncio.run(_serve(address, servicer, listening))


async def _serve(address: str, servicer: AtomEngineServicer, listening: Future) -> None:
    try:
        server = grpc.aio.server()
        pb_grpc.add_AtomEngineServicer_to_server(servicer, server)
        server.add_insecure_port(address)  # raises when the port is taken
        await server.start()
    except Exception as error:  # noqa: BLE001 - start_grpc_server raises it
        listening.set_exception(error)
        return
    listening.set_result(None)
    await server.wait_for_termination()
