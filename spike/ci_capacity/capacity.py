"""pytest plugin (`-p capacity`): once pytest has collected tests/, start 22 fake DP
ranks (3 x DP2, 4 x DP4), serve 2000 completions over them, print time and memory."""

import asyncio
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import psutil
import pytest

ENV = dict(os.environ)  # taken before collection, which may change os.environ
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Each instance's -tp; with --enable-dp-attention every TP rank is a DP rank.
LAYOUT = [int(tp) for tp in os.environ.get("SPIKE_LAYOUT", "2,2,2,4,4,4,4").split(",")]
REQUESTS = int(os.environ.get("SPIKE_REQUESTS", "2000"))
CYCLES = int(os.environ.get("SPIKE_CYCLES", "3"))
CONCURRENCY, MAX_TOKENS, DEADLINE_S = 256, 8, 1800
T0 = time.monotonic()


def log(event: str, **fields) -> None:
    print(json.dumps({"event": event, "t": round(time.monotonic() - T0), **fields}))
    sys.stdout.flush()


def load_cpu_engine():
    """#6's spike/cpu_engine directory and its drive.py, from $SPIKE_CPU_ENGINE."""
    path = Path(os.environ["SPIKE_CPU_ENGINE"])
    spec = importlib.util.spec_from_file_location("cpu_engine_drive", path / "drive.py")
    drive = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drive)
    return path, drive


def meminfo() -> dict[str, int]:
    """/proc/meminfo in MiB."""
    with open("/proc/meminfo") as f:
        rows = (line.split(":") for line in f)
        return {key: int(value.split()[0]) // 1024 for key, value in rows}


def in_use() -> dict[str, int]:
    m = meminfo()
    return {
        "mem_used_mib": m["MemTotal"] - m["MemAvailable"],
        "swap_used_mib": m["SwapTotal"] - m["SwapFree"],
    }


class Sampler(threading.Thread):
    """Peak memory and swap in use, read each second; a log line every 30 s."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak = in_use()
        self.done = threading.Event()

    def run(self):
        for tick in range(1, sys.maxsize):
            if self.done.wait(1.0):
                return
            now = in_use()
            self.peak = {key: max(self.peak[key], now[key]) for key in now}
            if tick % 30 == 0:
                log("sample", **now, load1=round(os.getloadavg()[0], 1))


def role(proc: psutil.Process, servers: set[int]) -> str:
    if proc.pid in servers:
        return "api_server"
    cmd = " ".join(proc.cmdline())
    if "resource_tracker" in cmd:
        return "resource_tracker"
    if cmd.startswith("ATOM::EngineCore"):
        return "engine_core"
    return "runner" if cmd.startswith("ATOM::") else cmd[:40]


def uss_by_role(procs: list[subprocess.Popen]) -> dict[str, dict]:
    """Median and max USS (and swap) of each role's processes, over every instance."""
    servers = {proc.pid for proc in procs}
    roles = defaultdict(list)
    for pid in servers:
        try:
            top = psutil.Process(pid)
            tree = [top, *top.children(recursive=True)]
        except psutil.Error:  # an instance that died is reported by its cycle
            continue
        for proc in tree:
            try:
                info = proc.memory_full_info()
                roles[role(proc, servers)].append((info.uss, info.swap))
            except psutil.Error:
                pass
    mib = lambda values: [round(v / 2**20) for v in values]
    return {
        name: {
            "n": len(rows),
            "uss_median_mib": statistics.median(mib(r[0] for r in rows)),
            "uss_max_mib": max(mib(r[0] for r in rows)),
            "swap_median_mib": statistics.median(mib(r[1] for r in rows)),
        }
        for name, rows in sorted(roles.items())
    }


def process_rss() -> dict[str, int]:
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
    )
    kib = lambda key: int(status[key].split()[0]) // 1024
    return {"pytest_rss_mib": kib("VmRSS"), "pytest_hwm_mib": kib("VmHWM")}


async def serve(ranks: list[tuple[int, int]], prompt: list[int]) -> list[str]:
    """REQUESTS completions, request k on ranks[k % len(ranks)]; returns the failures."""
    gate, failures, done = asyncio.Semaphore(CONCURRENCY), [], [0]
    connector = aiohttp.TCPConnector(limit=0, force_close=True)
    timeout = aiohttp.ClientTimeout(total=DEADLINE_S)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:

        async def one(k: int) -> None:
            port, rank = ranks[k % len(ranks)]
            body = {"prompt_token_ids": prompt, "max_tokens": MAX_TOKENS}
            body.update(temperature=0, data_parallel_rank=rank)
            async with gate:
                url = f"http://127.0.0.1:{port}/v1/completions"
                try:
                    async with session.post(url, json=body) as reply:
                        usage = (await reply.json()).get("usage") or {}
                        tokens = usage.get("completion_tokens")
                        if reply.status != 200 or tokens != MAX_TOKENS:
                            failures.append(f"{reply.status} {usage}")
                except Exception as e:  # noqa: BLE001 - every failure is a result
                    failures.append(repr(e))
                done[0] += 1

        async def progress() -> None:
            while True:
                await asyncio.sleep(30)
                log("serving", done=done[0], failed=len(failures))

        ticker = asyncio.create_task(progress())
        try:
            jobs = asyncio.gather(*(one(k) for k in range(REQUESTS)))
            await asyncio.wait_for(jobs, DEADLINE_S)
        finally:
            ticker.cancel()
    return failures


