# SPDX-License-Identifier: MIT
# Copyright (C) 2026, Advanced Micro Devices, Inc. All rights reserved.
"""atom.grpc.v1's committed modules: protoc's output for atom_grpc.proto and
engine_v1.proto, and the AtomEngine contract the gRPC server and atomesh build on."""

import pytest

from atom.entrypoints.grpc import atom_grpc_pb2, generate
from atom.model_engine.zmq_protocol import engine_v1_pb2


def test_committed_modules_are_what_generate_writes():
    pytest.importorskip("grpc_tools", reason="generate.py runs grpcio-tools' protoc")
    for module, code in generate.generated().items():
        assert (generate.ROOT / module).read_bytes() == code, (
            f"{module} is not what protoc writes from the protos; "
            "run python -m atom.entrypoints.grpc.generate"
        )


def test_atom_engine_rpcs_and_their_streaming():
    service = atom_grpc_pb2.DESCRIPTOR.services_by_name["AtomEngine"]
    shapes = [(m.name, m.client_streaming, m.server_streaming) for m in service.methods]
    assert shapes == [
        ("Generate", False, True),
        ("Abort", False, False),
        ("GetModelInfo", False, False),
        ("GetServerInfo", False, False),
        ("HealthCheck", False, False),
        ("Session", True, True),
    ]


def test_generate_request_reuses_engine_v1_messages():
    # One engine_v1 descriptor, imported by its repository path: no second copy.
    assert list(atom_grpc_pb2.DESCRIPTOR.dependencies) == [engine_v1_pb2.DESCRIPTOR]
    assert engine_v1_pb2.DESCRIPTOR.name == (
        "atom/model_engine/zmq_protocol/engine_v1.proto"
    )
    fields = atom_grpc_pb2.GenerateRequest.DESCRIPTOR.fields_by_name
    assert fields["sampling"].message_type is engine_v1_pb2.SamplingParams.DESCRIPTOR
    stop = fields["stop_token_sequences"]
    assert stop.message_type is engine_v1_pb2.TokenIds.DESCRIPTOR
    request = atom_grpc_pb2.GenerateRequest(
        prompt_token_ids=[5, 6],
        sampling=engine_v1_pb2.SamplingParams(max_tokens=4),
        stop_token_sequences=[engine_v1_pb2.TokenIds(ids=[7, 8])],
    )
    decoded = atom_grpc_pb2.GenerateRequest.FromString(request.SerializeToString())
    assert decoded == request
    # Presence crosses the wire: a set max_tokens stays set, an unset field unset.
    assert decoded.sampling.HasField("max_tokens")
    assert not decoded.sampling.HasField("temperature")
