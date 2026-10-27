#!/usr/bin/env python3
"""Helpers for node_check.sh: topology, CPU burst probe, /proc deltas and the final report.

Subcommands:
  topo                              GPUs, RDMA NICs, NUMA CPUs and the nearest GPU of each NIC, as JSON
  burst --cpu N [--gaps ..]         pinned pure-Python burst: latency after idle gaps, GHz and IPC (MSR)
  loop --seconds S                  run the same burst back to back (for `perf stat` IPC)
  procsnap DIR                      copy /proc/{softirqs,interrupts,stat} into DIR
  procdelta BEFORE AFTER SECONDS [--cpu N]   interrupt / softirq rates between two procsnap dirs
  summarize OUTDIR                  build summary.json and report.txt from results/metrics/config TSVs
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import struct
import sys
import time

# --------------------------------------------------------------------------- topology


def _read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _bdf(dev_dir: str) -> str:
    return os.path.basename(os.path.realpath(dev_dir))


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a.split("/"), b.split("/")):
        if x != y:
            break
        n += 1
    return n


def _gpu_index_by_bdf() -> dict[str, int]:
    """HIP/SMI index of each GPU, from amd-smi or rocm-smi; empty when neither is usable."""
    import subprocess

    for cmd in (["amd-smi", "list", "--json"], ["rocm-smi", "--showbus", "--json"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60, check=False
            ).stdout
            data = json.loads(out)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
            print(f"{cmd[0]}: {e}", file=sys.stderr)
            continue
        idx = {}
        if isinstance(data, list):  # amd-smi list
            for row in data:
                bdf = str(row.get("bdf", "")).lower()
                if bdf and "gpu" in row:
                    idx[bdf] = int(row["gpu"])
        elif isinstance(data, dict):  # rocm-smi: {"card0": {"PCI Bus": "0000:05:00.0"}}
            for card, row in data.items():
                m = re.match(r"card(\d+)$", card)
                bus = (
                    str(row.get("PCI Bus", "")).lower() if isinstance(row, dict) else ""
                )
                if m and bus:
                    idx[bus] = int(m.group(1))
        if idx:
            return idx
    return {}


def topo() -> dict:
    gpus = []
    for card in sorted(glob.glob("/sys/class/drm/card*/device")):
        if not re.search(r"/card\d+/device$", card) or not os.path.exists(
            f"{card}/mem_info_vram_total"
        ):
            continue
        gpus.append(
            {
                "card": card.split("/")[-2],
                "bdf": _bdf(card),
                "path": os.path.realpath(card),
                "numa": int(_read(f"{card}/numa_node", "-1") or -1),
            }
        )
    by_bdf = _gpu_index_by_bdf()
    if by_bdf and all(g["bdf"] in by_bdf for g in gpus):
        for g in gpus:
            g["index"] = by_bdf[g["bdf"]]
        index_source = "smi"
    else:
        for i, g in enumerate(sorted(gpus, key=lambda g: g["bdf"])):
            g["index"] = i
        index_source = "pci-order"
    gpus.sort(key=lambda g: g["index"])

    nics = []
    for dev in sorted(glob.glob("/sys/class/infiniband/*")):
        name = os.path.basename(dev)
        devdir = os.path.realpath(f"{dev}/device")
        netdevs = (
            sorted(os.listdir(f"{dev}/device/net"))
            if os.path.isdir(f"{dev}/device/net")
            else []
        )
        nearest = None
        if gpus:
            nearest = max(gpus, key=lambda g: _common_prefix_len(devdir, g["path"]))[
                "index"
            ]
        nics.append(
            {
                "dev": name,
                "bdf": os.path.basename(devdir),
                "netdev": netdevs[0] if netdevs else "",
                "numa": int(_read(f"{dev}/device/numa_node", "-1") or -1),
                "nearest_gpu": nearest,
            }
        )

    numa = {}
    for node in sorted(glob.glob("/sys/devices/system/node/node[0-9]*")):
        numa[os.path.basename(node)] = _read(f"{node}/cpulist")
    return {
        "gpus": gpus,
        "gpu_index_source": index_source,
        "nics": nics,
        "numa_cpulist": numa,
    }


# --------------------------------------------------------------------------- burst probe

# Same burst as the kipc.py check on SemiAnalysisAI/InferenceX#4339: healthy mia1 nodes
# run it in ~224K cycles (IPC ~7.2); the slow g11 took ~341K (IPC ~4.7).


def work() -> int:
    s = 0
    for i in range(2000):
        s += i * i
    return s


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def _latency(gaps: list[float], iters: int) -> dict:
    out = {}
    for gap in gaps:
        xs = []
        for _ in range(iters):
            if gap:
                time.sleep(gap)
            t = time.perf_counter_ns()
            work()
            xs.append((time.perf_counter_ns() - t) / 1000)
        out[f"{gap * 1e3:g}ms"] = {
            "p50_us": round(_pct(xs, 0.5), 2),
            "p90_us": round(_pct(xs, 0.9), 2),
            "p99_us": round(_pct(xs, 0.99), 2),
        }
    return out


MSR_APERF = 0xE7
MSR_HWCR = 0xC0010015
MSR_IRPERF = 0xC00000E9
MSR_PERFEVTSEL0, MSR_PERFCTR0 = 0xC0010200, 0xC0010201
MSR_PERFEVTSEL1, MSR_PERFCTR1 = 0xC0010202, 0xC0010203


def _msr_counters(cpu: int, iters: int) -> dict:
    """Cycles, instructions and GHz per burst from MSRs (read-only).

    Cycles come from APERF (actual cycles in C0). Instructions come from the fixed
    IRPerfCount counter when HWCR.IRPerfEn is set (Linux sets it on Zen), else from
    PerfCtr1 when it is already programmed for instructions retired (atop does this).
    """
    try:
        fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
    except OSError as e:
        return {"error": f"open msr: {e.strerror} (needs root and `modprobe msr`)"}

    def rd(addr: int) -> int:
        return struct.unpack("<Q", os.pread(fd, 8, addr))[0]

    mask48 = (1 << 48) - 1
    try:
        hwcr = rd(MSR_HWCR)
        if hwcr & (1 << 30):
            cyc_addr, ins_addr, ins_src = MSR_APERF, MSR_IRPERF, "IRPerfCount"
        else:
            sel0, sel1 = rd(MSR_PERFEVTSEL0), rd(MSR_PERFEVTSEL1)
            ev = lambda s: (s & 0xFF) | ((s >> 24) & 0xF00)
            on = lambda s: bool(s & (1 << 22))
            if not (on(sel0) and on(sel1) and ev(sel0) == 0x76 and ev(sel1) == 0xC0):
                return {
                    "error": f"no instruction counter: HWCR.IRPerfEn off and PerfCtr0/1 not programmed (sel0={sel0:#x} sel1={sel1:#x})"
                }
            cyc_addr, ins_addr, ins_src = MSR_PERFCTR0, MSR_PERFCTR1, "PerfCtr1(atop)"
        cyc, ins, ghz = [], [], []
        for _ in range(iters):
            t0 = time.perf_counter_ns()
            c0, i0 = rd(cyc_addr) & mask48, rd(ins_addr) & mask48
            work()
            c1, i1 = rd(cyc_addr) & mask48, rd(ins_addr) & mask48
            t1 = time.perf_counter_ns()
            if c1 > c0 and i1 > i0:
                cyc.append(c1 - c0)
                ins.append(i1 - i0)
                ghz.append((c1 - c0) / (t1 - t0))
    except OSError as e:
        return {"error": f"read msr: {e.strerror}"}
    finally:
        os.close(fd)
    if not cyc:
        return {"error": "counters did not advance"}
    c, i = _pct(cyc, 0.5), _pct(ins, 0.5)
    return {
        "cycles_p50_k": round(c / 1e3, 1),
        "instr_p50_k": round(i / 1e3, 1),
        "ipc": round(i / c, 2),
        "ghz_p50": round(_pct(ghz, 0.5), 2),
        "instr_source": ins_src,
    }


def burst(args) -> dict:
    os.sched_setaffinity(0, {args.cpu})
    gaps = [float(g) / 1e3 for g in args.gaps.split(",")]
    for _ in range(200):  # warm up the interpreter and caches
        work()
    out = {"cpu": args.cpu, "latency": _latency(gaps, args.iters)}
    if args.msr:
        out["msr"] = _msr_counters(args.cpu, args.msr_iters)
    return out


def loop(args) -> None:
    end = time.monotonic() + args.seconds
    while time.monotonic() < end:
        for _ in range(100):
            work()


# --------------------------------------------------------------------------- /proc deltas


def procsnap(outdir: str) -> None:
    os.makedirs(outdir, exist_ok=True)
    for name in ("softirqs", "interrupts", "stat"):
        shutil.copyfile(f"/proc/{name}", f"{outdir}/{name}")


def _softirqs(path: str) -> dict[str, list[int]]:
    rows = {}
    with open(path) as f:
        next(f)
        for line in f:
            parts = line.split()
            if parts:
                rows[parts[0].rstrip(":")] = [int(x) for x in parts[1:]]
    return rows


def _interrupts(path: str) -> tuple[int, dict[str, list[int]]]:
    with open(path) as f:
        ncpu = len(f.readline().split())
        rows = {}
        for line in f:
            parts = line.split()
            if not parts:
                continue
            counts = []
            for x in parts[1 : 1 + ncpu]:
                if not x.isdigit():
                    break
                counts.append(int(x))
            name = parts[0].rstrip(":")
            if len(parts) > 1 + ncpu:
                name = f"{name} {' '.join(parts[1 + ncpu:])}"
            rows[name] = counts
    return ncpu, rows


def _stat_cpu(path: str) -> dict[str, list[int]]:
    rows = {}
    with open(path) as f:
        for line in f:
            if line.startswith("cpu"):
                parts = line.split()
                rows[parts[0]] = [int(x) for x in parts[1:]]
    return rows


def procdelta(before: str, after: str, seconds: float, cpu: int | None) -> dict:
    out: dict = {"seconds": seconds}
    sb, sa = _softirqs(f"{before}/softirqs"), _softirqs(f"{after}/softirqs")
    soft = {}
    for k in sa:
        if k in sb:
            d = [a - b for a, b in zip(sa[k], sb[k])]
            soft[k] = {
                "total_per_s": round(sum(d) / seconds),
                "max_cpu_per_s": round(max(d) / seconds) if d else 0,
            }
            if cpu is not None and cpu < len(d):
                soft[k]["test_cpu_per_s"] = round(d[cpu] / seconds)
    out["softirq"] = soft
    _, ib = _interrupts(f"{before}/interrupts")
    _, ia = _interrupts(f"{after}/interrupts")
    per_cpu = None
    by_src = {}
    for k, va in ia.items():
        vb = ib.get(k)
        if not vb or len(vb) != len(va):
            continue
        d = [a - b for a, b in zip(va, vb)]
        per_cpu = d if per_cpu is None else [x + y for x, y in zip(per_cpu, d)]
        src = (
            "amdgpu"
            if "amdgpu" in k
            else "ionic" if "ionic" in k else "mlx" if "mlx" in k else None
        )
        if src:
            by_src[src] = by_src.get(src, 0) + sum(d)
    if per_cpu:
        out["irq_total_per_s"] = round(sum(per_cpu) / seconds)
        top = sorted(range(len(per_cpu)), key=lambda c: -per_cpu[c])[:5]
        out["irq_top_cpus"] = {f"cpu{c}": round(per_cpu[c] / seconds) for c in top}
        if cpu is not None and cpu < len(per_cpu):
            out["irq_test_cpu_per_s"] = round(per_cpu[cpu] / seconds)
    out["irq_by_driver_per_s"] = {k: round(v / seconds) for k, v in by_src.items()}
    stb, sta = _stat_cpu(f"{before}/stat"), _stat_cpu(f"{after}/stat")
    if "cpu" in stb and "cpu" in sta:
        d = [a - b for a, b in zip(sta["cpu"], stb["cpu"])]
        tot = sum(d) or 1
        # user nice system idle iowait irq softirq steal
        names = ["user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"]
        out["cpu_time_pct"] = {
            n: round(100 * d[i] / tot, 2) for i, n in enumerate(names) if i < len(d)
        }
    return out


# --------------------------------------------------------------------------- report


def _read_tsv(path: str, ncols: int) -> list[list[str]]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            rows.append((parts + [""] * ncols)[:ncols])
    return rows


def summarize(outdir: str) -> None:
    results = _read_tsv(f"{outdir}/results.tsv", 5)
    metrics = {}
    for k, v, d in _read_tsv(f"{outdir}/metrics.tsv", 3):
        try:
            metrics[k] = {"value": float(v), "dir": d or "info"}
        except ValueError:
            metrics[k] = {"value": v, "dir": "info"}
    config = {k: v for k, v in _read_tsv(f"{outdir}/config.tsv", 2)}
    meta = {k: v for k, v in _read_tsv(f"{outdir}/meta.tsv", 2)}
    counts: dict[str, int] = {}
    for r in results:
        counts[r[2]] = counts.get(r[2], 0) + 1
    summary = {
        "meta": meta,
        "status_counts": counts,
        "results": [
            dict(zip(("section", "check", "status", "value", "detail"), r))
            for r in results
        ],
        "metrics": metrics,
        "config": config,
    }
    with open(f"{outdir}/summary.json", "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)

    lines = []
    host = meta.get("host", "?")
    verdict = "FAIL" if counts.get("FAIL") else "WARN" if counts.get("WARN") else "PASS"
    lines.append(f"Node health check: {host}   verdict: {verdict}")
    lines.append(
        f"started {meta.get('start', '?')}  finished {meta.get('end', '?')}  mode {meta.get('mode', '?')}  "
        f"root {meta.get('root', '?')}  script v{meta.get('version', '?')}"
    )
    lines.append(
        "status counts: "
        + "  ".join(
            f"{k}={counts[k]}"
            for k in ("FAIL", "WARN", "PASS", "INFO", "SKIP")
            if k in counts
        )
    )
    lines.append("")
    for status in ("FAIL", "WARN"):
        rows = [r for r in results if r[2] == status]
        if rows:
            lines.append(f"==== {status} ({len(rows)})")
            for r in rows:
                lines.append(
                    f"  [{r[0]}] {r[1]}: {r[3]}" + (f"  -- {r[4]}" if r[4] else "")
                )
            lines.append("")
    lines.append("==== All checks")
    section = None
    for r in results:
        if r[0] != section:
            section = r[0]
            lines.append(f"-- {section}")
        lines.append(
            f"  {r[2]:<5} {r[1]:<34} {r[3]}" + (f"   ({r[4]})" if r[4] else "")
        )
    lines.append("")
    lines.append(
        "Raw command output is under raw/. Compare nodes with: python3 compare_nodes.py <result dirs or .tar.gz>"
    )
    with open(f"{outdir}/report.txt", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines[: lines.index("==== All checks")]))


# --------------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("topo")
    b = sub.add_parser("burst")
    b.add_argument("--cpu", type=int, required=True)
    b.add_argument(
        "--gaps", default="0,0.2,2", help="idle gaps before each burst, in ms"
    )
    b.add_argument("--iters", type=int, default=3000)
    b.add_argument("--msr", action="store_true")
    b.add_argument("--msr-iters", type=int, default=20000)
    lp = sub.add_parser("loop")
    lp.add_argument("--seconds", type=float, default=3.0)
    ps = sub.add_parser("procsnap")
    ps.add_argument("dir")
    pd = sub.add_parser("procdelta")
    pd.add_argument("before")
    pd.add_argument("after")
    pd.add_argument("seconds", type=float)
    pd.add_argument("--cpu", type=int)
    sm = sub.add_parser("summarize")
    sm.add_argument("outdir")
    a = p.parse_args()

    if a.cmd == "topo":
        print(json.dumps(topo(), indent=1))
    elif a.cmd == "burst":
        print(json.dumps(burst(a)))
    elif a.cmd == "loop":
        loop(a)
    elif a.cmd == "procsnap":
        procsnap(a.dir)
    elif a.cmd == "procdelta":
        print(json.dumps(procdelta(a.before, a.after, a.seconds, a.cpu)))
    elif a.cmd == "summarize":
        summarize(a.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
