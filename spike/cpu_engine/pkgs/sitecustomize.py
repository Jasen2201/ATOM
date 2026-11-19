"""Name the stub runner in Config: ATOM's CLI has no runner_qualname flag."""

import importlib.abc
import importlib.machinery
import os
import sys


class _NameRunner(importlib.abc.MetaPathFinder):
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
                config.runner_qualname = os.environ["SPIKE_RUNNER"]
                post_init(config)

            module.Config.__post_init__ = named

        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("SPIKE_RUNNER"):
    sys.meta_path.insert(0, _NameRunner())
