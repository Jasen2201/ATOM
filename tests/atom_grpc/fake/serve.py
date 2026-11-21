# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.fake.serve <api_server args>`: ATOM's own API server
and engine over `FakeRunner`. Spawned children rerun this module, so it imports late."""

import json
import multiprocessing
import os
import re

RUNNER = "tests.atom_grpc.fake.runner.FakeRunner"
CONFIG_ENV = "FAKE_ENGINE_CONFIG"  # the fake config file `FakeRunner` reads
CORES_ENV = "FAKE_ENGINE_CORES"  # JSON: {"front_end": [cpus], "ranks": [[cpus]]}
RECORD_ENV = "FAKE_ENGINE_RECORD"  # the engine record file, if any
LINKS_ENV = "FAKE_ENGINE_LINKS"  # the directory where fake Mooncake ranks meet
FIRST_SEQ_ID_ENV = "FAKE_ENGINE_FIRST_SEQ_ID"  # the API process's first sequence id
STAMPS_ENV = "FAKE_ENGINE_STAMPS"  # where gRPC's per-step stamps go, if anywhere
LEAN_ENV = "FAKE_ENGINE_RECORD_LEAN"  # set: admissions record no prompt ids or chain
COUNTS_ENV = "FAKE_ENGINE_GRPC_COUNTS"  # where gRPC's message counts go, if anywhere
TRANSFER = "tests.atom_grpc.fake.transfer"


def register_mooncake() -> None:
    """Build `mooncake` connectors from `TRANSFER` in this process."""
    from atom.kv_transfer.disaggregation.factory import KVConnectorFactory

    KVConnectorFactory.register(
        "mooncake",
        worker_module=TRANSFER,
        worker_class="FakeMooncakeWorker",
        scheduler_module=TRANSFER,
        scheduler_class="FakeMooncakeScheduler",
    )


def pin(api_server: bool) -> None:
    """Pin the API server, or EngineCore-DP<r>, to its core set before its first thread.
    Every other process keeps the set it inherits: runners their EngineCore's."""
    if CORES_ENV not in os.environ:
        return
    cores = json.loads(os.environ[CORES_ENV])
    name = multiprocessing.current_process().name
    if api_server:
        os.sched_setaffinity(0, cores["front_end"])
    elif rank := re.fullmatch(r"EngineCore-DP(\d+)", name):
        if int(rank[1]) >= len(cores["ranks"]):
            raise RuntimeError(f"{name} has no core set in {cores['ranks']}")
        os.sched_setaffinity(0, cores["ranks"][int(rank[1])])


if __name__ in ("__main__", "__mp_main__"):
    pin(api_server=__name__ == "__main__")

if __name__ == "__mp_main__":  # EngineCores and runners build the connectors
    register_mooncake()

if __name__ == "__main__":
    from atom.metrics.prometheus import initialize_metrics

    initialize_metrics()  # first, as `python -m atom.entrypoints.openai_server` does

    from atom.entrypoints.openai.api_server import main
    from atom.model_engine.arg_utils import EngineArgs
    from atom.utils import set_ulimit
    from tests.atom_grpc.fake.record import (
        count_grpc_messages,
        record_api_process,
        stamp_grpc_steps,
    )

    engine_kwargs = EngineArgs._get_engine_kwargs
    EngineArgs._get_engine_kwargs = lambda self: {
        **engine_kwargs(self),
        "runner_qualname": RUNNER,
    }
    record_api_process()
    stamp_grpc_steps()
    count_grpc_messages()
    if FIRST_SEQ_ID_ENV in os.environ:
        from itertools import count

        from atom.model_engine.sequence import Sequence

        Sequence.counter = count(int(os.environ[FIRST_SEQ_ID_ENV]))
    set_ulimit()
    main()
