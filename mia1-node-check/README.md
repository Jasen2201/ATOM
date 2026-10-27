# mia1 node health check

`node_check.sh` checks one MI355X node for the problems behind uneven GLM-5.2 ATOM P/D results on the mia1 cluster. It covers configuration, hardware health, leftovers from earlier jobs, CPU, memory, GPU and RDMA, and above all **how much short CPU work slows down under job-like load**. `compare_nodes.py` puts the results of many nodes side by side and flags the odd ones out.

## Background

The same GLM-5.2 c112 recipe gives about 45-46K tok/s/GPU on mia1-p01-g37, mia1-p01-g50 and the pit2 nodes, but only about 39-40K on g09, g11, g14, g15 and g18. On the slow nodes:

- short host-side Python work in the prefill process (scheduler bookkeeping, ~30 us on a good node) takes 71-96 us per call during the job, against 26-41 us on good nodes;
- the slowdown appears while RDMA reads GPU memory (P/D transfers, Mooncake Store), and the softirq rate rises about 7x;
- g14 and g18 also show 5-7 GB of extra non-PyTorch memory on every GPU and ~8% slower prefill compute.

An idle IPC test (`kipc.py`, SemiAnalysisAI/InferenceX#4339) found only g11 (IPC 4.7 vs ~7.2). g14, g15 and g18 read normal at idle, and g11 stayed slow in jobs after its power cycle restored its idle IPC. So idle tests are not enough; this script measures the same CPU work both idle and under load.

## Requirements

- Run as **root** on an **idle node** (drained, or inside an exclusive allocation with no other jobs). The script refuses to run on a busy node unless you pass `--force`.
- Required: `bash`, `python3` (standard library only).
- Strongly recommended: `perftest` built **with ROCm** (`ib_write_bw --help` lists `--use_rocm`), `turbostat`, `ipmitool`, `dmidecode`, `gcc`, `numactl`, `lsof`, `ethtool`, `amd-smi` or `rocm-smi`. Missing tools only skip their checks; the report lists them.
- Optional: `rocm-bandwidth-test`, `rvs` (`--rvs`), and `docker` with a ROCm PyTorch image (`--gpu-image`) for a per-GPU GEMM / copy / memory-overhead bench.

## Running

Copy the whole directory to the node (or run it from shared storage), then:

```bash
sudo bash node_check.sh                                   # full run, ~15-20 min
sudo bash node_check.sh --quick                           # ~5 min, skips the long tests
sudo bash node_check.sh --gpu-image rocm/atom-dev:<tag>   # adds the GPU bench
sudo bash node_check.sh --out /shared/nodecheck           # write results elsewhere
```

Each run writes `nodecheck_<host>_<time>/` and a `.tar.gz` of it:

| File | Content |
|---|---|
| `report.txt` | verdict, FAIL and WARN list, every check with its value |
| `summary.json` | all results, metrics and configuration (input of `compare_nodes.py`) |
| `config.tsv` | diff-friendly configuration snapshot |
| `raw/` | raw command output (dmesg, SEL, sensors, lspci, turbostat, perftest, probe JSON ...) |

The script only reads configuration. It creates load (CPU, memory, RDMA loopback on every NIC, optional GPU) and never changes BIOS, kernel, governor or C-state settings.

### Recommended procedure

1. **Validate first.** Run the full check on the known-good nodes g37 and g50 and on the known-slow nodes g11, g14, g15 and g18. Compare:
   ```bash
   python3 compare_nodes.py nodecheck_*.tar.gz --good mia1-p01-g37,mia1-p01-g50
   ```
   The `load.jobsim.*`, `load.rdma.*` and `cpu.idle.*` metrics should separate the two groups. If they don't, report that: it means the cause needs the full inference job to show up.
2. **Then sweep the cluster** and compare every node against the same good baseline.
3. Send back the `.tar.gz` files (or the `compare_nodes.py` output).

## What it checks

| Section | Checks |
|---|---|
| preflight | root, missing tools, GPU/NIC topology, node idle (no `/dev/kfd` users, no other Slurm jobs) |
| config | BIOS/board, kernel and cmdline, CPU model/microcode/SMT, amd_pstate, governor, EPP, boost, C-states, clocksource, THP, NUMA balancing, irqbalance, services (falcon-sensor, atop, ...), ROCm/amdgpu/VBIOS, NIC firmware and rate, BMC firmware, perftest ROCm support |
| health | dmesg (MCE, PCIe AER, amdgpu, NIC, thermal, lockups, OOM, clocksource), DRAM ECC (EDAC), DIMM population and speed, BMC sensors, SEL, power reading and power cap, PCIe link width/speed of GPUs and NICs, PCIe error bits, GPU RAS counters, RDMA port state and rate, NIC error counters |
| leftovers | idle VRAM in use per GPU (cluster setup waits for <=10%), processes on the GPUs, D-state processes, hugepages and their holders, stray inference/Mooncake processes, containers, `/dev/shm`, orphaned Slurm cgroups, busy processes at idle |
| cpu | clock at idle / one core / all cores (turbostat), the burst probe on each test CPU (latency after 0 / 0.2 / 2 ms idle gaps; cycles, instructions, IPC and GHz from MSRs; IPC from `perf stat`), IRQ placement, idle interrupt and softirq rates |
| memory | triad bandwidth and latency per NUMA node, remote latency |
| gpu | idle clocks/power/temperature, throttle flags, `rocm-bandwidth-test`, optional RVS GST, optional container bench (bf16 GEMM TFLOPS, H2D/D2H, non-PyTorch memory after init) |
| rdma | loopback bandwidth of every NIC, write and read, from GPU memory (nearest GPU) and host memory |
| load | the burst probe again while (a) all other CPUs are busy, (b) memory bandwidth is saturated, (c) every NIC runs RDMA loopback from GPU memory, (d) **job-like mix**: RDMA on every NIC + memory traffic + 8 CPUs running bursts at once. Reports the slowdown vs idle, IPC under load, interrupt/softirq rates and the RDMA counters that moved |

### The burst probe

The probe runs the same pure-Python burst as `kipc.py` (`for i in range(2000): s += i*i`) pinned to one CPU. Cycles come from APERF and instructions from the IRPerfCount MSR (or atop's PerfCtr1), read-only. Reference values:

| Node state | cycles per burst | IPC |
|---|---|---|
| healthy mia1 node at idle (g09, g37, ...) | ~224K | ~7.0-7.3 |
| g11 before its power cycle | ~341K | ~4.7 |

## Reading the results

- **FAIL**: a clear defect (uncorrected ECC, MCE, link below full width, port down, GPU memory held by an old job, IPC < 6.0, boost off, ...).
- **WARN**: worth a look (corrected errors, SEL events, sensors out of range, idle IPC < 6.8, a load slowdown above 1.25x, leftover processes, ...).
- The load thresholds (1.25x WARN, 1.6x FAIL) are **provisional**. What matters is how a node compares with g37/g50 in `compare_nodes.py`.
- `compare_nodes.py` also lists every configuration key whose value differs between nodes (BIOS, microcode, firmware, governor, C-states, services, IRQ placement ...).

Options: `python3 compare_nodes.py RESULTS... [--good HOSTS] [--tol 0.10] [--csv matrix.csv]`.
