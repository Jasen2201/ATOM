# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.harness.launch <topology.toml>`: start the fake instances
and atomesh routers it lists, wait until all serve; on exit, kill each group started."""

import argparse
import contextlib
import ctypes
import json
import os
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import tomllib

from tests.atom_grpc.fake.instance import FakeInstance, free_port
from tests.atom_grpc.harness.atomesh import Atomesh, RouterSpec, atomesh_bin

STARTUP_S = 1500  # a loaded dev host takes minutes per instance
REAP_S = 30  # for a killed group's last members to be reaped
MANIFEST = "topology.json"  # in the scratch directory, rewritten once all are ready
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36


class Stop(Exception):
    """A signal asked the launcher to stop."""


@dataclass(frozen=True)
class InstanceSpec:
    """One `[[instance]]` table of a topology file; `model` is relative to the file."""

    name: str
    model: str
    args: list[str]
    fake: dict
    dp: int = 1  # its DP ranks, each pinned to a core set of its own
    cores: dict | None = None  # {front_end = n, rank = n}: CPUs in each pinned set
    grpc: bool = False  # also serve gRPC, on a free port given as --grpc-port


def load(path: Path) -> tuple[list[InstanceSpec], list[RouterSpec]]:
    """A topology file's instances, then its routers, each naming instances it lists
    and, routing over gRPC, only instances serving it."""
    with open(path, "rb") as f:
        tables = tomllib.load(f)
    specs = [InstanceSpec(**table) for table in tables["instance"]]
    routers = [RouterSpec(**table) for table in tables.get("router", [])]
    names = [spec.name for spec in specs]
    if len(set(names)) < len(names):  # each names a scratch dir and a process group
        raise ValueError(f"{path}: instance names repeat in {names}")
    names += [router.name for router in routers]
    if len(set(names)) < len(names):
        raise ValueError(f"{path}: a router repeats a name in {names}")
    serves_grpc = {spec.name: spec.grpc for spec in specs}
    for router in routers:
        for name in router.instances():
            if name not in serves_grpc:
                raise ValueError(
                    f"{path}: router {router.name} names no instance {name}"
                )
            if router.grpc and not serves_grpc[name]:
                raise ValueError(f"{path}: router {router.name} needs gRPC of {name}")
    return specs, routers


def core_sets(specs: list[InstanceSpec], cpus: list[int]) -> dict[str, dict | None]:
    """Disjoint sets from `cpus` in order: a pinned instance's front end, then its ranks."""
    pinned = [spec for spec in specs if spec.cores]
    needed = sum(s.cores["front_end"] + s.dp * s.cores["rank"] for s in pinned)
    if needed > len(cpus):
        raise ValueError(
            f"the topology pins {needed} CPUs; this process has {len(cpus)}"
        )
    free = iter(cpus)

    def take(count: int) -> list[int]:
        return [next(free) for _ in range(count)]

    sets = {spec.name: None for spec in specs}
    for spec in pinned:
        front_end = take(spec.cores["front_end"])
        ranks = [take(spec.cores["rank"]) for _ in range(spec.dp)]
        sets[spec.name] = {"front_end": front_end, "ranks": ranks}
    return sets


def write_manifest(
    scratch: Path,
    instances: dict,
    cores: dict,
    grpc_ports: dict,
    routers: dict,
    ready: bool,
) -> None:
    manifest = {"ready": ready, "instances": {}, "routers": {}}
    for name, instance in instances.items():
        port = grpc_ports.get(name)
        manifest["instances"][name] = {
            "url": instance.url,
            "pid": instance.proc.pid,  # its process group too
            "workdir": str(instance.workdir),
            "cores": cores[name],
            "grpc": None if port is None else f"127.0.0.1:{port}",
        }
    for name, router in routers.items():
        manifest["routers"][name] = {
            "url": router.url,
            "pid": router.proc.pid,  # its process group too
            "workers": router.workers,
        }
    (scratch / f"{MANIFEST}.tmp").write_text(json.dumps(manifest, indent=1))
    os.replace(scratch / f"{MANIFEST}.tmp", scratch / MANIFEST)


