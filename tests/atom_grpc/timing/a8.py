# SPDX-License-Identifier: MIT
"""`python -m tests.atom_grpc.timing.a8 --compass <tree>`, as run.sh runs it: the C1228 recipe on
fake engines under the AgentX trace, through atomesh's HTTP P/D path, its gRPC streams and the
Session, one at a time from this commit; A8's report and criteria."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

import psutil
import tomllib

from tests.atom_grpc.fake.record import RECORD
from tests.atom_grpc.fake.serve import COUNTS_ENV, LEAN_ENV
from tests.atom_grpc.harness.atomesh import ATOMESH_BIN, atomesh_bin
from tests.atom_grpc.harness.launch import core_sets, load
from tests.atom_grpc.harness.topologies import TOPOLOGIES, Topology
from tests.atom_grpc.timing import tools
from tests.atom_grpc.timing.a2t import LAW
from tests.atom_grpc.timing.a7t import stat_ticks

TOPOLOGY = TOPOLOGIES / "a8.toml"
FIXTURE = TOPOLOGIES.parent / "fixtures" / "deepseek_v3_mtp"
# The AgentX trace at revision 23f152f6 in HF's hub cache layout, and the recipe's model files.
TRACE = Path("/share_nfs/models/datasets--semianalysisai--cc-traces-weka-062126")
TOKENIZER = Path("/share_nfs/models/deepseek-ai/DeepSeek-V4.1-Flash")
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")
HARNESS = "1.0.9"  # agentx-harness, CI's
PATHS, GRPC_PATHS = ("http", "grpc", "session"), (
    "grpc",
    "session",
)  # a8.toml's routers
API_SERVERS = 4  # the HTTP path's --api-server-count
ATOMESH_CORES = AIPERF_CORES = 16
CONCURRENCY, WINDOW_S, SLICE_S = 1228, 1800, 300
SAMPLE_S = 5.0  # how often CPU ticks and atomesh's metrics are read
CPU_SHARE = 0.8  # of atomesh's cores, and of one core for each ATOM front-end process
CLOSE = 0.05  # an extra-TTFT percentile this near the HTTP path's: both paths run again
FLOOR, PROMPT_SPREAD = (
    0.95,
    0.02,
)  # last slice over first; mean prompt length over HTTP's
STAGES, ERRORS = "mesh_prepare_stage_seconds", "mesh_prepare_errors_total"
QUEUE_TIMEOUT = f'{ERRORS}{{reason="prepare_queue_timeout"}}'
SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "sec": 1.0, "s": 1.0}  # aiperf's units
# The recipe's aiperf environment.
AIPERF_ENV = {
    "AIPERF_DATASET_WEKA_LIVE_ASSISTANT_RESPONSES": "0",
    "AIPERF_HTTP_X_DYNAMO_SESSION_ID_FROM_CORRELATION_ID": "true",
    "AIPERF_HTTP_X_SESSION_ID_FROM_CORRELATION_ID": "true",
    "AIPERF_DATASET_CONFIGURATION_TIMEOUT": "1800",
    "AIPERF_SERVICE_PROFILE_CONFIGURE_TIMEOUT": "1800",
    "AIPERF_TIMING_CANCEL_DRAIN_TIMEOUT": "300",
    "AIPERF_HTTP_TCP_USER_TIMEOUT": "900000",
    "TOKENIZERS_PARALLELISM": "false",
    "OMP_NUM_THREADS": "4",
}


def aiperf_args(url: str, model: str, out: Path) -> list[str]:
    """The recipe's AgentX command against `url`, serving `model`."""
    return [
        "profile", "--scenario", "inferencex-agentx-mvp", "--url", url,
        "--endpoint", "/v1/chat/completions", "--endpoint-type", "chat", "--streaming",
        "--model", model, "--tokenizer", model, "--tokenizer-trust-remote-code",
        "--concurrency", str(CONCURRENCY), "--benchmark-duration", str(WINDOW_S),
        "--stats-interval", "30", "--random-seed", "42",
        "--failed-request-threshold", "0.10", "--trajectory-start-min-ratio", "0.25",
        "--trajectory-start-max-ratio", "0.75", "--warmup-requests-per-lane", "10",
        "--trace-idle-gap-cap-seconds", "300", "--agentic-warmup-grace-period", "1800",
        "--use-server-token-count", "--no-gpu-telemetry", "--num-dataset-entries", "393",
        "--slice-duration", "1.0", "--public-dataset", "semianalysis_cc_traces_weka_062126",
        "--output-artifact-dir", str(out), "--no-server-metrics",
    ]  # fmt: skip


