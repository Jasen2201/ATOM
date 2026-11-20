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

if __name__ == "__main__":
    from atom.metrics.prometheus import initialize_metrics

    initialize_metrics()  # first, as `python -m atom.entrypoints.openai_server` does

    from atom.entrypoints.openai.api_server import main
    from atom.model_engine.arg_utils import EngineArgs
    from atom.utils import set_ulimit
    from tests.atom_grpc.fake.record import record_api_process

    engine_kwargs = EngineArgs._get_engine_kwargs
    EngineArgs._get_engine_kwargs = lambda self: {
        **engine_kwargs(self),
        "runner_qualname": RUNNER,
    }
    record_api_process()
    set_ulimit()
    main()
