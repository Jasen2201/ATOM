"""Reproduce SemiAnalysis InferenceX Kimi-K3 Pareto charts from the CSV exports."""

from __future__ import annotations

import csv
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from scipy.interpolate import PchipInterpolator

ROOT = Path(__file__).resolve().parent
E2E_CSV = ROOT / "InferenceX_Kimi-K3_e2e (1).csv"
INT_CSV = ROOT / "InferenceX_Kimi-K3_interactivity (1).csv"

# InferenceX light theme. NVIDIA greens match the GLM-5.2 chart; Kimi-K3's
# export uses Dynamo vLLM / vLLM keys rather than Dynamo TRTLLM / SGLang.
# MI355X (vLLM) is an extra series present in this export.
ATOMESH_SERIES = {
    "key": "atomesh_pd",
    "name": "MI355X (ATOMesh)",
    "color": "#7c3aed",
    "linewidth": 3.4,
    "marker_size": 70,
}

# One PP8 run, drawn as a star and kept off the ATOMesh curve.
PP_STAR = {
    "name": "MI355X (ATOMesh PP8)",
    "label": "PP8 C88",
    "color": "#2563eb",
    "e2e": 11.14,
    "itl": 27.68,
    "y": 15604.35,
}

SHARED_SERIES = [
    {
        "key": "gb300_dynamo-vllm",
        "name": "GB300 NVL72 (Dynamo vLLM)",
        "color": "#81a833",
    },
    {
        "key": "gb200_dynamo-vllm",
        "name": "GB200 NVL72 (Dynamo vLLM)",
        "color": "#4a9232",
    },
    {
        "key": "b300_vllm",
        "name": "B300 (vLLM)",
        "color": "#007c34",
    },
    {
        "key": "b200_dynamo-vllm",
        "name": "B200 (Dynamo vLLM)",
        "color": "#006435",
    },
    {
        "key": "mi355x_vllm",
        "name": "MI355X (vLLM)",
        "color": "#c2410c",
    },
    {
        "key": "mi355x_vllm-disagg",
        "name": "MI355X (vLLM Disagg)",
        "color": "#9a3412",
    },
]

E2E_SERIES = [
    *SHARED_SERIES,
    {
        "key": "mi355x_atom",
        "name": "MI355X (ATOM)",
        "color": "#c53732",
    },
    ATOMESH_SERIES,
]

INT_SERIES = [
    *SHARED_SERIES,
    {
        "key": "mi355x_atom",
        "name": "MI355X (ATOM)",
        "color": "#c53732",
    },
    ATOMESH_SERIES,
]

# ATOMesh manual benchmark at ISL 1,048,576 / OSL 1,024.
# Run: https://github.com/ROCm/ATOM/actions/runs/36596757971
# e2e: P90 E2E Normalized Interactivity; itl: P90 Interactivity (= 1000 / TPOT p90);
# y: Total tok/s/GPU.
# 1P1D-TP8-DCP8-DSPARK3 C48/C56/C72/C88 replace the no-spec DCP8 runs, which are
# worse on both axes. DSPARK7 C1/C4 stay: higher interactivity, so they
# remain on the itl frontier. DSPARK3 C16 was removed from the plotted set.
# 1P2D C48: https://github.com/ROCm/ATOM/actions/runs/38689282081
# TPOT p90 (ms): C1 6.40, C4 6.78, 1P2D C48 14.41,
# 1P1D C48 20.43, C56 24.44, C72 29.93, C88 35.60.
ATOMESH_ROWS = [
    {"Topology": "1P1D-DSPARK7", "Concurrency": "1", "e2e": 27.26, "itl": 156.25, "y": 694.19},
    {"Topology": "1P1D-DSPARK7", "Concurrency": "4", "e2e": 31.76, "itl": 147.49, "y": 1364.65},
    {"Topology": "1P2D-DS3", "Concurrency": "48", "e2e": 26.42, "itl": 69.42, "y": 9703.69},
    {"Topology": "1P1D-DS3", "Concurrency": "48", "e2e": 24.78, "itl": 48.95, "y": 12104.43},
    {"Topology": "1P1D-DS3", "Concurrency": "56", "e2e": 17.41, "itl": 40.91, "y": 12569.33},
    {"Topology": "1P1D-DS3", "Concurrency": "72", "e2e": 10.39, "itl": 33.41, "y": 14152.96},
    {"Topology": "1P1D-DS3", "Concurrency": "88", "e2e": 5.81, "itl": 28.09, "y": 14414.87},
]

