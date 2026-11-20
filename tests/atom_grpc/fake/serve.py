# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.fake.serve <api_server args>`: ATOM's own API server
and engine over `FakeRunner`. Spawned children rerun this module, so it imports late."""

RUNNER = "tests.atom_grpc.fake.runner.FakeRunner"

if __name__ == "__main__":
    from atom.metrics.prometheus import initialize_metrics

    initialize_metrics()  # first, as `python -m atom.entrypoints.openai_server` does

    from atom.entrypoints.openai.api_server import main
    from atom.model_engine.arg_utils import EngineArgs
    from atom.utils import set_ulimit

    engine_kwargs = EngineArgs._get_engine_kwargs
    EngineArgs._get_engine_kwargs = lambda self: {
        **engine_kwargs(self),
        "runner_qualname": RUNNER,
    }
    set_ulimit()
    main()
