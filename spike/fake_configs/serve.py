"""`python -m spike.fake_configs.serve <server args>`: ATOM's own server on the stub
runner, #6's aiter stand-in plus the names below, and no triton."""

if __name__ == "__main__":
    from atom.metrics.prometheus import initialize_metrics

    initialize_metrics()  # first, as `python -m atom.entrypoints.openai_server` does

import multiprocessing
import os
import sys

import aiter  # noqa: F401  #6's stand-in, on PYTHONPATH
from stand_ins import stand_in

SKIP = set(filter(None, os.environ.get("SPIKE_SKIP", "").split(",")))
RUNNER = "spike.fake_configs.stub.Stub"


def get_gfx():
    """Returns 'gfx950', the chip DCP with speculative decoding asks for."""
    return "gfx950"


# aiter names this spike adds; every spawned process reruns this module first.
ADDED = {"jit.utils.chip_info": {"get_gfx": get_gfx}}
stand_in("aiter", {sub: n for sub, n in ADDED.items() if not SKIP & set(n)})

from atom.config import SpeculativeConfig
from atom.model_engine.async_proc import AsyncIOProcManager

_resolve = SpeculativeConfig._resolve_synthetic_acceptance


def _resolve_below_early_return(self):
    """`acceptance_length_to_rates` imported below the early return, not above it."""
    if (
        self.synthetic_acceptance_rate is None
        and self.synthetic_acceptance_length is None
    ):
        return
    _resolve(self)


SpeculativeConfig._resolve_synthetic_acceptance = _resolve_below_early_return
_logged: set[tuple[str, str]] = set()


def _log_rpc(call):
    """Log each runner RPC an EngineCore sends: process, name, caller, stub lacks it."""

    def logged(self, func_name, *args, **kwargs):
        frame = sys._getframe(1)
        caller = f"{frame.f_globals['__name__']}:{frame.f_code.co_qualname}"
        path = os.environ.get("SPIKE_RPC_LOG")
        if path and (func_name, caller) not in _logged:
            _logged.add((func_name, caller))
            from spike.fake_configs.stub import Stub

            lacks = "" if hasattr(Stub, func_name) else "stub lacks it"
            process = multiprocessing.current_process().name
            with open(path, "a") as log:
                log.write(f"{process}\t{func_name}\t{caller}\t{lacks}\n")
        return call(self, func_name, *args, **kwargs)

    return logged


for _name in ("call_func", "call_func_with_aggregation"):
    setattr(AsyncIOProcManager, _name, _log_rpc(getattr(AsyncIOProcManager, _name)))

if __name__ == "__main__":
    from atom.entrypoints.openai.api_server import main
    from atom.model_engine.arg_utils import EngineArgs
    from atom.utils import set_ulimit

    _kwargs = EngineArgs._get_engine_kwargs
    EngineArgs._get_engine_kwargs = lambda self: {
        **_kwargs(self),
        "runner_qualname": RUNNER,
    }
    set_ulimit()
    main()
