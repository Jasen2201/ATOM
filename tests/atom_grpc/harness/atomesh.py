# SPDX-License-Identifier: MIT
"""The atomesh routers a topology lists: each runs the binary `ATOMESH_BIN` names, in a
process group of its own, over its instances' gRPC servers or their HTTP ones."""

import contextlib
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from tests.atom_grpc.fake.instance import Http, free_port

ATOMESH_BIN = "ATOMESH_BIN"
LOG = "atomesh.log"  # in the router's scratch directory


def atomesh_bin() -> str:
    """The atomesh routers run; unset, a launch with routers fails, not skips."""
    path = os.environ.get(ATOMESH_BIN)
    if not path:
        raise RuntimeError(
            f"{ATOMESH_BIN} is unset: build atom/mesh's atomesh and name it there"
        )
    return path


@dataclass(frozen=True)
class RouterSpec:
    """One `[[router]]` table: atomesh over the instances it names, `workers` regular,
    `prefill` and `decode` P/D; `args` follow the flags the launcher gives it."""

    name: str
    workers: list[str] = field(default_factory=list)
    prefill: list[str] = field(default_factory=list)
    decode: list[str] = field(default_factory=list)
    grpc: bool = True  # their gRPC servers, else their HTTP servers
    args: list[str] = field(default_factory=list)

    def instances(self) -> list[str]:
        return [*self.workers, *self.prefill, *self.decode]


class Atomesh(Http):
    """`start`, `wait_ready` until it lists each url@rank in `workers`, then `stop`.
    `urls` holds each instance's URL for this router, `ranks` its DP size."""

    def __init__(
        self,
        workdir: Path,
        spec: RouterSpec,
        urls: dict[str, str],
        ranks: dict[str, int],
        binary: str,
    ):
        self.workdir, self.grpc = workdir, spec.grpc
        port = free_port()
        self.url = f"http://127.0.0.1:{port}"
        cmd = [binary, "--host", "127.0.0.1", "--port", str(port)]
        cmd += ["--prometheus-host", "127.0.0.1", "--prometheus-port", str(free_port())]
        if spec.workers:
            cmd += ["--worker-urls", *(urls[name] for name in spec.workers)]
        if spec.prefill or spec.decode:
            cmd.append("--pd-disaggregation")
            cmd += [arg for name in spec.prefill for arg in ("--prefill", urls[name])]
            cmd += [arg for name in spec.decode for arg in ("--decode", urls[name])]
        self.cmd = [*cmd, *spec.args]
        dp_aware = "--dp-aware" in spec.args  # then one worker per DP rank
        self.workers = [
            f"{urls[name]}@{rank}" if dp_aware else urls[name]
            for name in spec.instances()
            for rank in range(ranks[name] if dp_aware else 1)
        ]
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        with open(self.workdir / LOG, "w") as log:
            self.proc = subprocess.Popen(
                self.cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def wait_ready(self, deadline: float) -> None:
        """Until each of `workers` is listed healthy, in a gRPC router with its model's
        tokenizer loaded; fail early once atomesh has exited."""
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._fail(f"exited with {self.proc.returncode}")
            with contextlib.suppress(OSError, ValueError):
                if self._serves():
                    return
            time.sleep(1)
        self._fail(f"did not list {self.workers} in time")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()

    def _serves(self) -> bool:
        status, listing = self.get("/workers")
        if status != 200:
            return False
        models = {
            w["url"]: w["model_id"] for w in listing["workers"] if w["is_healthy"]
        }
        if not set(self.workers) <= models.keys():
            return False
        if not self.grpc:  # an HTTP router routes without one
            return True
        status, loaded = self.get("/v1/tokenizers")
        names = {t["name"] for t in loaded["tokenizers"]} if status == 200 else set()
        return {models[url] for url in self.workers} <= names

    def _fail(self, what: str):
        log = (self.workdir / LOG).read_text(errors="replace")
        raise RuntimeError(f"atomesh `{' '.join(self.cmd[1:])}` {what}:\n{log[-4000:]}")
