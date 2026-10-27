#!/usr/bin/env python3
"""Compare node_check.sh results across nodes and flag the odd ones out.

Usage:
  python3 compare_nodes.py RESULT [RESULT ...] [--good HOST,HOST] [--tol 0.10] [--csv out.csv]

RESULT is a nodecheck_<host>_<time> directory, its .tar.gz, or its summary.json.
The baseline of each metric is the median over all nodes, or over the --good nodes
(e.g. mia1-p01-g37,mia1-p01-g50, the nodes known to run GLM-5.2 c112 at full speed).
A node is flagged when a metric is worse than the baseline by more than --tol
(direction-aware: IPC/bandwidth low is bad, latency/ratio high is bad).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import sys
import tarfile

# Metrics that best separate slow mia1 nodes, shown first (prefix match).
KEY_METRICS = [
    "load.jobsim.ratio",
    "load.jobsim.p50_us_avg",
    "load.jobsim.ipc_min",
    "load.rdma.ratio_max",
    "load.rdma.softirq_per_s",
    "load.jobsim.softirq_per_s",
    "cpu.idle.ipc_min",
    "cpu.idle.p50_us_gap0.2ms_max",
    "cpu.freq_1core_mhz",
    "cpu.freq_allcore_mhz",
    "rdma.write_gpu_gbps_min",
    "rdma.read_gpu_gbps_min",
    "gpu.vram_used_pct_idle_max",
    "gpu.bench.nontorch_gb_max",
    "gpu.bench.tflops_min",
    "mem.numa0.triad_gbps",
    "mem.numa1.triad_gbps",
    "mem.remote_lat_ns",
]


def load(path: str) -> dict:
    if path.endswith(".json"):
        with open(path) as f:
            return json.load(f)
    if os.path.isdir(path):
        with open(os.path.join(path, "summary.json")) as f:
            return json.load(f)
    if path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(path) as t:
            for m in t.getmembers():
                if m.name.endswith("/summary.json"):
                    return json.load(io.TextIOWrapper(t.extractfile(m)))
    raise SystemExit(f"no summary.json in {path}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("results", nargs="+")
    p.add_argument(
        "--good", default="", help="comma-separated hosts to use as the baseline"
    )
    p.add_argument(
        "--tol",
        type=float,
        default=0.10,
        help="relative tolerance before a metric is flagged",
    )
    p.add_argument("--csv", help="write the full node x metric matrix here")
    a = p.parse_args()

    nodes = {}
    for r in a.results:
        s = load(r)
        host = s.get("meta", {}).get("host") or os.path.basename(r)
        if host in nodes and s.get("meta", {}).get("start", "") < nodes[host].get(
            "meta", {}
        ).get("start", ""):
            continue  # keep the latest run per host
        nodes[host] = s
    hosts = sorted(nodes)
    good = [h for h in a.good.split(",") if h]
    missing = [h for h in good if h not in nodes]
    if missing:
        print(
            f"warning: --good hosts without results: {', '.join(missing)}",
            file=sys.stderr,
        )
    base_hosts = [h for h in good if h in nodes] or hosts

    # ---- status overview
    print(
        f"Nodes: {len(hosts)}   baseline: {'--good ' + ','.join(base_hosts) if good else 'median of all nodes'}   tolerance {a.tol:.0%}\n"
    )
    print(f"{'host':<20} {'verdict':<7} {'FAIL':>4} {'WARN':>4}  failed checks")
    for h in hosts:
        c = nodes[h].get("status_counts", {})
        fails = [
            r["check"] for r in nodes[h].get("results", []) if r["status"] == "FAIL"
        ]
        verdict = "FAIL" if c.get("FAIL") else "WARN" if c.get("WARN") else "PASS"
        print(
            f"{h:<20} {verdict:<7} {c.get('FAIL', 0):>4} {c.get('WARN', 0):>4}  {', '.join(fails)}"
        )

    # ---- metrics
    keys = sorted({k for s in nodes.values() for k in s.get("metrics", {})})
    direction = {}
    for s in nodes.values():
        for k, m in s.get("metrics", {}).items():
            direction.setdefault(k, m.get("dir", "info"))

    def val(h: str, k: str):
        m = nodes[h].get("metrics", {}).get(k)
        return m["value"] if m and isinstance(m.get("value"), (int, float)) else None

    flags: dict[str, list[str]] = {h: [] for h in hosts}
    baseline = {}
    for k in keys:
        vals = [val(h, k) for h in base_hosts if val(h, k) is not None]
        if not vals:
            continue
        b = statistics.median(vals)
        baseline[k] = b
        d = direction[k]
        if d not in ("hi", "lo") or b == 0:
            continue
        for h in hosts:
            v = val(h, k)
            if v is None:
                continue
            dev = (v - b) / abs(b)
            if (d == "hi" and dev < -a.tol) or (d == "lo" and dev > a.tol):
                flags[h].append(f"{k}={v:g} ({dev:+.0%} vs {b:g})")

    shown = [k for k in KEY_METRICS if k in baseline]
    if shown:
        width = max(len(k) for k in shown)
        print(
            f"\nKey metrics (direction: hi = higher is better, lo = lower is better)\n{'metric':<{width}} dir  {'baseline':>9}  "
            + "  ".join(f"{h[-9:]:>9}" for h in hosts)
        )
        for k in shown:
            row = []
            for h in hosts:
                v = val(h, k)
                cell = "-" if v is None else f"{v:g}"
                bad = v is not None and any(f.startswith(k + "=") for f in flags[h])
                row.append(f"{(cell + ('*' if bad else '')):>9}")
            print(
                f"{k:<{width}} {direction[k]:<4} {baseline[k]:>9g}  " + "  ".join(row)
            )
        print("(* = worse than baseline by more than the tolerance)")

    print("\nFlagged metrics per node")
    for h in hosts:
        if flags[h]:
            print(f"  {h}: {len(flags[h])} metrics")
            for f in flags[h][:25]:
                print(f"     {f}")
            if len(flags[h]) > 25:
                print(f"     ... {len(flags[h]) - 25} more (see --csv)")
        else:
            print(f"  {h}: none")

    # ---- configuration differences
    cfg_keys = sorted(
        {k for s in nodes.values() for k in s.get("config", {})}
        - {"host", "uptime_days"}
    )
    diffs = []
    for k in cfg_keys:
        groups: dict[str, list[str]] = {}
        for h in hosts:
            groups.setdefault(
                nodes[h].get("config", {}).get(k, "<missing>"), []
            ).append(h)
        if len(groups) > 1:
            diffs.append((k, groups))
    print(f"\nConfiguration keys that differ between nodes: {len(diffs)}")
    for k, groups in diffs:
        print(f"  {k}")
        for v, hs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f"     {v[:110]!s:<110}  {', '.join(hs)}")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["metric", "dir", "baseline", *hosts])
            for k in keys:
                w.writerow(
                    [
                        k,
                        direction[k],
                        baseline.get(k, ""),
                        *[val(h, k) if val(h, k) is not None else "" for h in hosts],
                    ]
                )
        print(f"\nwrote {a.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