# Excluded from the plotted Pareto curve (raw rows kept for reference).
ATOMESH_CURVE_EXCLUDE: set[tuple[str, str]] = set()

BG = "#eaebec"
PLOT_BG = "#f4f5f6"
GRID = "#d5d6d8"
TEXT = "#1b1c1d"
MUTED = "#5c5f63"


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def parse_int(value: str, default: int = 0) -> int:
    value = (value or "").strip()
    if not value:
        return default
    return int(float(value))


def config_segment(tp: int, ep: int, dp_attention: bool) -> str:
    if ep > 1 and tp == ep:
        return f"{'DEP' if dp_attention else 'TEP'}{tp}"
    if ep <= 1:
        return f"{'DPATP' if dp_attention else 'TP'}{tp}"
    return f"{'DPAEP' if dp_attention else 'EP'}{ep}"


def infer_role(
    n_chips: int, ep: int, dp_attention: bool, *, decode: bool
) -> tuple[int, int, int, bool]:
    """Return (tp, ep, workers, dp_attention) for one disagg role.

    Wide-EP Dynamo layouts in the screenshot use DEP4 prefill workers and a
    single DEP{ep} decode worker (e.g. 6xDEP4+1xDEP8).
    """
    if n_chips <= 0:
        return 1, 1, 1, dp_attention
    if dp_attention and ep > 1:
        if decode:
            if n_chips % ep == 0:
                return ep, ep, n_chips // ep, True
            return n_chips, ep, 1, True
        prefill_unit = 4
        if n_chips % prefill_unit == 0 and n_chips >= prefill_unit:
            return prefill_unit, prefill_unit, n_chips // prefill_unit, True
        if n_chips % ep == 0:
            return ep, ep, n_chips // ep, True
        return n_chips, ep, 1, True
    if n_chips % 8 == 0 and n_chips >= 8:
        return 8, 1, n_chips // 8, False
    if n_chips % 4 == 0 and n_chips >= 4:
        return 4, 1, n_chips // 4, False
    return n_chips, 1, 1, False


def point_label(row: dict) -> str:
    tp = parse_int(row["TP"], 8)
    ep = parse_int(row["EP"], 1)
    dp = parse_bool(row["DP Attention"])
    disagg = parse_bool(row["Disaggregated"])
    multinode = parse_bool(row["Is Multinode"])
    n_prefill = parse_int(row.get("Num Prefill Chips", ""))
    n_decode = parse_int(row.get("Num Decode Chips", ""))
    if not disagg:
        return config_segment(tp, ep, dp)
    if not multinode:
        return config_segment(tp, ep, dp)
    p_tp, p_ep, p_w, p_dp = infer_role(n_prefill, ep, dp, decode=False)
    d_tp, d_ep, d_w, d_dp = infer_role(n_decode, ep, dp, decode=True)
    prefill = config_segment(p_tp, p_ep, p_dp)
    decode = config_segment(d_tp, d_ep, d_dp)
    return f"{p_w}x{prefill}+{d_w}x{decode}"


def load_rows(path: Path, x_col: str) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    reader = csv.DictReader(lines)
    for row in reader:
        x_raw = (row.get(x_col) or "").strip()
        y_raw = (row.get("Token Throughput per Chip (tok/s/chip)") or "").strip()
        if not x_raw or not y_raw:
            continue
        rows.append(
            {
                **row,
                "x": float(x_raw),
                "y": float(y_raw),
                "label": point_label(row),
            }
        )
    return rows