def run(
    path: Path,
    scratch: Path,
    instances: dict[str, FakeInstance],
    routers: dict[str, Atomesh],
) -> None:
    """Start every instance, then every router, into the two dicts; wait until each is
    ready, then until a stop."""
    specs, router_specs = load(path)
    binary = atomesh_bin() if router_specs else ""
    cores = core_sets(specs, sorted(os.sched_getaffinity(0)))
    grpc_ports = {spec.name: free_port() for spec in specs if spec.grpc}
    for spec in specs:
        workdir = scratch / spec.name
        workdir.mkdir(parents=True, exist_ok=True)
        model = (path.parent / spec.model).resolve()
        args = spec.args
        if spec.grpc:
            args = [*args, "--grpc-port", str(grpc_ports[spec.name])]
        instance = FakeInstance(workdir, model, args, spec.fake, cores[spec.name])
        instances[spec.name] = instance
        instance.start()
    write_manifest(scratch, instances, cores, grpc_ports, routers, ready=False)
    deadline = time.monotonic() + STARTUP_S
    for instance in instances.values():
        instance.wait_ready(deadline)
    ranks = {spec.name: spec.dp for spec in specs}
    http_urls = {name: instance.url for name, instance in instances.items()}
    grpc_urls = {name: f"grpc://127.0.0.1:{port}" for name, port in grpc_ports.items()}
    for spec in router_specs:
        workdir = scratch / spec.name
        workdir.mkdir(parents=True, exist_ok=True)
        urls = grpc_urls if spec.grpc else http_urls
        routers[spec.name] = Atomesh(workdir, spec, urls, ranks, binary)
        routers[spec.name].start()
    write_manifest(scratch, instances, cores, grpc_ports, routers, ready=False)
    for router in routers.values():
        router.wait_ready(deadline)
    write_manifest(scratch, instances, cores, grpc_ports, routers, ready=True)
    print(f"ready: {scratch / MANIFEST}", flush=True)
    while True:
        signal.pause()


def _stop(signum, frame):
    for sig in SIGNALS:  # once: the stop that follows must not be interrupted
        signal.signal(sig, signal.SIG_IGN)
    raise Stop(signal.Signals(signum).name)


def stop(instances: dict[str, FakeInstance], routers: dict[str, Atomesh]) -> None:
    """Stop every router, then every instance; reap their orphans until their groups
    hold no process."""
    started = [*routers.values(), *instances.values()]
    for process in started:
        process.stop()  # SIGINT, then SIGKILL to its whole process group
    groups = [process.proc.pid for process in started if process.proc is not None]
    deadline = time.monotonic() + REAP_S
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups = [group for group in groups if _left_after_reaping(group)]
    if groups:
        raise RuntimeError(f"process groups {groups} outlived SIGKILL")


def _left_after_reaping(group: int) -> bool:
    """Reap the adopted members of `group`; whether any process is left in it."""
    with contextlib.suppress(ChildProcessError):  # no child of this process is in it
        while os.waitpid(-group, os.WNOHANG)[0]:
            pass
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topology", type=Path)
    parser.add_argument("--scratch", type=Path, help="default: a new temporary dir")
    args = parser.parse_args()
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix=f"{args.topology.stem}-"))
    scratch.mkdir(parents=True, exist_ok=True)
    for sig in SIGNALS:
        signal.signal(sig, _stop)
    libc = ctypes.CDLL(None)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)  # stop with the parent
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1)  # adopt the instances' orphans, to reap them
    instances: dict[str, FakeInstance] = {}
    routers: dict[str, Atomesh] = {}
    try:
        run(args.topology.resolve(), scratch, instances, routers)
    except Stop:
        pass
    finally:  # a failed launch exits 1 with its traceback, once this has run
        for sig in SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        stop(instances, routers)


if __name__ == "__main__":
    main()
