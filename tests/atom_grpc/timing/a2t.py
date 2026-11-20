# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.timing.a2t --compass <tree>`, as run.sh runs it: single
requests over gRPC Generate, their median TTFT and ITL held to Compass's prediction."""

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import grpc
import psutil

from atom.entrypoints.grpc import atom_grpc_pb2 as pb
from atom.entrypoints.grpc import atom_grpc_pb2_grpc as pb_grpc
from atom.model_engine.zmq_protocol import engine_v1_pb2 as v1
from tests.atom_grpc.fake.record import RECORD, EngineRecord
from tests.atom_grpc.harness.launch import core_sets, load
from tests.atom_grpc.harness.topologies import TOPOLOGIES, Topology
from tests.atom_grpc.timing import tools

LAW = "scripts/compass/laws/deepseek-v4.1-flash-tp4-ep4-dspark5-mi355x-gpu40.json.xz"
PROMPTS = (1024, 8192, 32768)  # prompt tokens
RUNS = 5  # requests of each prompt length, one at a time
OUTPUT = 256  # output tokens of every request
BOUND = 0.05  # how far a median may be from its prediction, relative to it
# Deferred output runs one step past a request's last token; a pause before each request
# lets it end, so that every request starts on an idle engine.
IDLE_S = 0.1
PRICES = Path(__file__).with_name("compass_price.py")


def prompt(length: int, seed: int, vocab: int) -> list[int]:
    """Seeded random ids: no two prompts here share a first block for prefix caching."""
    rng = random.Random(f"{length}/{seed}")
    return [rng.randrange(vocab) for _ in range(length)]


def send(stub, ids: list[int]) -> tuple[float, list[tuple[float, list[int]]]]:
    """One streamed Generate of `ids`: when it was sent, and each chunk's arrival and
    ids."""
    request = pb.GenerateRequest(
        request_id=f"a2t-{uuid.uuid4().hex}",
        prompt_token_ids=ids,
        sampling=v1.SamplingParams(max_tokens=OUTPUT, temperature=0, ignore_eos=True),
        stream=True,
    )
    chunks = []
    sent = time.perf_counter()
    for message in stub.Generate(request, timeout=600):
        kind = message.WhichOneof("response")
        if kind == "chunk":
            chunks.append((time.perf_counter(), list(message.chunk.token_ids)))
        elif kind == "error":
            raise RuntimeError(f"Generate refused a {len(ids)}-token prompt: {message}")
    return sent, chunks


def compass_prices(compass: Path, record: Path, out: Path) -> dict:
    """Compass's price of each step in `record`, and the law's host hops, from a process
    with only `compass` on its path."""
    command = [sys.executable, str(PRICES), str(compass / LAW), str(record), str(out)]
    subprocess.run(command, env={**os.environ, "PYTHONPATH": str(compass)}, check=True)
    return json.loads(out.read_text())