def model_dir(tokenizer: Path, out: Path) -> Path:
    """The fixture's config with `tokenizer`'s files, vocabulary and context: aiperf writes
    AgentX's prompts with that tokenizer, so they keep their lengths here."""
    source = json.loads((tokenizer / "config.json").read_text())
    text = source.get("text_config", source)
    ids = {name: source[name] for name in ("bos_token_id", "eos_token_id")}
    config = json.loads((FIXTURE / "config.json").read_text()) | ids
    config["vocab_size"] = text["vocab_size"]
    config["max_position_embeddings"] = text["max_position_embeddings"]
    out.mkdir(parents=True)
    (out / "config.json").write_text(json.dumps(config))
    (out / "generation_config.json").write_text(json.dumps(ids))
    for name in TOKENIZER_FILES:
        shutil.copyfile(tokenizer / name, out / name)
    return out


def hub_cache(trace: Path, hub: Path) -> None:
    """A writable copy of the trace's hub cache, its blobs as files: `datasets` loads it
    online, downloading nothing, and writes beside it."""
    out = hub / trace.name
    for part in ("refs", "snapshots", ".no_exist"):
        shutil.copytree(trace / part, out / part, symlinks=True)
    (out / "blobs").mkdir()
    for blob in (trace / "blobs").iterdir():
        shutil.copyfile(blob, out / "blobs" / blob.name)


def path_topology(path: str, run: Path, model: Path, law: str) -> Path:
    """a8.toml for one path: its router alone, serving `model` under `law`, each instance
    recording leanly and counting its gRPC messages; HTTP's with four front ends."""
    with open(TOPOLOGY, "rb") as f:
        tables = tomllib.load(f)
    for spec in tables["instance"]:
        env = spec["fake"].get("env", {}) | {LEAN_ENV: "1"}
        env[COUNTS_ENV] = str(run / spec["name"] / "grpc_counts.jsonl")
        spec["fake"] = spec["fake"] | {"law": law, "env": env}
        spec["model"] = str(model)
        if path == "http":
            spec["grpc"] = False
            spec["args"] = [*spec["args"], "--api-server-count", str(API_SERVERS)]
    (router,) = [router for router in tables["router"] if router["name"] == path]
    router["args"] = [*router["args"], "--model-path", str(model)]
    tables["router"] = [router]
    out = run.with_suffix(".toml")
    out.write_text(tools.dumps(tables))
    return out


def plan_cores(cpus: list[int], specs) -> dict[str, list[int]]:
    """The instances' sets first, as `core_sets` takes them, then atomesh's and aiperf's;
    this driver keeps the rest."""
    pinned = sum(s.cores["front_end"] + s.dp * s.cores["rank"] for s in specs)
    if pinned + ATOMESH_CORES + AIPERF_CORES >= len(cpus):
        raise ValueError(f"{len(cpus)} CPUs leave none to the driver beside {pinned}")
    atomesh = pinned + ATOMESH_CORES
    return {
        "instances": cpus[:pinned],
        "atomesh": cpus[pinned:atomesh],
        "aiperf": cpus[atomesh : atomesh + AIPERF_CORES],
        "driver": cpus[atomesh + AIPERF_CORES :],
    }


