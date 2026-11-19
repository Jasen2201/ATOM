"""Start each configuration of #9 on the fake base, serve one completion (and a
chunked receive where asked), and table what the stand-in and the stub added."""

import argparse
import importlib.util
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
PR6 = "d7d96c7466329f79b71f10103473787e67eb4437"  # refs/pull/6/head
PROMPT = [72, 101, 108, 108, 111, 32, 119, 111, 114, 108, 100]
# The recipe's P and D model command, less --kv-transfer-config and host addresses.
SHARED = [
    *("--trust-remote-code", "--data-parallel-master-ip", "127.0.0.1"),
    *("--moe-backend", "standard", "--all2all-backend", "none"),
    *("--kv_cache_dtype", "bf16", "--index-cache-dtype", "fp8"),
    *("--enable_prefix_caching", "--block-size", "128"),
    *("--state-checkpoint-interval-tokens", "8192"),
    *("--level", "3", "--cudagraph-mode", "FULL"),
    *("--tool-call-parser", "dsml_v41", "--disable-uvicorn-access-log"),
]
SPEC = ["--method", "dspark", "--num-speculative-tokens", "5"]
SPEC += ["--dspark-config", '{"confidence_schedule":false}']
P = ["--gpu-memory-utilization", "0.85", "--max-num-seqs", "64"]
P += ["--max-num-batched-tokens", "32768", "--attn-prefill-chunk-size", "65536"]
P += ["--cudagraph-capture-sizes", "[1,2,4,8,16,24,32,40,48,64]"]
D = ["--gpu-memory-utilization", "0.75", "--max-num-seqs", "24"]
D += ["--max-num-batched-tokens", "1024", "--attn-prefill-chunk-size", "16384"]
D += ["--cudagraph-capture-sizes", "[1,2,4,8,16,24]"]
P_TP, D_TP = ["-tp", "2", "--enable-dp-attention"], [
    "-tp",
    "4",
    "--enable-dp-attention",
]
SHARE, LEAST = ["--dp-load-balance", "share_cache"], [
    "--dp-load-balance",
    "least_requests",
]
PP2, DCP2 = ["-pp", "2"], ["-tp", "2", "-dcp", "2"]
# The recipe's exports, less the GPU and NIC ones.
ENV = {
    "ATOM_HOST_IP": "127.0.0.1",
    "ATOM_MONO_ENABLE": "1",
    "ATOM_FRONTEND_PREFIX_HASH": "1",
    "ATOM_DP_SESSION_AFFINITY": "0",
    "MC_NUM_QP_PER_EP": "1",
    "ATOM_MOONCAKE_MATCHED_RAILS": "auto",
    "ATOM_DISABLE_MORI_EP": "1",
    "ATOM_USE_CUSTOM_ALL_GATHER": "0",
    "ATOM_DSPARK_DISABLE_COMPILE": "1",
    "ATOM_DISABLE_MMAP": "true",
    "PYTHONUNBUFFERED": "1",
    "NCCL_DEBUG": "WARN",
    "MC_LOG_LEVEL": "INFO",
    "NCCL_IB_DISABLE": "1",
    "MOONCAKE_DISABLE_HIP_DMABUF": "1",
    "HSA_ENABLE_IPC_MODE_LEGACY": "1",
}
P_ENV = {k: "4" for k in ("OFFLOAD_LOAD_WORKERS", "OFFLOAD_COPY_WORKERS")}
P_ENV["OFFLOAD_MIN_LOAD_TOKENS"] = "512"
# The recipe's D --kv-transfer-config; the ports are filled per run.
CONSUMER = {"kv_connector": "mooncake", "kv_role": "kv_consumer"}
CONSUMER |= {"proxy_ip": "127.0.0.1", "protocol": "rdma"}
# What AtomAdapter sends D for a chunked receive from the recipe's P (TP 1, DP 2).
CHUNKED = {"remote_host": "127.0.0.1", "remote_handshake_port": 1}
CHUNKED |= {"remote_pp_size": 1, "block_size": 128, "dcp_size": 1}
CHUNKED |= {"chunked_transfer": True, "do_remote_prefill": True}
CHUNKED |= {"do_remote_decode": False, "transfer_id": "xfer-spike-9"}
CHUNKED |= {"remote_dp_size": 2, "remote_tp_size": 1, "remote_dp_rank": 0}


def config(what, flags, fixture="dsv3", env=None, chunked=False, consumer=False):
    """One configuration; `consumer` adds the recipe's D --kv-transfer-config."""
    return {
        "what": what,
        "flags": flags,
        "fixture": fixture,
        "env": env or {},
        "chunked": chunked,
        "consumer": consumer,
    }