def cycle(n: int, cpu_engine: Path, drive, model: Path, work: Path) -> dict:
    """Start the layout, wait for every /health 200, serve, stop; one result line."""
    paths = [cpu_engine / "pkgs", cpu_engine / "aiter_pkg", ROOT, HERE]
    procs, ports, ranks, sampler = [], [], [], Sampler()
    sampler.start()
    start = time.monotonic()
    result = {"cycle": n}
    try:
        for i, tp in enumerate(LAYOUT):
            port = drive.free_port()
            env = dict(ENV, PYTHONPATH=":".join(map(str, paths)))
            env.update(TMPDIR=tempfile.mkdtemp(prefix="i"), AITER_LOG_LEVEL="WARNING")
            cmd = [sys.executable, "-m", "stub_server", "--model", str(model)]
            cmd += ["--server-port", str(port), "-tp", str(tp), "--enable-dp-attention"]
            cmd += ["--data-parallel-master-port", str(drive.free_port())]
            with open(work / f"cycle{n}-instance{i}.log", "w") as out:
                procs.append(
                    subprocess.Popen(
                        cmd,
                        env=env,
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )
            ports.append(port)
            ranks += [(port, rank) for rank in range(tp)]
        for proc, port in zip(procs, ports):
            drive.wait_ready(proc, f"http://127.0.0.1:{port}", start)
        result["ready_s"] = round(time.monotonic() - start, 1)
        log("healthy", cycle=n, ready_s=result["ready_s"], uss_idle=uss_by_role(procs))
        served = time.monotonic()
        failures = asyncio.run(serve(ranks, drive.PROMPT))
        result["serve_s"] = round(time.monotonic() - served, 1)
        result.update(requests=REQUESTS, failed=len(failures), failures=failures[:3])
        result.update(uss=uss_by_role(procs), **process_rss())
    finally:
        sampler.done.set()
        sampler.join()
        result.update(peak_mem_used_mib=sampler.peak["mem_used_mib"])
        result.update(peak_swap_used_mib=sampler.peak["swap_used_mib"])
        for i, proc in enumerate(procs):
            if proc.poll() is not None:  # died before the stop: say where
                log("died", cycle=n, instance=i, exit_code=proc.returncode)
                print((work / f"cycle{n}-instance{i}.log").read_text()[-3000:])
        with ThreadPoolExecutor(len(procs) or 1) as pool:
            result["sigint_exit"] = list(pool.map(drive.stop, procs))
        log("cycle", **result)
    return result


def spread(values: list) -> str:
    return f"median {statistics.median(values)}, range {min(values)}-{max(values)}"


def run(session: pytest.Session) -> None:
    cpu_engine, drive = load_cpu_engine()
    work = Path(tempfile.mkdtemp(prefix="w"))
    drive.write_model(work / "model")
    cpuinfo = Path("/proc/cpuinfo").read_text().splitlines()
    models = {line.split(":", 1)[1].strip() for line in cpuinfo if "model name" in line}
    m = meminfo()
    log(
        "runner",
        cpus=os.cpu_count(),
        cpu_model=sorted(models),
        mem_total_mib=m["MemTotal"],
        swap_total_mib=m["SwapTotal"],
        collected=session.testscollected,
        collection_errors=session.testsfailed,
        layout=LAYOUT,
        **in_use(),
        **process_rss(),
    )
    results = [cycle(n, cpu_engine, drive, work / "model", work) for n in range(CYCLES)]
    keys = ["ready_s", "serve_s", "failed", "peak_mem_used_mib", "peak_swap_used_mib"]
    for key in [*keys, "pytest_rss_mib"]:
        print(f"{key}: {spread([r[key] for r in results])}")
    for name in results[0]["uss"]:
        values = [r["uss"][name]["uss_median_mib"] for r in results]
        print(f"{name} uss_median_mib: {spread(values)}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> bool:
    """Run the driver in place of the collected tests; a driver error fails the run."""
    try:
        run(session)
    except Exception:  # noqa: BLE001 - report it and fail the session
        traceback.print_exc()
        sys.stdout.flush()
        session.testsfailed += 1
    return True