def measure(compass: Path, scratch: Path) -> int:
    """The case: 0 when every median is within its bound, 1 when one is not, 2 when the
    load average refuses the run."""
    single = scratch / "single.toml"
    single.write_text(tools.under_law(TOPOLOGIES / "single.toml", str(compass / LAW)))
    specs, _ = load(single)
    *launcher, mesh, client = sorted(os.sched_getaffinity(0))
    cores = {"container": [*launcher, mesh, client]}
    for name, sets in core_sets(specs, launcher).items():
        if sets is None:
            raise ValueError(f"the timing topology leaves {name} unpinned")
        cores[f"{name} front end"] = sets["front_end"]
        cores |= {f"{name} rank {r}": cpus for r, cpus in enumerate(sets["ranks"])}
    cores |= {"atomesh": [mesh], "client": [client]}
    load_average = tools.load_average(Path("/proc/loadavg").read_text())
    print("\n".join(tools.header("a2t", load_average, cores)), flush=True)
    if tools.refuses(load_average):
        print(f"refused: the load average is {tools.MAX_LOAD:g} or more")
        return 2

    os.sched_setaffinity(0, launcher)  # inherited by the launcher, which pins from it
    topology = Topology(single, scratch / "topology")
    os.sched_setaffinity(0, [client])
    runs = []  # (prompt ids, when sent, chunks)
    try:
        topology.wait_ready()
        for router in topology.routers.values():  # up, as the topology has it, but idle
            for thread in psutil.Process(router.pid).threads():
                os.sched_setaffinity(thread.id, [mesh])
        (instance,) = topology.instances.values()
        config = json.loads((Path(specs[0].model) / "config.json").read_text())
        with grpc.insecure_channel(instance.grpc) as channel:
            stub = pb_grpc.AtomEngineStub(channel)
            send(stub, prompt(PROMPTS[0], RUNS, config["vocab_size"]))  # a warm-up
            for length in PROMPTS:
                for seed in range(RUNS):
                    ids = prompt(length, seed, config["vocab_size"])
                    time.sleep(IDLE_S)
                    runs.append((ids, *send(stub, ids)))
    finally:
        left = topology.stop()
    if left or topology.proc.returncode:
        code = topology.proc.returncode
        raise RuntimeError(f"launch exit {code}, left {left}:\n{topology.tail()}")

    record = EngineRecord(instance.workdir / RECORD)
    priced = compass_prices(compass, instance.workdir / RECORD, scratch / "prices.json")
    prices, host = priced["steps"], sum(priced["hops"].values())
    if len(prices) != len(record.forwards):
        raise RuntimeError(f"{len(prices)} prices for {len(record.forwards)} steps")
    times = {length: ([], []) for length in PROMPTS}  # measured, then predicted
    for ids, sent, chunks in runs:
        (request,) = record.with_prompt(ids)
        sent_ids = [chunk for _, chunk in chunks]
        step_ids = [s.ids[: len(c)] for s, c in zip(request.steps, sent_ids)]
        if step_ids != sent_ids or sum(map(len, sent_ids)) != OUTPUT:
            raise RuntimeError(
                f"seq {request.seq_id}: its chunks are not its steps' ids"
            )
        steps = [
            (price, step.samples)
            for step, price in zip(record.forwards, prices)
            if any(row[0] == request.seq_id for row in step.requests)
        ]
        tokens = [(arrival, len(chunk)) for arrival, chunk in chunks]
        measured, predicted = times[len(ids)]
        measured.append(tools.client_times(sent, tokens))
        predicted.append(tools.law_times(steps, [n for _, n in tokens], host))

    ok = True
    hops = ", ".join(f"{hop} {s * 1e3:.3f}" for hop, s in priced["hops"].items())
    print(f"\nTTFT's prediction adds the law's host hops, {host * 1e3:.3f} ms: {hops}")
    print(f"{'prompt':>6} metric {'median':>10} {'law':>10}  error  runs (ms)")
    for length, (measured, predicted) in times.items():
        for i, metric in enumerate(("TTFT", "ITL")):
            ms = [m[i] for m in measured]
            v = tools.verdict(ms, [p[i] for p in predicted], BOUND)
            ok &= v.ok
            each = " ".join(f"{t * 1e3:.3f}" for t in ms)
            print(
                f"{length:>6} {metric:<6} {v.measured * 1e3:>7.3f} ms "
                f"{v.predicted * 1e3:>7.3f} ms {v.error:+6.2%}  {each}"
            )
    verdict = "pass" if ok else "FAIL"
    print(f"\n{verdict}: each median within {BOUND:.0%} of Compass's prediction")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compass", type=Path, required=True, help="jgong5/ATOM tree")
    compass = parser.parse_args().compass.resolve()
    return measure(compass, Path(tempfile.mkdtemp(prefix="a2t-")))


if __name__ == "__main__":
    sys.exit(main())