def handshake_ports(specs) -> dict[str, int]:
    """Each P instance's Mooncake handshake port, by which its D receives name it."""
    ports = {}
    for spec in specs:
        kv = json.loads(spec.args[spec.args.index("--kv-transfer-config") + 1])
        if kv["kv_role"] == "kv_producer":
            ports[spec.name] = kv["handshake_port"]
    return ports


def pinned_atomesh(cores: list[int], scratch: Path) -> Path:
    """`ATOMESH_BIN`'s binary on `cores` from its first instruction, so that it sizes its
    runtime and preparation pool to them."""
    script = scratch / "atomesh"
    cpus = ",".join(map(str, cores))
    script.write_text(f'#!/bin/sh\nexec taskset -c {cpus} {atomesh_bin()} "$@"\n')
    script.chmod(0o755)
    return script


def front_ends(instance) -> list[int]:
    """The API server and the front-end workers it spawned, which keep its core set."""
    api, cores = psutil.Process(instance.pid), set(instance.cores["front_end"])
    workers = [
        child.pid
        for child in api.children(recursive=True)
        if set(child.cpu_affinity()) == cores
        and "resource_tracker" not in " ".join(child.cmdline())
    ]
    return [api.pid, *sorted(workers)]


def scrape(url: str) -> dict[str, float]:
    """atomesh's preparation errors and stage histograms, from its Prometheus text."""
    with urllib.request.urlopen(url, timeout=30) as reply:
        text = reply.read().decode()
    found = {}
    for line in text.splitlines():
        if line.startswith((STAGES, ERRORS)):
            name, value = line.rsplit(" ", 1)
            found[name] = float(value)
    return found


class Sampler(threading.Thread):
    """Reads each pid's CPU ticks and atomesh's metrics every `SAMPLE_S`, at wall time."""

    def __init__(self, pids: list[int], metrics_url: str):
        super().__init__(name="a8-sampler", daemon=True)
        self.pids, self.url = pids, metrics_url
        self.ticks: list[tuple[float, dict[int, int]]] = []
        self.metrics: list[tuple[float, dict[str, float]]] = []
        self.done = threading.Event()

    def run(self) -> None:
        while True:
            now, ticks = time.time(), {}
            for pid in self.pids:
                try:
                    ticks[pid] = stat_ticks(Path(f"/proc/{pid}/stat").read_text())[1]
                except FileNotFoundError:  # ended
                    pass
            self.ticks.append((now, ticks))
            self.metrics.append((now, scrape(self.url)))
            if self.done.wait(SAMPLE_S):
                return


def between(samples: list, start: float, end: float) -> tuple:
    """The first and the last (time, value) sample in [start, end]."""
    inside = [sample for sample in samples if start <= sample[0] <= end]
    if len(inside) < 2:
        raise ValueError(f"{len(inside)} samples in [{start}, {end}]")
    return inside[0], inside[-1]


def cpu_shares(ticks: list, start: float, end: float, hz: int) -> dict[int, float]:
    """Each pid's mean CPU over the window, in cores."""
    (t0, first), (t1, last) = between(ticks, start, end)
    return {pid: (last[pid] - first[pid]) / hz / (t1 - t0) for pid in first}


def stage_means(metrics: list, start: float, end: float) -> dict[str, tuple]:
    """Each preparation stage's (count, mean seconds) over the window."""
    (_, first), (_, last) = between(metrics, start, end)
    found = {}
    for name, value in last.items():
        if name.startswith(f"{STAGES}_count"):
            stage = name.split('stage="', 1)[1].split('"', 1)[0]
            total = name.replace("_count", "_sum", 1)
            n = value - first.get(name, 0.0)
            found[stage] = (n, (last[total] - first.get(total, 0.0)) / n if n else 0.0)
    return found


def grpc_messages(path: Path, start: float, end: float) -> dict[str, int]:
    """The gRPC messages an instance received and sent over the window."""
    text = path.read_text()
    totals = [json.loads(line) for line in text[: text.rfind("\n") + 1].splitlines()]
    (_, first), (_, last) = between([(t["time"], t) for t in totals], start, end)
    return {name: last[name] - first[name] for name in first if name != "time"}


