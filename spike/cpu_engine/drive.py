"""Serve one /v1/completions request from ATOM's own API server on CPU, through
the stand-ins and the stub runner; print the stand-in names read, USS and readiness."""

import argparse
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import psutil
import requests
from tokenizers import Tokenizer, decoders, models, pre_tokenizers
from transformers import PreTrainedTokenizerFast

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
# Each case's flags and the stand-in packages on its PYTHONPATH: (ii) needs no triton.
CASES = {
    "mtp": (["--method", "mtp", "--num-speculative-tokens", "5"], ["aiter", "triton"]),
    "dp2": (["-tp", "2", "--enable-dp-attention"], ["aiter"]),
}
PROMPT = [72, 101, 108, 108, 111, 32, 119, 111, 114, 108, 100]
BOS, EOS = 256, 257
# DeepSeek-V3 with an MTP head; transformers' defaults fill the rest, as no weights load.
MODEL = {
    "architectures": ["DeepseekV3ForCausalLM"],
    "model_type": "deepseek_v3",
    "num_nextn_predict_layers": 1,
    "vocab_size": 258,
    "max_position_embeddings": 4096,
    "bos_token_id": BOS,
    "eos_token_id": EOS,
}


def _byte_chars() -> dict[int, str]:
    """GPT-2's byte-to-character table, so token id == byte value."""
    printable = [*range(33, 127), *range(161, 173), *range(174, 256)]
    chars = {b: chr(b) for b in printable}
    extra = (b for b in range(256) if b not in chars)
    chars.update({b: chr(256 + i) for i, b in enumerate(extra)})
    return chars


def write_model(path: Path) -> None:
    """The config above and a byte-level tokenizer, with no weights."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps(MODEL))
    generation = {"bos_token_id": BOS, "eos_token_id": EOS}
    (path / "generation_config.json").write_text(json.dumps(generation))
    vocab = {ch: b for b, ch in _byte_chars().items()}
    tok = Tokenizer(models.BPE(vocab=vocab, merges=[]))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    tok.add_special_tokens(["<s>", "</s>"])
    fast = PreTrainedTokenizerFast(
        tokenizer_object=tok, bos_token="<s>", eos_token="</s>"
    )
    fast.save_pretrained(path)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def role(proc: psutil.Process, server_pid: int) -> str:
    cmd = " ".join(proc.cmdline())
    if proc.pid == server_pid:
        return "api_server"
    if "resource_tracker" in cmd:
        return "resource_tracker"
    return cmd.split()[0] if cmd.startswith("ATOM::") else cmd[:40]


def process_costs(server_pid: int, window: float = 5.0) -> dict[str, dict]:
    """Each process's USS, and its CPU over a window with no request in flight."""
    server = psutil.Process(server_pid)
    procs = {role(p, server_pid): p for p in [server, *server.children(recursive=True)]}
    used = {name: sum(p.cpu_times()[:2]) for name, p in procs.items()}
    time.sleep(window)
    return {
        name: {
            "uss_mib": round(p.memory_full_info().uss / 2**20),
            "idle_cpu_pct": round(100 * (sum(p.cpu_times()[:2]) - used[name]) / window),
        }
        for name, p in procs.items()
    }


def stop(proc: subprocess.Popen) -> int | None:
    """SIGINT (the server's own shutdown), then kill whatever is left."""
    proc.send_signal(signal.SIGINT)
    try:
        code = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        code = None
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()
    return code


def wait_ready(proc: subprocess.Popen, base: str, start: float) -> float:
    while proc.poll() is None and time.monotonic() - start < 1800:
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                return time.monotonic() - start
        except requests.RequestException:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"no /health 200 from {base}; exit code {proc.poll()}")


def serve_one(case: str, base: str) -> dict:
    body = {"prompt_token_ids": PROMPT, "max_tokens": 16, "temperature": 0}
    reply = requests.post(f"{base}/v1/completions", json=body, timeout=300).json()
    choice = reply["choices"][0]
    result = {
        "finish_reason": choice["finish_reason"],
        "completion_tokens": reply["usage"]["completion_tokens"],
        "text": choice["text"],
    }
    if case == "mtp":
        result["mtp_stats"] = requests.get(f"{base}/debug/mtp_stats", timeout=10).json()
    return result


def run_once(case: str, model: Path, work: Path, run: int) -> dict:
    flags, stand_ins = CASES[case]
    paths = [HERE / "pkgs", *(HERE / f"{name}_pkg" for name in stand_ins), ROOT]
    env = dict(
        os.environ,
        PYTHONPATH=":".join(map(str, paths)),
        SPIKE_RUNNER="stub_runner.StubRunner",
        SPIKE_NAME_LOG=str(work / f"names-{case}.log"),
        TMPDIR=tempfile.mkdtemp(prefix="i"),  # short: ZMQ ipc paths live here
        AITER_LOG_LEVEL="WARNING",
    )
    http = free_port()
    base = f"http://127.0.0.1:{http}"
    cmd = [sys.executable, "-m", "atom.entrypoints.openai.api_server"]
    cmd += ["--model", str(model), "--server-port", str(http)]
    cmd += ["--data-parallel-master-port", str(free_port()), *flags]
    result = {"case": case, "run": run, "load1": round(os.getloadavg()[0], 1)}
    with open(work / f"server-{case}-{run}.log", "w") as log:
        start = time.monotonic()
        proc = subprocess.Popen(
            cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            result["ready_s"] = round(wait_ready(proc, base, start), 2)
            result.update(serve_one(case, base))
            result["processes"] = process_costs(proc.pid)
        finally:
            result["sigint_exit"] = stop(proc)
    return result


def spread(values: list[float]) -> str:
    return f"median {statistics.median(values)}, range {min(values)}-{max(values)}"


def name_table(work: Path) -> list[str]:
    rows = defaultdict(lambda: {"cases": set(), "readers": set()})
    for case in CASES:
        path = work / f"names-{case}.log"
        for line in path.read_text().splitlines() if path.exists() else []:
            _process, name, kind, what, reader = line.split("\t")
            row = rows[name]
            row.update(kind=kind, what=what)
            row["cases"].add(case)
            row["readers"].add(reader)
    lines = ["| name | kind | what it does | (i) mtp | (ii) dp2 | read by |"]
    lines.append("|---|---|---|---|---|---|")
    for name, row in sorted(rows.items()):
        marks = ["x" if case in row["cases"] else "" for case in CASES]
        readers = ", ".join(sorted(row["readers"]))
        lines.append(
            f"| `{name}` | {row['kind']} | {row['what']} | {' | '.join(marks)} | {readers} |"
        )
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--cases", nargs="+", default=list(CASES), choices=list(CASES))
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix="w"))
    write_model(work / "model")
    results = []
    for run in range(args.runs):
        for case in args.cases:
            results.append(run_once(case, work / "model", work, run))
            print(json.dumps(results[-1]), flush=True)
    for case in args.cases:
        runs = [r for r in results if r["case"] == case]
        print(f"{case}: /health 200 after {spread([r['ready_s'] for r in runs])} s")
        for name in sorted(runs[0]["processes"]):
            for metric in ("uss_mib", "idle_cpu_pct"):
                values = [r["processes"][name][metric] for r in runs]
                print(f"{case}: {name} {metric}: {spread(values)}")
    print("\n".join(name_table(work)))
    print(f"work dir: {work}")


if __name__ == "__main__":
    main()