def pareto_max_xy(points: list[dict]) -> list[dict]:
    """Upper-right Pareto: maximize interactivity and throughput."""
    ordered = sorted(points, key=lambda p: (p["x"], -p["y"]), reverse=True)
    front: list[dict] = []
    best_y = -math.inf
    for point in ordered:
        if point["y"] > best_y:
            front.append(point)
            best_y = point["y"]
    front.reverse()
    return front


def atomesh_chart_rows(use_e2e_x: bool) -> list[dict]:
    """ATOMesh frontier is defined on itl; both charts plot the same points."""
    rows = [
        row
        for row in ATOMESH_ROWS
        if (row["Topology"], row["Concurrency"]) not in ATOMESH_CURVE_EXCLUDE
    ]
    points = [
        {
            "Hardware Key": "atomesh_pd",
            "Concurrency": row["Concurrency"],
            "x": row["e2e"] if use_e2e_x else row["itl"],
            "y": row["y"],
            "label": f"{row['Topology']} C{row['Concurrency']}",
            "_pareto_x": row["itl"],
        }
        for row in rows
    ]
    ordered = sorted(points, key=lambda p: (p["_pareto_x"], -p["y"]), reverse=True)
    front: list[dict] = []
    best_y = -math.inf
    for point in ordered:
        if point["y"] > best_y:
            front.append({k: v for k, v in point.items() if k != "_pareto_x"})
            best_y = point["y"]
    front.reverse()
    return front


def monotone_curve(xs: np.ndarray, ys: np.ndarray, n: int = 240) -> tuple[np.ndarray, np.ndarray]:
    if len(xs) == 1:
        return xs, ys
    if len(xs) == 2:
        t = np.linspace(0, 1, n)
        return xs[0] + t * (xs[1] - xs[0]), ys[0] + t * (ys[1] - ys[0])
    spline = PchipInterpolator(xs, ys)
    dense_x = np.linspace(xs[0], xs[-1], n)
    return dense_x, spline(dense_x)


def label_offset(series_key: str, index: int, n_points: int, label: str = "") -> tuple[int, int]:
    if series_key == "atomesh_pd":
        # Same points on both charts; offsets are in screen points.
        # dx sign also selects horizontal alignment in draw_chart.
        return {
            "1P1D-DS3 C88": (0, 16),
            "1P1D-DS3 C72": (-12, 6),
            "1P1D-DS3 C56": (12, 6),
            "1P1D-DS3 C48": (12, -14),
            "1P2D-DS3 C48": (-12, -12),
            "1P1D-DSPARK7 C4": (-8, 14),
            "1P1D-DSPARK7 C1": (8, -16),
        }.get(label, (8, 12))
    pattern = [
        (0, 12),
        (-6, -16),
        (8, 12),
        (-10, 10),
        (6, -14),
    ]
    extra = {
        "gb300_dynamo-vllm": [(8, 10), (10, 8), (4, 12), (-18, -8), (8, 12), (0, -16)],
        "gb200_dynamo-vllm": [(8, 10), (10, -12), (0, 12), (8, 10), (0, -16)],
        "mi355x_atom": [(10, 8), (8, -14), (0, 12), (-6, -16), (10, 8)],
        "mi355x_vllm": [(8, 12), (10, -14), (0, 12), (8, -14)],
    }
    offsets = extra.get(series_key, pattern)
    return offsets[index % len(offsets)]