CONFIGS = {
    "1": config("the recipe's P", SHARED + SPEC + P_TP + SHARE + P, env=P_ENV),
    "1a": config(
        "1 with least_requests for share_cache",
        SHARED + SPEC + P_TP + LEAST + P,
        env=P_ENV,
    ),
    "2": config("the recipe's D", SHARED + SPEC + D_TP + LEAST + D),
    "3": config("2 with -pp 2", SHARED + SPEC + D_TP + PP2 + LEAST + D, chunked=True),
    "3a": config("3 without dspark", SHARED + D_TP + PP2 + LEAST + D, chunked=True),
    "3b": config(
        "3a without -tp 4 --enable-dp-attention",
        SHARED + PP2 + LEAST + D,
        chunked=True,
    ),
    "3c": config(
        "3b with the recipe's D --kv-transfer-config",
        SHARED + PP2 + LEAST + D,
        chunked=True,
        consumer=True,
    ),
    "4": config(
        "2 with -tp 2 -dcp 2 for -tp 4 --enable-dp-attention, on Qwen3-Next",
        SHARED + SPEC + DCP2 + LEAST + D,
        fixture="qwen3_next",
        chunked=True,
    ),
}
EXCEPTION = re.compile(r"^([\w.]*(Error|Exception)|AssertionError)\b.*")
FRAME = re.compile(r'^\s*File ".*?/(atom/[^"]+)", line \d+, in (\S+)')


def load_pr6(work: Path):
    """#6's spike/cpu_engine at refs/pull/6/head, and its drive.py as a module."""
    try:
        tar = subprocess.run(
            ["git", "-C", ROOT, "archive", PR6, "spike/cpu_engine"],
            check=True,
            capture_output=True,
        ).stdout
    except subprocess.CalledProcessError:
        sys.exit(f"{PR6} is missing: git fetch <fork remote> refs/pull/6/head")
    subprocess.run(["tar", "-x", "-C", work], input=tar, check=True)
    base = work / "spike" / "cpu_engine"
    sys.path[:0] = [str(base / "pkgs"), str(base / "aiter_pkg")]
    import aiter  # noqa: F401  #6's names only, to tell this spike's apart

    spec = importlib.util.spec_from_file_location("pr6_drive", base / "drive.py")
    pr6 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pr6)
    return base, pr6


def write_fixtures(work: Path, pr6) -> None:
    """#6's DeepSeek-V3 fixture with an MTP head, and a Qwen3-Next one beside it."""
    pr6.write_model(work / "dsv3")
    pr6.write_model(work / "qwen3_next")
    qwen = {**pr6.MODEL, "architectures": ["Qwen3NextForCausalLM"]}
    qwen["model_type"] = "qwen3_next"
    (work / "qwen3_next" / "config.json").write_text(json.dumps(qwen))


def wait_ready(proc, base: str, start: float, timeout: float, log: Path):
    """Seconds to the first /health 200, or None: the server exited, timed out, or
    went 30 s without one after a child logged an exception (it waits forever)."""
    raised = None
    while proc.poll() is None and time.monotonic() - start < timeout:
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                return time.monotonic() - start
        except requests.RequestException:
            pass
        if raised is None and any(map(EXCEPTION.match, log.read_text().split("\n"))):
            raised = time.monotonic()
        if raised is not None and time.monotonic() - raised > 30:
            return None
        time.sleep(0.2)
    return None


def complete(base: str, kv_transfer_params=None, timeout: float = 120) -> str:
    body = {"prompt_token_ids": PROMPT, "max_tokens": 16, "temperature": 0}
    if kv_transfer_params:
        body["kv_transfer_params"] = kv_transfer_params
    try:
        reply = requests.post(f"{base}/v1/completions", json=body, timeout=timeout)
    except requests.Timeout:
        return f"no answer in {timeout:.0f} s"
    except requests.RequestException as error:
        return f"no answer: {type(error).__name__}"
    if reply.status_code != 200:
        return f"{reply.status_code} {reply.text[:200]}"
    choice, usage = reply.json()["choices"][0], reply.json()["usage"]
    return f"200, {choice['finish_reason']}, {usage['completion_tokens']} tokens"


def lines(path: Path) -> list[list[str]]:
    text = path.read_text() if path.exists() else ""
    return [line.split("\t") for line in text.splitlines()]


def first_failure(log: Path, rpcs: Path) -> str | None:
    """The first exception with its last two ATOM frames, else an RPC the stub
    lacks, else a never-scheduled warning."""
    text = log.read_text(errors="replace").splitlines()
    found = []
    for i, line in enumerate(text):
        if EXCEPTION.match(line):
            tb = [j for j in range(i) if text[j].startswith("Traceback")] or [0]
            frames = [
                f"{m[1]}: {m[2]}" for m in map(FRAME.match, text[tb[-1] : i]) if m
            ]
            found.append(f"{line[:240]} (at {', '.join(frames[-2:])})")
    found += [
        f"waits on {r[1]} ({r[2]}), which the stub lacks" for r in lines(rpcs) if r[3]
    ]
    found += [t[t.index("Request") :] for t in text if "will never be scheduled" in t]
    return found[0] if found else None


