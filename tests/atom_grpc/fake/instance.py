# SPDX-License-Identifier: MIT
"""One fake instance as a test drives it: `python -m tests.atom_grpc.fake.serve`
in a process group of its own, its fake config in a file, its sockets in `workdir`."""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil

from tests.atom_grpc.fake.runner import CONFIG_ENV

ROOT = Path(__file__).resolve().parents[3]
STAND_INS = Path(__file__).resolve().parent / "stand_ins"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeInstance:
    """`start`, `wait_ready`, then requests; `stop` kills whatever its group left."""

    def __init__(self, workdir: Path, model: Path, args: list[str], fake: dict):
        self.workdir = workdir
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        (workdir / "fake.json").write_text(json.dumps(fake))
        self.cmd = [sys.executable, "-m", "tests.atom_grpc.fake.serve"]
        self.cmd += ["--model", str(model), "--server-port", str(self.port)]
        self.cmd += ["--data-parallel-master-port", str(free_port())]
        self.cmd += ["--data-parallel-base-port", str(free_port()), *args]
        path = [str(STAND_INS), str(ROOT), os.environ.get("PYTHONPATH", "")]
        self.env = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join(filter(None, path)),
            CONFIG_ENV: str(workdir / "fake.json"),
            "TMPDIR": str(workdir),  # ZMQ ipc paths: short, and gone with the dir
        }
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        with open(self.workdir / "server.log", "w") as log:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=ROOT,
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def wait_ready(self, deadline: float) -> None:
        """Until /health answers 200; fail early once the server or a child is dead."""
        zombies: set[int] = set()
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self._fail(f"exited with {self.proc.returncode}")
            # A dead EngineCore leaves the server waiting forever, unreaped.
            seen, zombies = zombies, self._zombie_children()
            if seen & zombies:
                self._fail(f"child processes {sorted(seen & zombies)} died")
            with contextlib.suppress(OSError):
                if self.get("/health")[0] == 200:
                    return
            time.sleep(1)
        self._fail("did not answer /health in time")

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

    def get(self, path: str, timeout: float = 60) -> tuple[int, object]:
        return self._call(urllib.request.Request(self.url + path), timeout)

    def post(self, path: str, body: dict, timeout: float = 600) -> tuple[int, object]:
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        return self._call(request, timeout)

    @staticmethod
    def _call(request, timeout: float) -> tuple[int, object]:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode(errors="replace")

    def _zombie_children(self) -> set[int]:
        with contextlib.suppress(psutil.Error):
            children = psutil.Process(self.proc.pid).children()
            return {c.pid for c in children if c.status() == psutil.STATUS_ZOMBIE}
        return set()

    def _fail(self, what: str):
        log = (self.workdir / "server.log").read_text(errors="replace")
        args = " ".join(self.cmd[3:])
        raise RuntimeError(f"fake instance `{args}` {what}:\n{log[-4000:]}")
