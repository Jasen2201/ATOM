# SPDX-License-Identifier: MIT
"""What the timing cases share: the load average a run refuses at, the report header, a
topology file under a step law, and a metric's median held to its prediction."""

import json
import os
import socket
import statistics
from pathlib import Path
from typing import NamedTuple

import tomllib

from tests.atom_grpc.harness.atomesh import atomesh_bin

# A run refuses to start at this 1-minute load average or above, less the host's kernel
# threads in uninterruptible sleep: each counts in the load average and runs nothing.
MAX_LOAD = 32.0
KTHREADS_ENV = "ATOM_TIMING_D_KTHREADS"  # run.sh's count of those threads on the host
IMAGE_ENV = "ATOM_TIMING_IMAGE"  # run.sh's image: its name and id
COMMIT_ENV = "ATOM_TIMING_COMMIT"  # run.sh's tree: its commit, and whether it is dirty


def load_average(loadavg: str) -> float:
    """The 1-minute load average in the text of /proc/loadavg."""
    return float(loadavg.split()[0])


def refuses(load: float, kthreads: int) -> bool:
    return load - kthreads >= MAX_LOAD


def header(case: str, load: float, kthreads: int, cores: dict) -> list[str]:
    """A report's first lines: the image, commit, load average at start and cores."""
    sleeping = f"less {kthreads} kernel threads in uninterruptible sleep"
    lines = [
        f"timing case {case} on {socket.gethostname()}",
        f"image: {os.environ.get(IMAGE_ENV, 'unknown')}",
        f"commit: {os.environ.get(COMMIT_ENV, 'unknown')}",
        f"load average (1 min) at start: {load:.2f}, {sleeping}: {load - kthreads:.2f};"
        + f" a run refuses at {MAX_LOAD:g}",
    ]
    return lines + [f"cores, {name}: {sorted(cpus)}" for name, cpus in cores.items()]


def pinned_atomesh(cores: list[int], scratch: Path) -> Path:
    """`ATOMESH_BIN`'s binary on `cores` from its first instruction, so that it sizes its
    runtime and preparation pool to them."""
    script = scratch / "atomesh"
    cpus = ",".join(map(str, cores))
    script.write_text(f'#!/bin/sh\nexec taskset -c {cpus} {atomesh_bin()} "$@"\n')
    script.chmod(0o755)
    return script


def _toml(value) -> str:
    if isinstance(value, dict):
        pairs = (f"{_toml(key)} = {_toml(item)}" for key, item in value.items())
        return "{ " + ", ".join(pairs) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return json.dumps(value)  # a JSON string, number or boolean reads alike in TOML


def dumps(tables: dict) -> str:
    """A topology's tables, arrays of `[[name]]` tables, as TOML text."""
    return "".join(
        f"[[{name}]]\n"
        + "".join(f"{_toml(k)} = {_toml(v)}\n" for k, v in table.items())
        for name, array in tables.items()
        for table in array
    )


def under_law(topology: Path, law: str) -> str:
    """`topology`'s TOML with each instance's steps priced by the law file `law` and its
    model path absolute, so that the text serves from any directory."""
    with open(topology, "rb") as f:
        tables = tomllib.load(f)
    for spec in tables["instance"]:
        spec["model"] = str((topology.parent / spec["model"]).resolve())
        spec["fake"] = {**spec["fake"], "law": law}
    return dumps(tables)


def client_times(sent: float, chunks: list[tuple[float, int]]) -> tuple[float, float]:
    """TTFT and ITL as a client saw one stream of (arrival, tokens) chunks: the first
    chunk's delay, then the time from it to the last over the tokens after it."""
    (first, _), (last, _) = chunks[0], chunks[-1]
    return first - sent, (last - first) / sum(tokens for _, tokens in chunks[1:])


def law_times(steps: list, tokens: list[int], host: float) -> tuple[float, float]:
    """TTFT and ITL as `client_times` takes them, from one request's priced `steps`,
    (price, samples) in order, its chunks of `tokens`, and `host`, seconds TTFT adds."""
    first = next(i for i, (_, samples) in enumerate(steps) if samples)
    later = [price for price, samples in steps[first + 1 :] if samples]
    if len(later) < len(tokens) - 1:
        raise ValueError(f"{len(tokens)} chunks, {len(later) + 1} sampling steps")
    ttft = host + sum(price for price, _ in steps[: first + 1])
    return ttft, sum(later[: len(tokens) - 1]) / sum(tokens[1:])


class Verdict(NamedTuple):
    measured: float  # the median over the runs
    predicted: float  # the median of the runs' predictions
    error: float  # measured over predicted, less one
    ok: bool  # the error is within the bound


def verdict(measured: list[float], predicted: list[float], bound: float) -> Verdict:
    median, law = statistics.median(measured), statistics.median(predicted)
    error = median / law - 1
    return Verdict(median, law, error, abs(error) <= bound)