def additions(names: Path, rpcs: Path) -> dict:
    """Each name read that #6 does not define, or stub addition used, with its readers."""
    callers = defaultdict(set)
    for _process, rpc, caller, _lacks in lines(rpcs):
        callers[rpc].add(caller)
    added = {}
    for _process, name, kind, what, reader in lines(names):
        module, _, attr = name.rpartition(".")
        if hasattr(sys.modules.get(module), attr):
            continue  # one of #6's names
        readers = callers[attr] if module == "Stub" else {reader}
        row = added.setdefault(name, {"kind": kind, "what": what, "readers": set()})
        row["readers"] |= readers
    return {n: {**r, "readers": sorted(r["readers"])} for n, r in sorted(added.items())}


def run_once(label, base6, pr6, work, run, skip, ready_timeout) -> dict:
    cfg = CONFIGS[label]
    tag = f"{label}-{run}"
    names, rpcs, log = (work / f"{k}-{tag}.log" for k in ("names", "rpc", "server"))
    paths = [base6 / "pkgs", base6 / "aiter_pkg", ROOT]
    env = dict(os.environ, **ENV, **cfg["env"], AITER_LOG_LEVEL="WARNING")
    env |= {"PYTHONPATH": ":".join(map(str, paths)), "SPIKE_SKIP": ",".join(skip)}
    env |= {"SPIKE_NAME_LOG": str(names), "SPIKE_RPC_LOG": str(rpcs)}
    env["TMPDIR"] = tempfile.mkdtemp(prefix="i")  # short: ZMQ ipc paths live here
    http = pr6.free_port()
    cmd = [sys.executable, "-m", "spike.fake_configs.serve"]
    cmd += ["--model", work / cfg["fixture"], "--server-port", http]
    cmd += ["--data-parallel-master-port", pr6.free_port()]
    cmd += ["--data-parallel-base-port", pr6.free_port(), *cfg["flags"]]
    if cfg["consumer"]:
        kv = {**CONSUMER, "http_port": http, "handshake_port": pr6.free_port()}
        cmd += ["--kv-transfer-config", json.dumps(kv)]
    result = {"config": label, "run": run, "load1": round(os.getloadavg()[0], 1)}
    base = f"http://127.0.0.1:{http}"
    with open(log, "w") as out:
        start = time.monotonic()
        proc = subprocess.Popen(
            [str(c) for c in cmd],
            cwd=ROOT,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:  # each failure is named before the shutdown adds its own lines
            ready = wait_ready(proc, base, start, ready_timeout, log)
            if ready is None:
                late = f"no /health 200 in {ready_timeout:.0f} s"
                result["started"] = first_failure(log, rpcs) or late
            else:
                result |= {"started": "yes", "ready_s": round(ready, 1)}
                result["served"] = complete(base)
                if not result["served"].startswith("200"):
                    result["served"] += f"; {first_failure(log, rpcs)}"
                if cfg["chunked"]:
                    result["chunked"] = complete(base, CHUNKED, timeout=60)
        finally:
            result["exit"] = pr6.stop(proc)
    result["read"] = sorted({row[1] for row in lines(names)})
    result["added"] = additions(names, rpcs)
    return result


def agree(values: list[str]) -> str:
    """The value when every run agrees, else each value with its count."""
    counts = Counter(values)
    if len(counts) == 1:
        return values[0]
    return " / ".join(f"{v} ({n} runs)" for v, n in counts.items())


def table(results: list[dict]) -> list[str]:
    out = ["| config | started, or its first failure | served | chunked receive "]
    out[0] += "| added: name, kind, what it does, read by |"
    out.append("|---|---|---|---|---|")
    for label, cfg in CONFIGS.items():
        runs = [r for r in results if r["config"] == label]
        if not runs:
            continue
        started = agree([r["started"] for r in runs])
        if started == "yes":
            ready = [r["ready_s"] for r in runs]
            started += f", /health 200 after {statistics.median(ready)} s"
            started += f" ({min(ready)}-{max(ready)}, {len(runs)} runs)"
        added = agree(
            [
                "<br>".join(
                    f"`{n}`: {a['kind']}, {a['what']}; {', '.join(a['readers'])}"
                    for n, a in r["added"].items()
                )
                or "none"
                for r in runs
            ]
        )
        cells = [f"{label}: {cfg['what']}", started]
        cells += [agree([r.get(k, "-") for r in runs]) for k in ("served", "chunked")]
        out.append("| " + " | ".join([*cells, added]).replace("\n", " ") + " |")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--configs", nargs="+", default=list(CONFIGS))
    parser.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="additions to leave out: get_gfx, flush_pp_send, state_slots",
    )
    parser.add_argument("--ready-timeout", type=float, default=1200)
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="w"))
    base6, pr6 = load_pr6(work)
    write_fixtures(work, pr6)
    results = []
    for run in range(args.runs):
        for label in args.configs:
            results.append(
                run_once(label, base6, pr6, work, run, args.skip, args.ready_timeout)
            )
            print(json.dumps(results[-1]), flush=True)
    print("\n".join(table(results)))
    print(f"work dir: {work}")


if __name__ == "__main__":
    main()