def draw_chart(
    rows: list[dict],
    *,
    title: str,
    x_label: str,
    x_max: float,
    x_step: int,
    y_max: float,
    y_step: int,
    out_path: Path,
    series: list[dict],
    tag_anchors: dict[str, tuple[float, float]],
    star: dict | None = None,
) -> None:
    fig, ax = plt.subplots(figsize=(16, 9.9), dpi=150)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(PLOT_BG)
    ax.set_xlim(0, x_max)
    ax.set_ylim(0, y_max)
    ax.set_xticks(np.arange(0, x_max + 0.1, x_step))
    y_ticks = np.arange(0, y_max + 0.1, y_step)
    ax.set_yticks(y_ticks)
    ax.set_yticklabels(["0" if tick == 0 else f"{int(tick / 1000)}k" for tick in y_ticks])
    ax.grid(True, color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#c9cacd")
        spine.set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=10, length=0)
    ax.set_xlabel(x_label, fontsize=12, color=TEXT, labelpad=10)
    ax.set_ylabel("Token Throughput per Chip (tok/s/chip)", fontsize=12, color=TEXT, labelpad=10)

    fig.text(0.07, 0.965, title, ha="left", va="top", fontsize=14.5, fontweight="bold", color=TEXT)
    fig.text(
        0.07,
        0.932,
        "Updated: 2026-10-09      Source: SemiAnalysis InferenceX™",
        ha="left",
        va="top",
        fontsize=9,
        color=MUTED,
    )
    ax.text(
        0.50,
        0.52,
        "semianalysis",
        transform=ax.transAxes,
        ha="center",
        va="center",
        fontsize=42,
        color="#9aa0a6",
        alpha=0.16,
        fontstyle="italic",
        zorder=1,
    )

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["Hardware Key"], []).append(row)

    for item in series:
        points = grouped.get(item["key"], [])
        # ATOMesh frontier is pre-selected on itl; keep the same points on both charts.
        if item["key"] == "atomesh_pd":
            front = sorted(points, key=lambda p: p["x"])
        else:
            front = pareto_max_xy(points)
        if not front:
            continue
        xs = np.array([p["x"] for p in front], dtype=float)
        ys = np.array([p["y"] for p in front], dtype=float)
        cx, cy = monotone_curve(xs, ys)
        linewidth = float(item.get("linewidth", 2.6))
        marker_size = float(item.get("marker_size", 46))
        ax.plot(
            cx,
            cy,
            color=item["color"],
            linewidth=linewidth,
            solid_capstyle="round",
            zorder=4 if item["key"] == "atomesh_pd" else 3,
        )
        ax.scatter(
            xs,
            ys,
            s=marker_size,
            facecolors="#f7f8f8",
            edgecolors=item["color"],
            linewidths=2.1 if item["key"] == "atomesh_pd" else 1.8,
            zorder=6 if item["key"] == "atomesh_pd" else 5,
        )
        seen_labels: set[str] = set()
        for index, point in enumerate(front):
            label = point["label"]
            # Repeat identical labels only at the ends of a run, matching the screenshot density.
            if label in seen_labels and 0 < index < len(front) - 1 and front[index - 1]["label"] == label:
                continue
            seen_labels.add(label)
            dx, dy = label_offset(item["key"], index, len(front), label)
            if dx >= 8:
                ha = "left"
            elif dx <= -8:
                ha = "right"
            else:
                ha = "center"
            ax.annotate(
                label,
                (point["x"], point["y"]),
                textcoords="offset points",
                xytext=(dx, dy),
                fontsize=8.5,
                fontweight="bold",
                color="#111111",
                ha=ha,
                zorder=6,
            )
        tag_x, tag_y = tag_anchors.get(item["key"], (xs[len(xs) // 2], ys[len(ys) // 2]))
        ax.annotate(
            item["name"],
            (tag_x, tag_y),
            textcoords="offset points",
            xytext=(8, -2),
            fontsize=8,
            fontweight="bold",
            color="white",
            ha="left",
            va="center",
            bbox={
                "boxstyle": "round,pad=0.28",
                "facecolor": item["color"],
                "edgecolor": "none",
                "alpha": 0.95,
            },
            zorder=7,
        )

    if star is not None:
        ax.scatter(
            [star["x"]],
            [star["y"]],
            s=280,
            marker="*",
            facecolors=star["color"],
            edgecolors="#1e3a8a",
            linewidths=0.6,
            zorder=8,
        )
        ax.annotate(
            star["label"],
            (star["x"], star["y"]),
            textcoords="offset points",
            xytext=star["label_offset"],
            fontsize=8.5,
            fontweight="bold",
            color="#111111",
            ha="left",
            va="center",
            zorder=8,
        )

    legend_handles = [
        Line2D([0], [0], color=item["color"], linewidth=2.4, label=item["name"])
        for item in series
    ]
    if star is not None:
        legend_handles.append(
            Line2D(
                [0],
                [0],
                color=star["color"],
                marker="*",
                markersize=12,
                linestyle="None",
                label=star["name"],
            )
        )
    legend = ax.legend(
        handles=legend_handles,
        loc="upper right",
        frameon=True,
        fancybox=True,
        borderpad=0.7,
        fontsize=8.5,
        labelcolor=TEXT,
        handlelength=1.8,
    )
    frame = legend.get_frame()
    frame.set_facecolor("#eceeef")
    frame.set_edgecolor("#c5c7ca")
    frame.set_linewidth(0.8)
    frame.set_alpha(0.96)

    fig.text(
        0.5,
        0.012,
        "inferencex.semianalysis.com/inference/kimi-k3",
        ha="center",
        va="bottom",
        fontsize=9,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.07, right=0.985, top=0.90, bottom=0.085)
    fig.savefig(out_path, facecolor=fig.get_facecolor(), edgecolor="none")
    plt.close(fig)
    print(f"wrote {out_path}")


def dump_frontiers(rows: list[dict], title: str, series: list[dict]) -> None:
    print(f"\n=== {title} ===")
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["Hardware Key"], []).append(row)
    for item in series:
        points = grouped.get(item["key"], [])
        front = points if item["key"] == "atomesh_pd" else pareto_max_xy(points)
        print(item["name"])
        for point in front:
            print(
                f"  x={point['x']:.2f}  y={point['y']:.2f}  {point['label']}  "
                f"C{point['Concurrency']}"
            )


def main() -> None:
    e2e_x = "P90 E2E Normalized Interactivity (tok/s/user)"
    int_x = "P90 Interactivity (tok/s/user)"
    e2e_rows = load_rows(E2E_CSV, e2e_x)
    int_rows = load_rows(INT_CSV, int_x)
    e2e_rows.extend(atomesh_chart_rows(use_e2e_x=True))
    int_rows.extend(atomesh_chart_rows(use_e2e_x=False))
    dump_frontiers(e2e_rows, "E2E normalized", E2E_SERIES)
    dump_frontiers(int_rows, "P90 interactivity", INT_SERIES)

    draw_chart(
        e2e_rows,
        title="Kimi K3 2.8T Agentic Token Throughput per Chip vs. P90 E2E Normalized Interactivity",
        x_label="P90 E2E Normalized Interactivity (tok/s/user)",
        x_max=100,
        x_step=10,
        y_max=25000,
        y_step=5000,
        out_path=ROOT / "InferenceX_Kimi-K3_e2e_pareto.png",
        series=E2E_SERIES,
        tag_anchors={
            "gb300_dynamo-vllm": (55, 7000),
            "gb200_dynamo-vllm": (40, 2000),
            "b300_vllm": (42, 15000),
            "b200_dynamo-vllm": (64, 6200),
            "mi355x_vllm": (24, 10000),
            "mi355x_vllm-disagg": (40, 6000),
            "mi355x_atom": (50, 10500),
            "atomesh_pd": (36, 16500),
        },
        star={
            **PP_STAR,
            "x": PP_STAR["e2e"],
            "label_offset": (12, -12),
        },
    )
    draw_chart(
        int_rows,
        title="Kimi K3 2.8T Agentic Token Throughput per Chip vs. P90 Interactivity",
        x_label="P90 Interactivity (tok/s/user)",
        x_max=280,
        x_step=20,
        y_max=25000,
        y_step=5000,
        out_path=ROOT / "InferenceX_Kimi-K3_interactivity_pareto.png",
        series=INT_SERIES,
        tag_anchors={
            "gb300_dynamo-vllm": (155, 12000),
            "gb200_dynamo-vllm": (80, 3500),
            "b300_vllm": (100, 15000),
            "b200_dynamo-vllm": (125, 7000),
            "mi355x_vllm": (36, 9000),
            "mi355x_vllm-disagg": (80, 8000),
            "mi355x_atom": (34, 11500),
            "atomesh_pd": (70, 15500),
        },
        star={
            **PP_STAR,
            "x": PP_STAR["itl"],
            "label_offset": (14, 16),
        },
    )


if __name__ == "__main__":
    main()
