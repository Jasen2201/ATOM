# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.timing.a7t --split <test binary>`, as run.sh runs it: atomesh
over one fake instance's Session, a step every 16.5 ms, at DP1 and DP4: the CPU of the
threads serving gRPC at c512, each step's delivery and lag at c1024, atomesh's split."""

import argparse
import asyncio
import json
import math
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import NamedTuple

import aiohttp
import psutil

from atom.entrypoints.grpc.server import THREAD_NAME
from atom.entrypoints.grpc.session import SEQ_IDS
from tests.atom_grpc.fake.record import grpc_steps
from tests.atom_grpc.fake.serve import STAMPS_ENV
from tests.atom_grpc.harness.atomesh import LOG
from tests.atom_grpc.harness.launch import core_sets, load
from tests.atom_grpc.harness.topologies import TOPOLOGIES, Topology
from tests.atom_grpc.timing import tools

DPS = (1, 4)  # one topology each, topologies/a7t_dp<n>.toml
PROMPT, OUTPUT = 1024, 4096  # tokens of each request; one that ends is replaced at once
CPU_LOAD, LAG_LOAD = 512, 1024  # requests in flight
# Requests start one by one over about one's length, so they end and are replaced one by
# one; started at once, every request ends in the same step, and so on in waves.
RAMP_S = 20
WARM_S = RAMP_S + 5  # the load before its window
WINDOW_S, SETTLE_S = 60, 2  # the window, then the load past it
# The server's thread and those it starts, which keep its name, then grpcio's own threads.
GRPC_THREADS = (THREAD_NAME, "event_engine", "grpc_global_tim", "lifeguard")
CPU_BOUND = 0.05  # of one core
LAG_BOUND_NS = 2_000_000  # p99
SPLIT_BOUND_NS = 1_000_000  # p99
STEP_ARRIVAL = "atom_session_step"  # the target of atomesh's event per StreamBatch read
SPLIT = "routers::grpc::engine::atom::session::tests::split_one_step_of_1024_requests"


def stat_ticks(stat: str) -> tuple[str, int]:
    """The name and user plus system clock ticks in a /proc stat file's text."""
    close = stat.rindex(")")  # the name may hold parentheses
    name, fields = stat[stat.index("(") + 1 : close], stat[close + 1 :].split()
    return name, int(fields[11]) + int(fields[12])  # fields 14 and 15


def thread_ticks(pid: int) -> dict[int, tuple[str, int]]:
    """Each live thread of `pid` by tid: its name and ticks."""
    threads = {}
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            threads[int(tid)] = stat_ticks(
                Path(f"/proc/{pid}/task/{tid}/stat").read_text()
            )
        except (FileNotFoundError, ProcessLookupError):  # it ended since the listing
            pass
    return threads


def busy(samples: list[dict], names: tuple) -> int:
    """Ticks the threads named in `names` used from the first sample on, each up to the
    last sample it is in: one that ends between samples loses only that stretch."""
    last = {}
    for sample in samples:
        last.update(sample)
    first = samples[0]
    return sum(
        ticks - first.get(tid, (name, 0))[1]
        for tid, (name, ticks) in last.items()
        if name in names
    )


def lags(sent: list, arrived: list, start: int, end: int) -> tuple[list[int], int]:
    """The lag of each step handed to gRPC in [start, end) that reached atomesh, and how
    many did not. Steps match by seq ids: engine ids sent, a Session's ids arrived."""
    stamped = defaultdict(deque)  # each step's seq ids: when each was handed to gRPC
    for at, seq_ids in sent:
        stamped[tuple(seq_id % SEQ_IDS for seq_id in seq_ids)].append(at)
    found = []
    for at, seq_ids in arrived:
        queue = stamped[tuple(seq_ids)]
        if not queue:
            raise ValueError(f"atomesh read a StreamBatch of {seq_ids} no step was")
        handed = queue.popleft()
        if start <= handed < end:
            found.append(at - handed)
    missed = sum(start <= at < end for queue in stamped.values() for at in queue)
    return found, missed


def mesh_steps(log: Path) -> list[tuple[int, list[int]]]:
    """Each StreamBatch atomesh's Session reader read, from its JSON log: (at, seq ids)."""
    text, steps = log.read_text(errors="replace"), []
    for line in text[: text.rfind("\n") + 1].splitlines():
        event = json.loads(line) if STEP_ARRIVAL in line else {}
        if event.get("target") == STEP_ARRIVAL:
            steps.append((event["at"], json.loads(event["seq_ids"])))
    return steps


