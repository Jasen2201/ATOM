"""`python -m stub_server <api_server args>`: ATOM's API server on #6's stub runner.
Spawned children rerun only this top level, which imports nothing from the API stack."""

import importlib.abc
import importlib.machinery
import sys

RUNNER = "stub_runner.StubRunner"


class _NameRunner(importlib.abc.MetaPathFinder):
    """Name the stub runner in every Config, once atom.config is imported."""

    def find_spec(self, name, path, target=None):
        if name != "atom.config":
            return None
        sys.meta_path.remove(self)
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        load = spec.loader.exec_module

        def exec_module(module):
            load(module)
            post_init = module.Config.__post_init__

            def named(config):
                config.runner_qualname = RUNNER
                post_init(config)

            module.Config.__post_init__ = named

        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _NameRunner())

if __name__ == "__main__":
    from atom.metrics.prometheus import initialize_metrics

    initialize_metrics()  # what api_server's own __main__ block does first
    from atom.entrypoints.openai import api_server

    api_server.main()