class Client(NamedTuple):
    """One profiled request as aiperf recorded it, in wall seconds."""

    start: float
    end: float
    ttft: float | None
    itl: float | None
    isl: int | None
    osl: int | None
    outcome: str  # "ok", "cancelled", "refused" or its error's code and type


def _metric(metrics: dict, tag: str, unit: bool = False):
    metric = metrics.get(tag)
    if metric is None:
        return None
    return metric["value"] * (SECONDS[metric["unit"]] if unit else 1)


def clients(export: Path) -> list[Client]:
    """The profiling phase's records in aiperf's profile_export.jsonl."""
    found = []
    with open(export) as f:
        for line in f:
            record = json.loads(line)
            meta, metrics = record["metadata"], record.get("metrics") or {}
            if meta.get("benchmark_phase") != "profiling":
                continue
            outcome, error = "ok", record.get("error")
            if meta.get("was_cancelled"):
                outcome = "cancelled"
            elif meta.get("context_overflow_skip"):
                outcome = "refused"  # by ATOM, for its context length
            elif error:
                outcome = f"{error.get('code')} {error.get('type')}"
            found.append(
                Client(
                    meta["request_start_ns"] / 1e9,
                    meta["request_end_ns"] / 1e9,
                    _metric(metrics, "time_to_first_token", unit=True),
                    _metric(metrics, "inter_token_latency", unit=True),
                    _metric(metrics, "input_sequence_length"),
                    _metric(metrics, "output_sequence_length"),
                    outcome,
                )
            )
    return found


class Seq(NamedTuple):
    """One sequence of a lean engine record, in wall seconds."""

    admit: float
    prompt: int
    max_tokens: int
    kv: dict
    first: float | None  # its first step with ids
    last: float | None  # its step reaching max_tokens less one: a D's first id is P's


def _events(record: Path, kind: str):
    prefix = f'{{"kind": "{kind}"'
    with open(record) as f:
        for line in f:
            if line.startswith(prefix) and line.endswith("\n"):  # whole lines
                yield json.loads(line)


def engine_seqs(record: Path) -> dict[int, Seq]:
    """Each sequence of an instance's lean record, by seq id."""
    admits = {event["seq_id"]: event for event in _events(record, "admit")}
    first, last, count = {}, {}, Counter()
    for step in _events(record, "step"):
        for seq_id, ids in step["rows"]:
            if not ids or seq_id in last or seq_id not in admits:
                continue
            first.setdefault(seq_id, step["time"])
            count[seq_id] += len(ids)
            if count[seq_id] >= admits[seq_id]["sampling"]["max_tokens"] - 1:
                last[seq_id] = step["time"]
    return {
        seq_id: Seq(
            event["time"],
            event["num_prompt_tokens"],
            event["sampling"]["max_tokens"],
            event["kv_transfer_params"] or {},
            first.get(seq_id),
            last.get(seq_id),
        )
        for seq_id, event in admits.items()
    }


def extras(done: list[Client], prefills: dict, decodes: list[Seq]) -> tuple:
    """Each completed request's extra TTFT and ITL, its times less its engine times: its
    D sequence has its prompt and output lengths and was admitted between its start and
    first token, and P's is that D's by handshake port and transfer id. A request with
    none or several such Ds is left out: (extra TTFTs, extra ITLs, left out)."""
    by_lengths = defaultdict(list)
    for seq in decodes:
        by_lengths[seq.prompt, seq.max_tokens].append(seq)
    ttfts, itls, left = [], [], 0
    for client in done:
        found = [
            seq
            for seq in by_lengths[client.isl, client.osl]
            if client.start <= seq.admit <= client.start + client.ttft
        ]
        decode = found[0] if len(found) == 1 else None
        kv = decode.kv if decode else {}
        prefill = prefills.get((kv.get("remote_handshake_port"), kv.get("transfer_id")))
        if prefill is None or prefill.first is None or decode.first is None:
            left += 1
            continue
        engine = (prefill.first - prefill.admit) + (decode.first - decode.admit)
        ttfts.append(client.ttft - engine)
        if client.osl >= 2 and client.itl is not None and decode.last is not None:
            itls.append(client.itl - (decode.last - decode.first) / (client.osl - 1))
    return ttfts, itls, left


