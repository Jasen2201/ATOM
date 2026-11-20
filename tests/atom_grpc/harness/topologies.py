# SPDX-License-Identifier: MIT
"""Topologies as tests drive them: each one run of the launcher command, in a scratch
directory of its own, with at most one running at a time."""

import contextlib
import inspect
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from tests.atom_grpc.fake.instance import ROOT, Http
from tests.atom_grpc.harness.launch import MANIFEST, STARTUP_S

TOPOLOGIES = Path(__file__).resolve().parents[1] / "topologies"
STOP_S = 300  # the launcher stops its instances one after another


@dataclass
class Instance(Http):
    """A fake instance as its launcher reports it; `pid`, its API server, leads its group."""

    url: str
    pid: int
    workdir: Path
    cores: dict | None


def group_members(group: int) -> list[int]:
    """Every process in process group `group`, zombies included."""
    members = []
    for pid in psutil.pids():
        with contextlib.suppress(OSError):
            if os.getpgid(pid) == group:
                members.append(pid)
    return members


class Topology:
    """One `python -m tests.atom_grpc.harness.launch` run and the instances it reports."""

    def __init__(self, path: Path, scratch: Path):
        self.scratch = scratch
        scratch.mkdir(parents=True, exist_ok=True)
        self.log = scratch / "launch.log"
        cmd = [sys.executable, "-m", "tests.atom_grpc.harness.launch", str(path)]
        cmd += ["--scratch", str(scratch)]
        with open(self.log, "w") as log:
            self.proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
            )
        self.instances: dict[str, Instance] = {}

    def wait_ready(self) -> None:
        deadline = time.monotonic() + STARTUP_S + STOP_S
        while not self._manifest().get("ready"):
            if self.proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(
                    f"launch not ready, exit {self.proc.poll()}:\n{self.tail()}"
                )
            time.sleep(1)
        for name, fields in self._manifest()["instances"].items():
            fields["workdir"] = Path(fields["workdir"])
            self.instances[name] = Instance(**fields)

    def groups(self) -> list[int]:
        """The process groups the launcher started: one per instance."""
        return [
            fields["pid"] for fields in self._manifest().get("instances", {}).values()
        ]

    def stop(self) -> dict[int, list[int]]:
        """SIGINT the launcher, then kill and return whatever its groups still hold."""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=STOP_S)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        left = {group: group_members(group) for group in self.groups()}
        left = {group: pids for group, pids in left.items() if pids}
        for group in left:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        return left

    def tail(self) -> str:
        return self.log.read_text(errors="replace")[-8000:]

    def _manifest(self) -> dict:
        with contextlib.suppress(FileNotFoundError):
            return json.loads((self.scratch / MANIFEST).read_text())
        return {}


class Topologies:
    """The session's topologies: `up(name)` stops whichever other one runs, then starts
    `topologies/<name>.toml` unless it is already up; a failed start is not retried."""

    def __init__(self, tmp_path_factory):
        self.tmp = tmp_path_factory
        self.running: tuple[str, Topology] | None = None
        self.failed: dict[str, RuntimeError] = {}

    def up(self, name: str) -> Topology:
        if self.running is not None and self.running[0] == name:
            return self.running[1]
        self.down()
        if name in self.failed:
            raise self.failed[name]
        topology = Topology(TOPOLOGIES / f"{name}.toml", self.tmp.mktemp(name))
        try:
            topology.wait_ready()
        except RuntimeError as error:
            topology.stop()
            self.failed[name] = error
            raise
        self.running = (name, topology)
        return topology

    def down(self) -> None:
        """Stop the running topology; fail if its launcher did, or left a process."""
        if self.running is None:
            return
        (name, topology), self.running = self.running, None
        left = topology.stop()
        if left or topology.proc.returncode != 0:
            raise RuntimeError(
                f"{name}: launch exit {topology.proc.returncode}, left {left}:\n"
                + topology.tail()
            )


def by_topology(items: list) -> list:
    """Tests marked `topology(name)` in one group per name, at its first test's place;
    those with a `topologies` argument run last in it. Others keep their places."""
    anchors: dict[str, int] = {}
    keys = []
    for index, item in enumerate(items):
        marker = item.get_closest_marker("topology")
        if marker is None:
            keys.append((index, False, index))
        else:
            anchor = anchors.setdefault(marker.args[0], index)
            own_args = inspect.signature(item.function).parameters
            keys.append((anchor, "topologies" in own_args, index))
    order = sorted(range(len(items)), key=keys.__getitem__)
    return [items[index] for index in order]
