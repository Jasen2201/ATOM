"""#6's stub runner plus what these configurations need; each addition logs its use."""

import multiprocessing
import os

from stub_runner import StubRunner  # spike/cpu_engine/pkgs at refs/pull/6/head

from atom.model_engine.kv_block import STATE_SLOT_CLASS
from atom.model_engine.llm_engine import InputOutputProcessor

SKIP = set(filter(None, os.environ.get("SPIKE_SKIP", "").split(",")))
_used: set[str] = set()


def _log(name: str, what: str) -> None:
    """An addition's first use, in #6's name-log format; the driver fills its caller."""
    path = os.environ.get("SPIKE_NAME_LOG")
    if path and name not in _used:
        _used.add(name)
        process = multiprocessing.current_process().name
        with open(path, "a") as log:
            log.write(f"{process}\tStub.{name}\tbehaviour\t{what}\t(rpc)\n")


class Stub(StubRunner):
    def __init__(self, rank, config):
        super().__init__(rank, config)
        self.config = config

    def get_num_blocks(self):
        info = super().get_num_blocks()
        stateful = InputOutputProcessor._per_req_cache_model_types()
        if self.config.hf_config.model_type in stateful and "state_slots" not in SKIP:
            per_req = 1 + self.mtp_k
            slots = self.config.max_num_seqs * per_req
            info["pool_entries"] = {STATE_SLOT_CLASS: slots}
            info["pool_entries_per_req"] = {STATE_SLOT_CLASS: per_req}
            _log("get_num_blocks", f"reports {slots} state slots, {per_req} a request")
        return info


def flush_pp_send(self):
    """Returns True, as the runner does once its pending PP sends are flushed."""
    _log("flush_pp_send", flush_pp_send.__doc__)
    return True


if "flush_pp_send" not in SKIP:
    Stub.flush_pp_send = flush_pp_send