def percentile(values: list[float], p: int) -> float:
    """The nearest-rank percentile."""
    ranked = sorted(values)
    return ranked[max(0, -(-len(ranked) * p // 100) - 1)]


def slices(done: list[Client], start: float) -> list[float]:
    """Output tokens a second of the requests that ended in each slice of the window."""
    tokens = [0] * (WINDOW_S // SLICE_S)
    for client in done:
        index = int((client.end - start) // SLICE_S)
        if 0 <= index < len(tokens):
            tokens[index] += client.osl
    return [count / SLICE_S for count in tokens]


class Run(NamedTuple):
    """One path's run, measured over aiperf's profiling window."""

    path: str
    atomesh: float  # mean CPU, in cores
    stages: dict[str, tuple]  # preparation stage: (count, mean seconds)
    queue_timeouts: float  # requests that hit --prepare-queue-timeout-ms, whole run
    # "<instance> <k>", 0 its API server: mean CPU, in cores
    front_ends: dict[str, float]
    messages: dict[str, dict[str, int]]  # gRPC messages by instance
    ttft: tuple[float, float]  # extra, p50 and p90
    itl: tuple[float, float]
    completed: int
    left_out: int  # completed requests matched to no engine request
    slices: list[float]  # output tokens a second
    outcomes: Counter  # profiled requests by outcome
    prompt: float  # mean prompt tokens of completed requests


def measured(path: str, topology, sampler: Sampler, fronts: dict, export: Path) -> Run:
    """A finished run, from aiperf's records, the engine records and the samples."""
    profiled = clients(export)
    start = min(client.start for client in profiled)
    end = start + WINDOW_S
    done = [c for c in profiled if c.outcome == "ok" and c.ttft is not None]
    ports = handshake_ports(load(TOPOLOGY)[0])
    seqs = {
        name: engine_seqs(instance.workdir / RECORD)
        for name, instance in topology.instances.items()
    }
    prefills = {
        (port, seq_id): seq
        for name, port in ports.items()
        for seq_id, seq in seqs[name].items()
    }
    decodes = [seq for name in seqs if name not in ports for seq in seqs[name].values()]
    ttfts, itls, left = extras(done, prefills, decodes)
    shares = cpu_shares(sampler.ticks, start, end, os.sysconf("SC_CLK_TCK"))
    (router,) = topology.routers.values()
    messages = {}
    for name, instance in topology.instances.items():
        if instance.grpc is not None:
            counted = instance.workdir / "grpc_counts.jsonl"
            messages[name] = grpc_messages(counted, start, end)
    return Run(
        path,
        shares[router.pid],
        stage_means(sampler.metrics, start, end),
        sampler.metrics[-1][1].get(QUEUE_TIMEOUT, 0.0),
        {
            f"{name} {k}": shares[pid]
            for name, pids in fronts.items()
            for k, pid in enumerate(pids)
        },
        messages,
        (percentile(ttfts, 50), percentile(ttfts, 90)),
        (percentile(itls, 50), percentile(itls, 90)),
        len(done),
        left,
        slices(done, start),
        Counter(client.outcome for client in profiled),
        sum(client.isl for client in done) / len(done),
    )


def run_path(
    path: str, attempt: int, plan: dict, model: Path, law: str, scratch: Path
) -> Run:
    """One run of the recipe's AgentX load through `path`'s router."""
    run = scratch / f"{path}{attempt}"
    # The launcher pins its instances from these, and atomesh pins itself.
    os.sched_setaffinity(0, plan["instances"])
    topology = Topology(path_topology(path, run, model, law), run)
    os.sched_setaffinity(0, plan["driver"])
    sampler, out, code = None, run / "aiperf", None
    try:
        topology.wait_ready()
        (router,) = topology.routers.values()
        fronts = {name: front_ends(i) for name, i in topology.instances.items()}
        command = psutil.Process(router.pid).cmdline()
        metrics = f"http://127.0.0.1:{command[command.index('--prometheus-port') + 1]}"
        pids = [router.pid, *(pid for group in fronts.values() for pid in group)]
        sampler = Sampler(pids, f"{metrics}/metrics")
        sampler.start()
        hf = scratch / "hf"
        env = os.environ | AIPERF_ENV
        env |= {"HF_HUB_CACHE": str(hf / "hub"), "HF_HOME": str(hf / "home")}
        env["HF_DATASETS_CACHE"] = str(hf / "datasets")
        aiperf = ["taskset", "-c", ",".join(map(str, plan["aiperf"])), "agentx"]
        command = [*aiperf, *aiperf_args(router.url, str(model), out)]
        print(f"{path} run {attempt}: aiperf started {time.ctime()}", flush=True)
        with open(run / "aiperf.log", "w") as log:
            ran = subprocess.run(command, env=env, stdout=log, stderr=log, check=False)
            code = ran.returncode
    finally:
        if sampler is not None:
            sampler.done.set()
            sampler.join()
        left = topology.stop()
    if left or topology.proc.returncode:
        code = topology.proc.returncode
        raise RuntimeError(f"launch exit {code}, left {left}:\n{topology.tail()}")
    if not (out / "profile_export.jsonl").exists():
        tail = (run / "aiperf.log").read_text(errors="replace")[-8000:]
        raise RuntimeError(f"aiperf exit {code} wrote no records:\n{tail}")
    measure = measured(path, topology, sampler, fronts, out / "profile_export.jsonl")
    shutil.rmtree(run)  # its engine records: gigabytes of /dev/shm
    return measure


def ms(seconds: float) -> str:
    return f"{seconds * 1e3:.1f} ms"


def report(run: Run, attempt: int) -> list[str]:
    stages = ", ".join(
        f"{s} {ms(mean)} over {n:.0f}" for s, (n, mean) in run.stages.items()
    )
    fronts = ", ".join(f"{name} {share:.0%}" for name, share in run.front_ends.items())
    messages = ", ".join(
        f"{name} {m['generate_in'] + m['session_in']} in, "
        f"{m['generate_out'] + m['session_out']} out"
        for name, m in run.messages.items()
    )
    outcomes = ", ".join(f"{k} {n}" for k, n in run.outcomes.most_common())
    return [
        f"\n{run.path} run {attempt}:",
        f"  atomesh CPU {run.atomesh / ATOMESH_CORES:.1%} of its {ATOMESH_CORES} cores;"
        + f" prepare stages: {stages or 'none'};"
        + f" prepare-queue timeouts {run.queue_timeouts:.0f}",
        f"  ATOM front-end processes, CPU of one core: {fronts}",
        f"  gRPC messages: {messages or 'none'}",
        f"  extra TTFT p50 {ms(run.ttft[0])}, p90 {ms(run.ttft[1])}; extra ITL p50"
        + f" {ms(run.itl[0])}, p90 {ms(run.itl[1])}; over {run.completed} completed,"
        + f" {run.left_out} matching no engine request",
        f"  output tokens/s per {SLICE_S} s: "
        + ", ".join(f"{s:.0f}" for s in run.slices),
        f"  requests: {outcomes}; mean prompt {run.prompt:.0f} tokens",
    ]


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values)


def verdicts(runs: dict[str, list[Run]]) -> list[tuple[str, bool]]:
    """A8's criteria on both gRPC paths: each run's, then extra TTFT over the runs' mean."""
    http = runs["http"]
    prompt = mean(r.prompt for r in http)
    found = []
    for path in GRPC_PATHS:
        for k, r in enumerate(runs[path], 1):
            name = f"{path} run {k}"
            atomesh = r.atomesh < CPU_SHARE * ATOMESH_CORES and r.queue_timeouts == 0
            refused = set(r.outcomes) <= {"ok", "refused"}
            found += [
                (f"{name}: atomesh's CPU and its prepare queue", atomesh),
                (f"{name}: each ATOM front-end process's CPU", max(r.front_ends.values()) < CPU_SHARE),
                (f"{name}: the last slice's throughput", r.slices[-1] >= FLOOR * r.slices[0]),
                (f"{name}: no failure or cancel but ATOM's refusals", refused),
                (f"{name}: mean prompt length", abs(r.prompt - prompt) <= PROMPT_SPREAD * prompt),
            ]  # fmt: skip
        for i, p in enumerate((50, 90)):
            mine = mean(r.ttft[i] for r in runs[path])
            theirs = mean(r.ttft[i] for r in http)
            text = f"{path}: extra TTFT p{p} {ms(mine)} below HTTP's {ms(theirs)}"
            runs_ = f" (means of {len(runs[path])} and {len(http)} runs)"
            found.append((text + runs_, mine < theirs))
    return found


def close(grpc: Run, http: Run) -> bool:
    """Whether an extra-TTFT percentile of `grpc` is within `CLOSE` of HTTP's."""
    return any(abs(g - h) <= CLOSE * abs(h) for g, h in zip(grpc.ttft, http.ttft))


def measure(law: str, scratch: Path) -> int:
    """The case: 0 when every criterion holds on both gRPC paths, 1 when one does not, 2
    when the run is refused."""
    specs, _ = load(TOPOLOGY)
    cpus = sorted(os.sched_getaffinity(0))
    plan = plan_cores(cpus, specs)
    cores = {"container": cpus}
    for name, sets in core_sets(specs, plan["instances"]).items():
        cores[f"{name} front end"] = sets["front_end"]
        cores |= {f"{name} rank {r}": rank for r, rank in enumerate(sets["ranks"])}
    cores |= {name: plan[name] for name in ("atomesh", "aiperf", "driver")}
    load_average = tools.load_average(Path("/proc/loadavg").read_text())
    kthreads = int(os.environ.get(tools.KTHREADS_ENV, "0"))
    print("\n".join(tools.header("a8", load_average, kthreads, cores)), flush=True)
    if tools.refuses(load_average, kthreads):
        print(f"refused: the load less those threads is {tools.MAX_LOAD:g} or more")
        return 2
    version = subprocess.run(
        ["agentx", "--version"], capture_output=True, text=True, check=False
    )
    if version.stdout.strip() != HARNESS:
        print(f"refused: agentx-harness {version.stdout.strip()!r}, not {HARNESS}")
        return 2
    os.sched_setaffinity(0, plan["driver"])
    model = model_dir(TOKENIZER, scratch / "model")
    hub_cache(TRACE, scratch / "hf" / "hub")
    os.environ[ATOMESH_BIN] = str(pinned_atomesh(plan["atomesh"], scratch))
    runs = defaultdict(list)

    def run(path: str) -> None:
        runs[path].append(
            run_path(path, len(runs[path]) + 1, plan, model, law, scratch)
        )
        print("\n".join(report(runs[path][-1], len(runs[path]))), flush=True)

    for path in PATHS:
        run(path)
    again = [path for path in GRPC_PATHS if close(runs[path][0], runs["http"][0])]
    for path in ["http", *again] if again else []:
        run(path)
    print()
    found = verdicts(runs)
    for text, ok in found:
        print(f"{text}: {'pass' if ok else 'FAIL'}")
    ok = all(ok for _, ok in found)
    print(f"\n{'pass' if ok else 'FAIL'}")
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compass", type=Path, required=True, help="jgong5/ATOM tree")
    law = str(parser.parse_args().compass.resolve() / LAW)
    return measure(law, Path(tempfile.mkdtemp(prefix="a8-")))


if __name__ == "__main__":
    sys.exit(main())