def p99(values: list[int]) -> int:
    return sorted(values)[math.ceil(len(values) * 0.99) - 1]


def split_ns(bench: Path) -> tuple[int, int]:
    """The p50 and p99 ns atomesh's split benchmark prints, run here."""
    command = [str(bench), "--ignored", "--exact", SPLIT, "--nocapture"]
    out = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    found = re.search(r"p50_ns=(\d+) p99_ns=(\d+)", out)
    if found is None:
        raise RuntimeError(f"no split time in:\n{out}")
    return int(found[1]), int(found[2])


class Window(NamedTuple):
    start: int  # CLOCK_MONOTONIC ns
    end: int
    threads: list[dict]  # `thread_ticks` of the API server, once a second
    process: int  # the API server's ticks in the window
    ended: list[str]  # "ok" or what went wrong, for each request that ended


def outcome(status: int, text: str) -> str:
    if status != 200:
        return f"status {status}: {text[:200]}"
    reply = json.loads(text)
    tokens = len((reply[0] if isinstance(reply, list) else reply)["output_ids"])
    return "ok" if tokens == OUTPUT else f"{tokens} output tokens"


async def drive(url: str, model: str, concurrency: int, vocab: int, stop) -> list:
    """`concurrency` /generate requests for `model` in flight until `stop`, each replaced
    when it ends: the outcome of each that ended."""
    ended = []
    timeout = aiohttp.ClientTimeout(total=None)
    connector = aiohttp.TCPConnector(limit=0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as client:

        async def requests(worker: int) -> None:
            await asyncio.sleep(worker * RAMP_S / concurrency)
            rng = random.Random(worker)  # a prompt per request; none shares a block
            sampling = {"max_new_tokens": OUTPUT, "ignore_eos": True, "temperature": 0}
            while True:
                ids = [rng.randrange(vocab) for _ in range(PROMPT)]
                body = {"model": model, "input_ids": ids, "sampling_params": sampling}
                try:
                    async with client.post(f"{url}/generate", json=body) as reply:
                        ended.append(outcome(reply.status, await reply.text()))
                except Exception as error:  # noqa: BLE001 - counted as failed
                    ended.append(repr(error))

        tasks = [asyncio.create_task(requests(worker)) for worker in range(concurrency)]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return ended


async def window(router, concurrency: int, vocab: int, pid: int) -> Window:
    """The load through `router`, with the API server's threads read once a second over
    its window."""
    status, listing = router.get("/workers")
    if status != 200:
        raise RuntimeError(f"atomesh lists no workers: {listing}")
    model = listing["workers"][0]["model_id"]
    stop = asyncio.Event()
    driving = asyncio.create_task(drive(router.url, model, concurrency, vocab, stop))
    await asyncio.sleep(WARM_S)
    start = time.monotonic_ns()
    before, samples = Path(f"/proc/{pid}/stat").read_text(), [thread_ticks(pid)]
    for second in range(1, WINDOW_S + 1):
        await asyncio.sleep(max(0.0, start / 1e9 + second - time.monotonic()))
        samples.append(thread_ticks(pid))
    end, after = time.monotonic_ns(), Path(f"/proc/{pid}/stat").read_text()
    await asyncio.sleep(SETTLE_S)  # the window's last steps reach atomesh
    stop.set()
    ended = await driving
    process = stat_ticks(after)[1] - stat_ticks(before)[1]
    return Window(start, end, samples, process, ended)


def run(dp: int, concurrency: int, stamped: bool, scratch: Path, cores: dict) -> tuple:
    """One launch of topology a7t_dp<dp> under the load: its `Window`, then the steps
    handed to gRPC and those atomesh read, when `stamped`."""
    path, workdir = TOPOLOGIES / f"a7t_dp{dp}.toml", scratch / f"dp{dp}-c{concurrency}"
    workdir.mkdir()
    env = {}
    if stamped:
        env = {STAMPS_ENV: str(workdir / "grpc_steps.jsonl")}
        env["RUST_LOG"] = f"mesh=info,{STEP_ARRIVAL}=debug"
    os.environ.update(env)
    os.sched_setaffinity(0, cores["launcher"])  # inherited by the launcher, which pins
    topology = Topology(path, workdir)
    for name in env:
        del os.environ[name]
    os.sched_setaffinity(0, cores["client"])
    try:
        topology.wait_ready()
        router, (instance,) = topology.routers["mesh"], topology.instances.values()
        for thread in psutil.Process(router.pid).threads():
            os.sched_setaffinity(thread.id, cores["atomesh"])
        (spec,), _ = load(path)
        config = json.loads((TOPOLOGIES / spec.model / "config.json").read_text())
        args = router, concurrency, config["vocab_size"], instance.pid
        measured = asyncio.run(window(*args))
    finally:
        left = topology.stop()
    if left or topology.proc.returncode:
        code = topology.proc.returncode
        raise RuntimeError(f"launch exit {code}, left {left}:\n{topology.tail()}")
    if not stamped:
        return measured, [], []
    mesh = workdir / "mesh" / LOG
    return measured, grpc_steps(workdir / "grpc_steps.jsonl"), mesh_steps(mesh)


def ended(outcomes: list[str]) -> tuple[str, bool]:
    """How many requests ended and how many of them failed; whether none did."""
    bad = [outcome for outcome in outcomes if outcome != "ok"]
    first = f", first: {bad[0]}" if bad else ""
    return f"{len(outcomes)} requests ended, {len(bad)} failed{first}", not bad


def ms(ns: float) -> str:
    return f"{ns / 1e6:.3f} ms"


def measure(bench: Path, scratch: Path) -> int:
    """The case: 0 when every bound holds, 1 when one does not, 2 when the load average
    refuses the run."""
    *launcher, mesh, client = sorted(os.sched_getaffinity(0))
    cores = {"container": [*launcher, mesh, client]}
    for dp in DPS:
        specs, _ = load(TOPOLOGIES / f"a7t_dp{dp}.toml")
        for name, sets in core_sets(specs, launcher).items():
            cores[f"{name} front end"] = sets["front_end"]
            cores |= {f"{name} rank {r}": cpus for r, cpus in enumerate(sets["ranks"])}
    cores |= {"atomesh": [mesh], "client": [client]}
    load_average = tools.load_average(Path("/proc/loadavg").read_text())
    kthreads = int(os.environ.get(tools.KTHREADS_ENV, "0"))
    print("\n".join(tools.header("a7t", load_average, kthreads, cores)), flush=True)
    if tools.refuses(load_average, kthreads):
        print(f"refused: the load less those threads is {tools.MAX_LOAD:g} or more")
        return 2

    verdicts = []

    def report(case: str, text: str, ok: bool) -> None:
        verdicts.append(ok)
        print(f"{case}: {text}: {'pass' if ok else 'FAIL'}", flush=True)

    os.sched_setaffinity(0, [mesh])
    p50, worst = split_ns(bench)
    text = f"p50 {ms(p50)}, p99 {ms(worst)}; bound {ms(SPLIT_BOUND_NS)}"
    print()
    report("split of one 1024-request StreamBatch", text, worst < SPLIT_BOUND_NS)
    pinned = {"launcher": launcher, "atomesh": [mesh], "client": [client]}
    tick = os.sysconf("SC_CLK_TCK")
    for dp in DPS:
        cpu, *_ = run(dp, CPU_LOAD, False, scratch, pinned)
        seconds = (cpu.end - cpu.start) / 1e9
        share = busy(cpu.threads, GRPC_THREADS) / tick / seconds
        requests, served = ended(cpu.ended)
        text = f"the threads serving gRPC {share:.2%} of one core, the process "
        text += f"{cpu.process / tick / seconds:.1%}, over {seconds:.1f} s; bound "
        text += f"{CPU_BOUND:.0%}; {requests}"
        report(f"DP{dp} c{CPU_LOAD}", text, share <= CPU_BOUND and served)

        lag, sent, arrived = run(dp, LAG_LOAD, True, scratch, pinned)
        found, missed = lags(sent, arrived, lag.start, lag.end)
        worst = p99(found) if found else math.inf
        median = sorted(found)[len(found) // 2] if found else math.inf
        rate = len(found) / ((lag.end - lag.start) / 1e9)
        requests, served = ended(lag.ended)
        text = f"{len(found)} steps reached atomesh ({rate:.1f}/s), {missed} did not; "
        text += f"lag p50 {ms(median)}, p99 {ms(worst)}; bound {ms(LAG_BOUND_NS)}; "
        text += requests
        good = bool(found) and not missed and worst <= LAG_BOUND_NS and served
        report(f"DP{dp} c{LAG_LOAD}", text, good)
    print(f"\n{'pass' if all(verdicts) else 'FAIL'}")
    return 0 if all(verdicts) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", type=Path, required=True, help="atomesh's lib tests")
    bench = parser.parse_args().split.resolve()
    return measure(bench, Path(tempfile.mkdtemp(prefix="a7t-")))


if __name__ == "__main__":
    sys.exit(main())
