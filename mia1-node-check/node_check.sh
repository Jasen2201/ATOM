#!/usr/bin/env bash
# Node health check for the mia1 MI355X GPU nodes.
#
# Why this exists: GLM-5.2 ATOM P/D runs are 10-15% slower on some mia1 nodes (g09, g11, g14,
# g15, g18 so far) than on g37 / g50 / the pit2 nodes. On those nodes short host-side CPU work
# (Python bursts of ~50 us) slows down 2-3x while RDMA reads GPU memory, and the softirq rate
# rises ~7x. An idle IPC test (kipc.py on SemiAnalysisAI/InferenceX#4339) only caught g11, so
# this script measures the same CPU work both idle and under job-like load (RDMA from GPU
# memory on every NIC, interrupts, several busy CPUs), plus a full config / health snapshot.
#
# Run as root on an IDLE node (drained, or inside an exclusive allocation):
#   sudo bash node_check.sh                # full run, ~15-20 min
#   sudo bash node_check.sh --quick        # skip the long tests, ~5 min
#   sudo bash node_check.sh --gpu-image rocm/atom-dev:<tag>   # also run a GPU bench in a container
# Output: <out>/nodecheck_<host>_<time>/{report.txt,summary.json,raw/} and a .tar.gz of it.
# Compare nodes: python3 compare_nodes.py nodecheck_*.tar.gz [--good mia1-p01-g37,mia1-p01-g50]
#
# The script only reads configuration. It generates load (CPU, memory, RDMA loopback, GPU) and
# never changes BIOS, kernel, governor or C-state settings.

set -uo pipefail

VERSION=1.0
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MODE=full
FORCE=0
OUT_BASE=${OUT_BASE:-$PWD}
TEST_CPUS=""
GPU_IMAGE=""
RUN_RVS=0
SKIP_RDMA=0
SKIP_GPU=0
LOAD_SECONDS=30

usage() {
    awk 'NR > 1 && /^#/ {sub(/^# ?/, ""); print; next} NR > 1 {exit}' "$0"
    cat <<'EOF'
Options:
  --quick              skip rocm-bandwidth-test, RDMA read tests and the single-load tests
  --out DIR            where to write results (default: current directory)
  --cpus "a b c"       CPUs for the burst probe (default: 16 40 80 100, clipped to the node)
  --gpu-image IMAGE    docker image with ROCm PyTorch for the GPU bench (optional)
  --rvs                also run the ROCm Validation Suite (rvs) if installed
  --skip-rdma          skip RDMA tests (e.g. NICs in use)
  --skip-gpu           skip GPU tests
  --load-seconds N     length of each load phase (default 30)
  --force              run even if the node looks busy (results will not be comparable)
EOF
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --quick) MODE=quick ;;
        --out) OUT_BASE=$2; shift ;;
        --cpus) TEST_CPUS=$2; shift ;;
        --gpu-image) GPU_IMAGE=$2; shift ;;
        --rvs) RUN_RVS=1 ;;
        --skip-rdma) SKIP_RDMA=1 ;;
        --skip-gpu) SKIP_GPU=1 ;;
        --load-seconds) LOAD_SECONDS=$2; shift ;;
        --force) FORCE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option $1"; usage; exit 2 ;;
    esac
    shift
done

HOST=$(hostname -s)
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
OUT=$OUT_BASE/nodecheck_${HOST}_$(date -u +%Y%m%dT%H%M%SZ)
RAW=$OUT/raw
TOOLS=$OUT/tools
mkdir -p "$RAW" "$TOOLS" || exit 1
RES=$OUT/results.tsv
MET=$OUT/metrics.tsv
CFG=$OUT/config.tsv
META=$OUT/meta.tsv
: > "$RES"; : > "$MET"; : > "$CFG"; : > "$META"
IS_ROOT=0; [[ $(id -u) -eq 0 ]] && IS_ROOT=1
BG_PIDS=()

cleanup() {
    for p in "${BG_PIDS[@]:-}"; do [[ -n $p ]] && kill "$p" 2>/dev/null; done
    pkill -f "$TOOLS/" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# ----------------------------------------------------------------------------- helpers

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/run.log"; }
section() { CUR_SECTION=$1; log "== $1"; }
# res CHECK STATUS VALUE [DETAIL]   STATUS: PASS WARN FAIL INFO SKIP
res() {
    printf '%s\t%s\t%s\t%s\t%s\n' "$CUR_SECTION" "$1" "$2" "$3" "${4:-}" >> "$RES"
    printf '   %-5s %-34s %s%s\n' "$2" "$1" "$3" "${4:+   ($4)}" | tee -a "$OUT/run.log"
}
# metric KEY VALUE DIR    DIR: hi (higher is better), lo (lower is better), info
metric() { [[ -n ${2:-} ]] && printf '%s\t%s\t%s\n' "$1" "$2" "${3:-info}" >> "$MET"; }
cfg() { printf '%s\t%s\n' "$1" "$(echo "${2:-}" | tr '\t\n' '  ' | sed 's/ *$//')" >> "$CFG"; }
have() { command -v "$1" >/dev/null 2>&1; }
# run NAME TIMEOUT CMD...   -> raw/NAME.txt, returns the command's exit code
run() { local n=$1 t=$2; shift 2; timeout "$t" "$@" > "$RAW/$n.txt" 2>&1; }
# pyj FILE EXPR   evaluate a Python expression on the JSON in FILE (bound to d); empty on error
pyj() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); v=$2; print('' if v is None else v)" "$1" 2>/dev/null; }
calc() { python3 -c "print($1)" 2>/dev/null; }
# cmp_status VALUE WARN FAIL DIR   -> PASS/WARN/FAIL (DIR lo: bad when above; hi: bad when below)
cmp_status() {
    python3 - "$@" <<'PY'
import sys
v, w, f, d = float(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
bad = (lambda x, t: x > t) if d == "lo" else (lambda x, t: x < t)
print("FAIL" if bad(v, f) else "WARN" if bad(v, w) else "PASS")
PY
}
helpers() { python3 "$TOOLS/nodecheck_helpers.py" "$@"; }

cp "$SCRIPT_DIR/nodecheck_helpers.py" "$TOOLS/" || { echo "nodecheck_helpers.py must sit next to this script"; exit 1; }
cp "$SCRIPT_DIR/membw.c" "$TOOLS/" 2>/dev/null

printf 'host\t%s\nstart\t%s\nmode\t%s\nroot\t%s\nversion\t%s\nargs\t%s\n' \
    "$HOST" "$START" "$MODE" "$IS_ROOT" "$VERSION" "$*" > "$META"
log "node_check v$VERSION on $HOST, mode $MODE, output $OUT"

# ============================================================================= 0. preflight

section preflight
if [[ $IS_ROOT -eq 1 ]]; then res root PASS yes; else res root WARN "no" "MSR, dmesg, dmidecode, ipmitool and RDMA checks need root; they will be skipped"; fi
[[ $IS_ROOT -eq 1 ]] && modprobe msr 2>/dev/null

missing=()
for t in python3 lspci dmidecode ipmitool turbostat perf numactl gcc ib_write_bw ib_read_bw ibv_devinfo ethtool amd-smi rocm-smi rocm-bandwidth-test lsof; do
    have "$t" || missing+=("$t")
done
res tools_missing "$([[ ${#missing[@]} -eq 0 ]] && echo PASS || echo INFO)" "${missing[*]:-none}"
have python3 || { echo "python3 is required"; exit 1; }

helpers topo > "$RAW/topo.json" 2> "$RAW/topo.err"
NGPU=$(pyj "$RAW/topo.json" 'len(d["gpus"])')
NNIC=$(pyj "$RAW/topo.json" 'len(d["nics"])')
res topology INFO "${NGPU:-0} GPUs, ${NNIC:-0} RDMA devices" "GPU index from $(pyj "$RAW/topo.json" 'd["gpu_index_source"]')"

busy=()
gpu_pids=$(lsof -t /dev/kfd 2>/dev/null | sort -u | tr '\n' ' ')
[[ -n $gpu_pids ]] && busy+=("processes holding /dev/kfd: $gpu_pids")
if have squeue; then
    jobs=$(squeue -h -w "$HOST" -o '%i %u %j' 2>/dev/null | grep -v "^${SLURM_JOB_ID:-none} " | head -5 | tr '\n' ';')
    [[ -n $jobs ]] && busy+=("slurm jobs: $jobs")
fi
if [[ ${#busy[@]} -gt 0 ]]; then
    res node_idle FAIL "busy" "${busy[*]}"
    if [[ $FORCE -eq 0 ]]; then
        log "node is busy; drain it or re-run with --force"
        helpers summarize "$OUT"; exit 3
    fi
else
    res node_idle PASS "no GPU processes or other slurm jobs"
fi

NCPU=$(nproc --all)
if [[ -z $TEST_CPUS ]]; then
    for c in 16 40 80 100; do [[ $c -lt $NCPU ]] && TEST_CPUS+="$c "; done
    [[ -z $TEST_CPUS ]] && TEST_CPUS=0
fi
TEST_CPUS=$(echo $TEST_CPUS)
res test_cpus INFO "$TEST_CPUS"

# ============================================================================= 1. configuration

section config
cfg host "$HOST"
if [[ $IS_ROOT -eq 1 ]]; then
    for k in bios-vendor bios-version bios-release-date system-manufacturer system-product-name baseboard-product-name baseboard-version; do
        cfg "dmi.$k" "$(dmidecode -s "$k" 2>/dev/null | head -1)"
    done
    run dmidecode_memory 30 dmidecode -t memory
    run dmidecode_processor 30 dmidecode -t processor
fi
cfg kernel "$(uname -r)"
cfg os "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}")"
cfg cmdline "$(sed 's/root=UUID=[^ ]*//; s/BOOT_IMAGE=[^ ]*//' /proc/cmdline)"
cfg uptime_days "$(awk '{printf "%.2f", $1/86400}' /proc/uptime)"
run lscpu 10 lscpu
cfg cpu.model "$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | xargs)"
cfg cpu.count "$NCPU"
cfg cpu.sockets "$(lscpu | awk -F: '/^Socket\(s\)/{print $2}' | xargs)"
cfg cpu.threads_per_core "$(lscpu | awk -F: '/^Thread\(s\) per core/{print $2}' | xargs)"
cfg cpu.microcode "$(grep microcode /proc/cpuinfo | awk '{print $3}' | sort | uniq -c | xargs)"
cfg numa.nodes "$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l)"
cfg numa.cpulists "$(pyj "$RAW/topo.json" '" ".join(f"{k}={v}" for k,v in d["numa_cpulist"].items())')"
P=/sys/devices/system/cpu
cfg cpu.amd_pstate "$(cat $P/amd_pstate/status 2>/dev/null)"
cfg cpu.amd_pstate_prefcore "$(cat $P/amd_pstate/prefcore 2>/dev/null)"
cfg cpu.scaling_driver "$(cat $P/cpufreq/policy0/scaling_driver 2>/dev/null)"
cfg cpu.governor "$(cat $P/cpufreq/policy*/scaling_governor 2>/dev/null | sort | uniq -c | xargs)"
cfg cpu.epp "$(cat $P/cpufreq/policy*/energy_performance_preference 2>/dev/null | sort | uniq -c | xargs)"
cfg cpu.boost "$(cat $P/cpufreq/boost 2>/dev/null || cat $P/cpufreq/policy0/boost 2>/dev/null)"
cfg cpu.freq_min_max_khz "$(cat $P/cpufreq/policy0/scaling_min_freq $P/cpufreq/policy0/scaling_max_freq 2>/dev/null | xargs)"
cfg cpu.cpuinfo_max_khz "$(cat $P/cpufreq/policy0/cpuinfo_max_freq 2>/dev/null)"
cstates=""
for s in $P/cpu0/cpuidle/state*; do
    [[ -d $s ]] && cstates+="$(cat "$s/name"):lat$(cat "$s/latency"):dis$(cat "$s/disable") "
done
cfg cpu.cpuidle_cpu0 "$cstates"
cfg cpu.cpuidle_disabled_anywhere "$(cat $P/cpu*/cpuidle/state*/disable 2>/dev/null | sort | uniq -c | xargs)"
cfg cpu.cpuidle_driver "$(cat $P/cpuidle/current_driver 2>/dev/null)"
cfg cpu.smt "$(cat $P/smt/control 2>/dev/null)"
cfg cpu.vulnerabilities "$(grep -H . $P/vulnerabilities/* 2>/dev/null | sed 's#.*/##' | md5sum | cut -c1-12)"
grep -H . $P/vulnerabilities/* > "$RAW/vulnerabilities.txt" 2>/dev/null
cfg clocksource "$(cat /sys/devices/system/clocksource/clocksource0/current_clocksource)"
cfg thp "$(cat /sys/kernel/mm/transparent_hugepage/enabled) defrag=$(cat /sys/kernel/mm/transparent_hugepage/defrag)"
cfg numa_balancing "$(cat /proc/sys/kernel/numa_balancing 2>/dev/null)"
cfg ksm "$(cat /sys/kernel/mm/ksm/run 2>/dev/null)"
cfg swap "$(awk '/SwapTotal/{print $2" kB"}' /proc/meminfo)"
cfg mem.total_gb "$(awk '/MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo)"
cfg hugepages.2m_config "$(cat /proc/sys/vm/nr_hugepages) overcommit=$(cat /proc/sys/vm/nr_overcommit_hugepages)"
cfg irqbalance "$(systemctl is-active irqbalance 2>/dev/null)"
cfg tuned "$(tuned-adm active 2>/dev/null | cut -d: -f2- | xargs)"
cfg cgroup "$(stat -fc %T /sys/fs/cgroup)"
cfg ulimit.memlock "$(ulimit -l)"
cfg time_sync "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
for svc in falcon-sensor atop atopacct weka-agent rasdaemon mcelog nvidia-fabricmanager; do
    cfg "service.$svc" "$(systemctl is-active "$svc" 2>/dev/null)"
done
systemctl list-units --type=service --state=running --no-pager --no-legend > "$RAW/services_running.txt" 2>/dev/null
cfg services_running_count "$(wc -l < "$RAW/services_running.txt")"
# GPU / ROCm stack
cfg rocm.version "$(cat /opt/rocm/.info/version 2>/dev/null)"
cfg amdgpu.driver "$(cat /sys/module/amdgpu/version 2>/dev/null || modinfo -F version amdgpu 2>/dev/null)"
cfg gpu.count "${NGPU:-0}"
vb=""
for d in $(pyj "$RAW/topo.json" '" ".join(g["card"] for g in d["gpus"])'); do vb+="$(cat /sys/class/drm/$d/device/vbios_version 2>/dev/null) "; done
cfg gpu.vbios "$(echo $vb | tr ' ' '\n' | sort | uniq -c | xargs)"
have amd-smi && { run amdsmi_static 60 amd-smi static; run amdsmi_firmware 60 amd-smi firmware; }
have rocm-smi && run rocmsmi_all 60 rocm-smi -a
# NICs
cfg nic.count "${NNIC:-0}"
fw=""; rates=""
for dev in $(pyj "$RAW/topo.json" '" ".join(n["dev"] for n in d["nics"])'); do
    fw+="$(cat /sys/class/infiniband/$dev/fw_ver 2>/dev/null) "
    rates+="$(cat /sys/class/infiniband/$dev/ports/1/rate 2>/dev/null | awk '{print $1}') "
done
cfg nic.fw "$(echo $fw | tr ' ' '\n' | sort | uniq -c | xargs)"
cfg nic.rate_gbps "$(echo $rates | tr ' ' '\n' | sort | uniq -c | xargs)"
have ibv_devinfo && run ibv_devinfo 60 ibv_devinfo -v
have rdma && { run rdma_link 30 rdma link; run rdma_res 30 rdma resource; }
ip -br link > "$RAW/ip_link.txt" 2>&1; ip -br addr > "$RAW/ip_addr.txt" 2>&1
if [[ $IS_ROOT -eq 1 ]] && have ipmitool; then
    run ipmi_mc_info 60 ipmitool mc info
    cfg bmc.fw "$(awk -F: '/Firmware Revision/{print $2}' "$RAW/ipmi_mc_info.txt" | xargs)"
fi
cfg perftest.rocm "$(ib_write_bw --help 2>&1 | grep -q -- '--use_rocm' && echo yes || echo no)"
lspci -tv > "$RAW/lspci_tree.txt" 2>&1
lspci -vvv > "$RAW/lspci_vvv.txt" 2>&1
res config_snapshot INFO "$(wc -l < "$CFG") keys" "config.tsv; diff it across nodes with compare_nodes.py"

# Settings with a known good value on these nodes
gov=$(cat $P/cpufreq/policy*/scaling_governor 2>/dev/null | sort -u | xargs)
res cpu_governor INFO "${gov:-n/a}" "performance pins the clock; compare across nodes"
boost=$(cat $P/cpufreq/boost 2>/dev/null || cat $P/cpufreq/policy0/boost 2>/dev/null)
[[ -n $boost ]] && res cpu_boost "$([[ $boost == 1 ]] && echo PASS || echo FAIL)" "$boost" "boost off caps the 9575F well below 5 GHz"
smt=$(cat $P/smt/control 2>/dev/null)
res cpu_smt INFO "${smt:-n/a}" "the healthy reference nodes run SMT off (128 CPUs)"
mc=$(grep microcode /proc/cpuinfo | awk '{print $3}' | sort -u | wc -l)
res microcode_uniform "$([[ $mc -le 1 ]] && echo PASS || echo FAIL)" "$(grep -m1 microcode /proc/cpuinfo | awk '{print $3}')" "$mc distinct values across CPUs"

# ============================================================================= 2. hardware health

section health
if [[ $IS_ROOT -eq 1 ]]; then
    dmesg -T > "$RAW/dmesg.txt" 2>&1
    count() { grep -ciE "$1" "$RAW/dmesg.txt"; }
    n=$(count 'machine check|mce:|hardware error|\[Hardware Error\]'); res dmesg_mce "$([[ $n -eq 0 ]] && echo PASS || echo FAIL)" "$n lines"
    n=$(count 'AER:|pcieport.*error|PCIe Bus Error'); res dmesg_pcie_aer "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines"
    n=$(count 'amdgpu.*(fault|timeout|ring .* timeout|gpu reset|RAS|ECC|error)'); res dmesg_amdgpu "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines" "$(grep -iE 'amdgpu.*(fault|timeout|reset|RAS|ECC|error)' "$RAW/dmesg.txt" | tail -1 | cut -c1-140)"
    n=$(count '(ionic|mlx5|bnxt).*(error|fail|timeout|reset|down)'); res dmesg_nic "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines"
    n=$(count 'thermal|throttl|PROCHOT|temperature above'); res dmesg_thermal "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines"
    n=$(count 'hung_task|soft lockup|hard LOCKUP|rcu.*stall|blocked for more than'); res dmesg_lockup "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines"
    n=$(count 'out of memory|oom-kill'); res dmesg_oom "$([[ $n -eq 0 ]] && echo PASS || echo INFO)" "$n lines"
    n=$(count 'clocksource.*(unstable|watchdog)|tsc.*unstable'); res dmesg_clocksource "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n lines"
else
    res dmesg SKIP "needs root"
fi

ce=0; ue=0
for f in /sys/devices/system/edac/mc/mc*/ce_count; do [[ -f $f ]] && ce=$((ce + $(cat "$f"))); done
for f in /sys/devices/system/edac/mc/mc*/ue_count; do [[ -f $f ]] && ue=$((ue + $(cat "$f"))); done
if ls /sys/devices/system/edac/mc/mc* >/dev/null 2>&1; then
    res dram_ecc_uncorrected "$([[ $ue -eq 0 ]] && echo PASS || echo FAIL)" "$ue"
    res dram_ecc_corrected "$([[ $ce -eq 0 ]] && echo PASS || echo WARN)" "$ce"
    metric mem.ecc_ce "$ce" lo
else
    res dram_ecc SKIP "no EDAC memory controllers registered"
fi
if [[ -s $RAW/dmidecode_memory.txt ]]; then
    dimms=$(grep -cE '^\s+Size: [0-9]+ (GB|MB)' "$RAW/dmidecode_memory.txt")
    speeds=$(grep -E '^\s+Configured Memory Speed: [0-9]' "$RAW/dmidecode_memory.txt" | awk -F: '{print $2}' | sort | uniq -c | xargs)
    cfg mem.dimms "$dimms"; cfg mem.dimm_speed "$speeds"
    nspeeds=$(grep -E '^\s+Configured Memory Speed: [0-9]' "$RAW/dmidecode_memory.txt" | sort -u | wc -l)
    res dimm_population INFO "$dimms DIMMs" "$speeds"
    res dimm_speed_uniform "$([[ $nspeeds -le 1 ]] && echo PASS || echo WARN)" "$nspeeds distinct speeds"
fi

if [[ $IS_ROOT -eq 1 ]] && have ipmitool; then
    run ipmi_sdr 120 ipmitool sdr elist
    bad=$(awk -F'|' '{gsub(/ /,"",$3)} $3!="ok" && $3!="ns" && $3!="" {print $1"="$5}' "$RAW/ipmi_sdr.txt" | sed 's/  */ /g' | head -8 | tr '\n' ';')
    res bmc_sensors "$([[ -z $bad ]] && echo PASS || echo WARN)" "${bad:-all ok}"
    run ipmi_sel 120 ipmitool sel elist
    n=$(grep -ciE 'critical|fail|error|non-recoverable|uncorrect|ierr|mce|thermal|throttl|predictive' "$RAW/ipmi_sel.txt")
    res bmc_sel_events "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n of $(wc -l < "$RAW/ipmi_sel.txt") entries" "$(grep -iE 'critical|fail|error|non-recoverable|uncorrect|ierr|mce|thermal|throttl|predictive' "$RAW/ipmi_sel.txt" | tail -2 | cut -c1-120 | tr '\n' ';')"
    run ipmi_power 60 ipmitool dcmi power reading
    w=$(awk -F: '/Instantaneous power reading/{print $2}' "$RAW/ipmi_power.txt" | awk '{print $1}')
    res node_power_idle INFO "${w:-n/a} W"; metric power.idle_watts "$w" info
    run ipmi_power_limit 60 ipmitool dcmi power get_limit
    lim=$(grep -i 'Limit State' "$RAW/ipmi_power_limit.txt" | cut -d: -f2- | xargs)
    res node_power_cap "$(echo "$lim" | grep -qi 'no active' && echo PASS || echo WARN)" "${lim:-n/a}"
else
    res bmc SKIP "needs root and ipmitool"
fi

# PCIe links of GPUs and NICs: width must match the capability; NIC speed too (GPUs change
# link speed with load, so only their width is judged).
pcie_bad=()
for spec in $(pyj "$RAW/topo.json" '" ".join("gpu:"+g["bdf"] for g in d["gpus"])') $(pyj "$RAW/topo.json" '" ".join("nic:"+n["bdf"] for n in d["nics"])'); do
    kind=${spec%%:*}; bdf=${spec#*:}; d=/sys/bus/pci/devices/$bdf
    cw=$(cat "$d/current_link_width" 2>/dev/null); mw=$(cat "$d/max_link_width" 2>/dev/null)
    cs=$(cat "$d/current_link_speed" 2>/dev/null | awk '{print $1}'); ms=$(cat "$d/max_link_speed" 2>/dev/null | awk '{print $1}')
    echo "$kind $bdf width $cw/$mw speed $cs/$ms" >> "$RAW/pcie_links.txt"
    [[ -n $cw && -n $mw && $cw != "$mw" ]] && pcie_bad+=("$kind $bdf x$cw/x$mw")
    [[ $kind == nic && -n $cs && -n $ms && $cs != "$ms" ]] && pcie_bad+=("$kind $bdf ${cs}/${ms}GT/s")
done
res pcie_links "$([[ ${#pcie_bad[@]} -eq 0 ]] && echo PASS || echo FAIL)" "${pcie_bad[*]:-all GPU/NIC links at full width}" "raw/pcie_links.txt"
if [[ $IS_ROOT -eq 1 ]]; then
    n=$(grep -cE 'CESta:.*(RxErr|BadTLP|BadDLLP|Rollover|Timeout)\+' "$RAW/lspci_vvv.txt")
    res pcie_corrected_errors "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n devices with corrected-error bits set" "lspci CESta"
    n=$(grep -E 'UESta:' "$RAW/lspci_vvv.txt" | sed 's/UnsupReq+//' | grep -c '+')
    res pcie_uncorrected_errors "$([[ $n -eq 0 ]] && echo PASS || echo WARN)" "$n devices with uncorrected-error bits set" "UnsupReq ignored; check lspci UESta"
fi

# GPU RAS counters
if [[ $SKIP_GPU -eq 0 && ${NGPU:-0} -gt 0 ]]; then
    gue=0; gce=0
    for card in $(pyj "$RAW/topo.json" '" ".join(g["card"] for g in d["gpus"])'); do
        for f in /sys/class/drm/$card/device/ras/*_err_count; do
            [[ -f $f ]] || continue
            u=$(awk '/^ue:/{print $2}' "$f"); c=$(awk '/^ce:/{print $2}' "$f")
            gue=$((gue + ${u:-0})); gce=$((gce + ${c:-0}))
            [[ ${u:-0} -gt 0 || ${c:-0} -gt 0 ]] && echo "$card ${f##*/} ue=$u ce=$c" >> "$RAW/gpu_ras.txt"
        done
    done
    res gpu_ras_uncorrected "$([[ $gue -eq 0 ]] && echo PASS || echo FAIL)" "$gue"
    res gpu_ras_corrected "$([[ $gce -eq 0 ]] && echo PASS || echo WARN)" "$gce"
fi

# RDMA ports and NIC error counters
if [[ ${NNIC:-0} -gt 0 ]]; then
    down=(); rates=()
    for dev in $(pyj "$RAW/topo.json" '" ".join(n["dev"] for n in d["nics"])'); do
        st=$(cat /sys/class/infiniband/$dev/ports/1/state 2>/dev/null)
        [[ $st == *ACTIVE* ]] || down+=("$dev:${st:-?}")
        rates+=("$(cat /sys/class/infiniband/$dev/ports/1/rate 2>/dev/null | awk '{print $1}')")
        if [[ -d /sys/class/infiniband/$dev/ports/1/hw_counters ]]; then
            grep -H . /sys/class/infiniband/$dev/ports/1/hw_counters/* 2>/dev/null | sed "s#.*/hw_counters/#$dev #" >> "$RAW/rdma_hw_counters_start.txt"
        fi
    done
    res rdma_ports_active "$([[ ${#down[@]} -eq 0 ]] && echo PASS || echo FAIL)" "${down[*]:-all ACTIVE}"
    nr=$(printf '%s\n' "${rates[@]}" | sort -u | wc -l)
    res rdma_rate_uniform "$([[ $nr -le 1 ]] && echo PASS || echo WARN)" "$(printf '%s\n' "${rates[@]}" | sort | uniq -c | xargs) Gb/s"
    if have ethtool; then
        for nd in $(pyj "$RAW/topo.json" '" ".join(n["netdev"] for n in d["nics"] if n["netdev"])'); do
            { echo "== $nd"; ethtool "$nd"; ethtool -i "$nd"; ethtool -S "$nd"; } >> "$RAW/ethtool.txt" 2>&1
        done
        errs=$(awk '/^== /{nd=$2} /(err|drop|discard|crc|fcs|timeout|retry|out_of_seq|nak)/ && $NF+0>0 {print nd":"$1$NF}' "$RAW/ethtool.txt" | head -12 | tr '\n' ' ')
        res nic_error_counters "$([[ -z $errs ]] && echo PASS || echo WARN)" "${errs:-none}" "cumulative since driver load; see raw/ethtool.txt"
    fi
fi

# ============================================================================= 3. leftover state from earlier jobs

section leftovers
if [[ ${NGPU:-0} -gt 0 ]]; then
    worst=0; used_list=""
    for card in $(pyj "$RAW/topo.json" '" ".join(g["card"] for g in d["gpus"])'); do
        u=$(cat /sys/class/drm/$card/device/mem_info_vram_used 2>/dev/null); t=$(cat /sys/class/drm/$card/device/mem_info_vram_total 2>/dev/null)
        [[ -n $u && -n $t ]] || continue
        pct=$(calc "round(100*$u/$t,1)"); gb=$(calc "round($u/2**30,1)")
        used_list+="$card=${gb}G "
        metric "gpu.$card.vram_used_gb_idle" "$gb" lo
        [[ $(calc "int($pct>$worst)") == 1 ]] && worst=$pct
    done
    st=$(cmp_status "$worst" 2 10 lo)
    res gpu_vram_used_idle "$st" "max ${worst}% ($used_list)" "cluster setup waits for <=10%; >2% on an idle GPU is leftover memory"
    metric gpu.vram_used_pct_idle_max "$worst" lo
fi
pids=$(lsof -t /dev/kfd /dev/dri/renderD* 2>/dev/null | sort -u)
res gpu_processes "$([[ -z $pids ]] && echo PASS || echo WARN)" "${pids:-none}" "$(for p in $pids; do ps -o comm= -p "$p"; done | tr '\n' ' ')"
dstate=$(ps -eo stat,pid,comm | awk '$1 ~ /^D/ {print $2":"$3}' | head -10 | tr '\n' ' ')
res processes_D_state "$([[ -z $dstate ]] && echo PASS || echo WARN)" "${dstate:-none}" "uninterruptible processes often hold GPU memory after a job"
res zombies INFO "$(ps -eo stat | grep -c '^Z')"
for f in /sys/devices/system/node/node*/hugepages/hugepages-2048kB; do
    n=${f#/sys/devices/system/node/}; n=${n%%/*}
    cfg "hugepages.$n" "nr=$(cat $f/nr_hugepages) free=$(cat $f/free_hugepages)"
done
held=$(awk '/^HugePages_Total/{t=$2} /^HugePages_Free/{f=$2} END{print t-f}' /proc/meminfo)
holders=$(grep -H '^HugetlbPages:' /proc/[0-9]*/status 2>/dev/null | awk '$2>0 {split($1,a,"/"); print a[3]":"$2"kB"}' | head -5 | tr '\n' ' ')
res hugepages_in_use INFO "$held pages ($((held * 2 / 1024)) GiB)" "holders: ${holders:-none}"
# Servers and stores of earlier jobs (process names / python modules, not CI runner scripts)
leftover=$(ps -eo pid=,comm=,args= | awk '$2 ~ /^(mooncake_master|mooncake_store|mooncake_client|atomesh|lmcache_server|vllm)$/ || ($2 ~ /^python/ && $0 ~ / -m (atom\.entrypoints|sglang\.launch_server|vllm\.entrypoints|lmcache)/)' | head -5 | cut -c1-100 | tr '\n' ';')
res inference_processes "$([[ -z $leftover ]] && echo PASS || echo WARN)" "${leftover:-none}"
if have docker; then
    nc=$(docker ps -q 2>/dev/null | wc -l)
    res docker_containers "$([[ $nc -eq 0 ]] && echo PASS || echo WARN)" "$nc running"
fi
encnt=$(pgrep -c -f 'enroot|pyxis' 2>/dev/null); res enroot_processes "$([[ ${encnt:-0} -eq 0 ]] && echo PASS || echo WARN)" "${encnt:-0}"
shm=$(du -sm /dev/shm 2>/dev/null | cut -f1); res dev_shm_used "$([[ ${shm:-0} -lt 1024 ]] && echo PASS || echo WARN)" "${shm:-0} MiB"
orph=$(find /sys/fs/cgroup -maxdepth 6 -type d -name 'job_*' 2>/dev/null | wc -l)
res slurm_job_cgroups "$([[ $orph -eq 0 || -n ${SLURM_JOB_ID:-} ]] && echo PASS || echo WARN)" "$orph job cgroup dirs" "non-zero with no job running = orphaned cgroups"
ps -eo pcpu,pid,user,comm --sort=-pcpu | head -20 > "$RAW/top_cpu_idle.txt"
hot=$(awk 'NR>1 && $1>20 {print $4"("$1"%)"}' "$RAW/top_cpu_idle.txt" | head -6 | tr '\n' ' ')
res idle_cpu_consumers "$([[ -z $hot ]] && echo PASS || echo INFO)" "${hot:-none above 20%}" "busy-polling agents (e.g. WEKA) are normal; compare across nodes"
la=$(cut -d' ' -f1 /proc/loadavg); res load_average INFO "$la"; metric os.loadavg_idle "$la" lo

# ============================================================================= 4. CPU

section cpu
# 4a. clock: idle, one busy core, all cores busy (turbostat reads APERF/MPERF)
freq_test() { # NAME CPULIST_TO_LOAD
    local name=$1 load=$2 pids=() c
    for c in $load; do taskset -c "$c" python3 -c 'while 1: pass' & pids+=($!); done
    sleep 3
    if have turbostat && [[ $IS_ROOT -eq 1 ]]; then
        timeout 20 turbostat --quiet --show CPU,Busy%,Bzy_MHz,PkgWatt,CorWatt,PkgTmp,CoreTmp --interval 5 --num_iterations 1 > "$RAW/turbostat_$name.txt" 2>&1
    else
        sleep 5; grep 'cpu MHz' /proc/cpuinfo > "$RAW/cpuinfo_mhz_$name.txt"
    fi
    for c in "${pids[@]}"; do kill "$c" 2>/dev/null; done; wait "${pids[@]}" 2>/dev/null
}
ts_col() { # FILE COLUMN [CPU]   value of COLUMN in the summary row ('-') or for CPU
    awk -v col="$2" -v cpu="${3:--}" 'NR==1 {for (i=1;i<=NF;i++) h[$i]=i; next} $1==cpu || (cpu=="-" && $1=="-") {print $h[col]; exit}' "$1" 2>/dev/null
}
FIRST=$(echo "$TEST_CPUS" | awk '{print $1}')
freq_test idle ""
freq_test onecore "$FIRST"
freq_test allcore "$(seq 0 $((NCPU - 1)) | tr '\n' ' ')"
if [[ -s $RAW/turbostat_onecore.txt ]]; then
    one=$(ts_col "$RAW/turbostat_onecore.txt" Bzy_MHz "$FIRST"); all=$(ts_col "$RAW/turbostat_allcore.txt" Bzy_MHz)
    pkg_idle=$(ts_col "$RAW/turbostat_idle.txt" PkgWatt); pkg_all=$(ts_col "$RAW/turbostat_allcore.txt" PkgWatt)
    tmp_all=$(ts_col "$RAW/turbostat_allcore.txt" PkgTmp)
    if [[ -n $one ]]; then res freq_1core_mhz "$(cmp_status "$one" 4800 4300 hi)" "$one" "EPYC 9575F boosts to ~5000 MHz"
    else res freq_1core_mhz SKIP "no turbostat reading" "raw/turbostat_onecore.txt"; fi
    res freq_allcore_mhz INFO "${all:-n/a}" "pkg ${pkg_all:-?} W, ${tmp_all:-?} C; compare across nodes"
    res pkg_power_idle_w INFO "${pkg_idle:-n/a}"
    metric cpu.freq_1core_mhz "$one" hi; metric cpu.freq_allcore_mhz "$all" hi
    metric cpu.pkg_watt_idle "$pkg_idle" info; metric cpu.pkg_watt_allcore "$pkg_all" info; metric cpu.pkg_temp_allcore "$tmp_all" lo
else
    one=$(awk -F: '{print int($2)}' "$RAW/cpuinfo_mhz_onecore.txt" 2>/dev/null | sort -n | tail -1)
    res freq_1core_mhz INFO "${one:-n/a}" "from /proc/cpuinfo (turbostat unavailable)"; metric cpu.freq_1core_mhz "$one" hi
fi

# 4b. the burst probe, idle: latency after idle gaps, IPC and GHz from MSRs, IPC from perf
ipc_min=99; p50_max=0
for c in $TEST_CPUS; do
    helpers burst --cpu "$c" --gaps 0,0.2,2 --iters 3000 --msr > "$RAW/burst_idle_cpu$c.json" 2> "$RAW/burst_idle_cpu$c.err"
    f=$RAW/burst_idle_cpu$c.json
    ipc=$(pyj "$f" 'd["msr"].get("ipc")'); ghz=$(pyj "$f" 'd["msr"].get("ghz_p50")'); cyc=$(pyj "$f" 'd["msr"].get("cycles_p50_k")')
    p0=$(pyj "$f" 'd["latency"]["0ms"]["p50_us"]'); p02=$(pyj "$f" 'd["latency"]["0.2ms"]["p50_us"]'); p2=$(pyj "$f" 'd["latency"]["2ms"]["p50_us"]')
    p99=$(pyj "$f" 'd["latency"]["0.2ms"]["p99_us"]')
    metric "cpu.idle.cpu$c.ipc" "$ipc" hi; metric "cpu.idle.cpu$c.ghz" "$ghz" hi
    metric "cpu.idle.cpu$c.p50_us_gap0" "$p0" lo; metric "cpu.idle.cpu$c.p50_us_gap0.2ms" "$p02" lo
    metric "cpu.idle.cpu$c.p50_us_gap2ms" "$p2" lo; metric "cpu.idle.cpu$c.p99_us_gap0.2ms" "$p99" lo
    if [[ -n $ipc ]]; then
        res "burst_ipc_cpu$c" "$(cmp_status "$ipc" 6.8 6.0 hi)" "IPC $ipc, ${cyc}K cycles, ${ghz} GHz" "healthy ~7.2 / 224K cycles; g11 was 4.7 / 341K"
        [[ $(calc "int($ipc<$ipc_min)") == 1 ]] && ipc_min=$ipc
    else
        res "burst_ipc_cpu$c" SKIP "$(pyj "$f" 'd["msr"].get("error")')"
    fi
    res "burst_latency_cpu$c" INFO "p50 ${p0}/${p02}/${p2} us at gap 0/0.2/2 ms" "p99 at 0.2 ms: $p99 us"
    [[ -n $p02 && $(calc "int($p02>$p50_max)") == 1 ]] && p50_max=$p02
    if have perf; then
        perf stat -x, -e cycles,instructions -- taskset -c "$c" python3 "$TOOLS/nodecheck_helpers.py" loop --seconds 3 2> "$RAW/perf_ipc_cpu$c.txt" >/dev/null
        pipc=$(awk -F, '$3=="cycles"||$3~/^cycles/{c=$1} $3=="instructions"||$3~/^instructions/{i=$1} END{if (c>0) printf "%.2f", i/c}' "$RAW/perf_ipc_cpu$c.txt")
        [[ -n $pipc ]] && { metric "cpu.perf_ipc.cpu$c" "$pipc" hi; res "perf_ipc_cpu$c" INFO "$pipc" "perf stat over a 3 s burst loop (includes interpreter start)"; }
    fi
done
[[ $ipc_min != 99 ]] && metric cpu.idle.ipc_min "$ipc_min" hi
metric cpu.idle.p50_us_gap0.2ms_max "$p50_max" lo

# 4c. interrupt placement
for drv in amdgpu ionic mlx5; do
    irqs=$(awk -F: -v d="$drv" '$0 ~ d {gsub(/ /,"",$1); print $1}' /proc/interrupts)
    [[ -z $irqs ]] && continue
    aff=$(for i in $irqs; do cat /proc/irq/$i/effective_affinity_list 2>/dev/null; done | sort | uniq -c | sort -rn | head -6 | awk '{print "cpu"$2"x"$1}' | xargs)
    res "irq_affinity_$drv" INFO "$(echo $irqs | wc -w) IRQs; busiest targets: $aff"
    cfg "irq.affinity.$drv" "$aff"
done
helpers procsnap "$RAW/proc_idle_a"; sleep 10; helpers procsnap "$RAW/proc_idle_b"
helpers procdelta "$RAW/proc_idle_a" "$RAW/proc_idle_b" 10 > "$RAW/procdelta_idle.json"
sirq=$(pyj "$RAW/procdelta_idle.json" 'sum(v["total_per_s"] for v in d["softirq"].values())')
irq=$(pyj "$RAW/procdelta_idle.json" 'd.get("irq_total_per_s")')
res idle_interrupt_rate INFO "irq ${irq}/s, softirq ${sirq}/s" "top CPUs: $(pyj "$RAW/procdelta_idle.json" 'd.get("irq_top_cpus")')"
metric os.idle_irq_per_s "$irq" lo; metric os.idle_softirq_per_s "$sirq" lo

# ============================================================================= 5. memory

section memory
MEMBW=""
if have gcc; then
    if gcc -O2 -fopenmp -o "$TOOLS/membw" "$TOOLS/membw.c" 2> "$RAW/membw_build.txt" || gcc -O2 -o "$TOOLS/membw" "$TOOLS/membw.c" 2>> "$RAW/membw_build.txt"; then
        MEMBW=$TOOLS/membw
    fi
fi
if [[ -n $MEMBW ]] && have numactl; then
    nodes=$(ls -d /sys/devices/system/node/node[0-9]* | sed 's#.*node##' | sort -n)
    for n in $nodes; do
        th=$(numactl -H | awk -v n="$n" '$1=="node" && $2==n && $3=="cpus:" {print NF-3}')
        OMP_NUM_THREADS=$th OMP_PROC_BIND=spread numactl --cpunodebind="$n" --membind="$n" "$MEMBW" bw 1024 6 > "$RAW/membw_bw_node$n.txt" 2>&1
        bw=$(awk '{print $2}' "$RAW/membw_bw_node$n.txt")
        numactl --cpunodebind="$n" --membind="$n" "$MEMBW" lat 1024 20000000 > "$RAW/membw_lat_node$n.txt" 2>&1
        lat=$(awk '{print $2}' "$RAW/membw_lat_node$n.txt")
        res "mem_numa$n" INFO "triad ${bw} GB/s, latency ${lat} ns" "$th threads"
        metric "mem.numa$n.triad_gbps" "$bw" hi; metric "mem.numa$n.lat_ns" "$lat" lo
    done
    if [[ $(echo "$nodes" | wc -w) -gt 1 ]]; then
        a=$(echo $nodes | awk '{print $1}'); b=$(echo $nodes | awk '{print $2}')
        numactl --cpunodebind="$a" --membind="$b" "$MEMBW" lat 1024 20000000 > "$RAW/membw_lat_remote.txt" 2>&1
        rl=$(awk '{print $2}' "$RAW/membw_lat_remote.txt")
        res mem_remote_latency INFO "${rl} ns" "node$a CPUs -> node$b memory"; metric mem.remote_lat_ns "$rl" lo
    fi
else
    res memory_bench SKIP "needs gcc and numactl"
fi

# ============================================================================= 6. GPU

section gpu
if [[ $SKIP_GPU -eq 1 || ${NGPU:-0} -eq 0 ]]; then
    res gpu SKIP "no GPUs or --skip-gpu"
else
    for card in $(pyj "$RAW/topo.json" '" ".join(g["card"] for g in d["gpus"])'); do
        d=/sys/class/drm/$card/device
        hw=$(ls -d $d/hwmon/hwmon* 2>/dev/null | head -1)
        t=$(cat $hw/temp1_input 2>/dev/null); p=$(cat $hw/power1_average 2>/dev/null || cat $hw/power1_input 2>/dev/null)
        echo "$card temp_mC=$t power_uW=$p busy=$(cat $d/gpu_busy_percent 2>/dev/null) sclk=$(grep '\*' $d/pp_dpm_sclk 2>/dev/null | xargs) mclk=$(grep '\*' $d/pp_dpm_mclk 2>/dev/null | xargs)" >> "$RAW/gpu_sysfs_idle.txt"
    done
    res gpu_idle_state INFO "$(awk '{print $2}' "$RAW/gpu_sysfs_idle.txt" | sort | uniq -c | wc -l) readings" "raw/gpu_sysfs_idle.txt"
    busy=$(awk '{split($4,a,"="); if (a[2]+0>5) print $1}' "$RAW/gpu_sysfs_idle.txt" | xargs)
    res gpu_busy_idle "$([[ -z $busy ]] && echo PASS || echo WARN)" "${busy:-all GPUs idle}"
    if have amd-smi; then
        run amdsmi_metric 120 amd-smi metric
        run amdsmi_process 60 amd-smi process
        thr=$(grep -iE 'throttle|violation' "$RAW/amdsmi_metric.txt" | grep -viE ': *(0|N/A|NONE|no)\b' | head -3 | xargs)
        res gpu_throttle_flags INFO "${thr:-none reported}" "raw/amdsmi_metric.txt"
    fi
    if have rocm-bandwidth-test && [[ $MODE == full ]]; then
        log "rocm-bandwidth-test (can take a few minutes)"
        run rocm_bandwidth_test 900 rocm-bandwidth-test
        res rocm_bandwidth_test "$([[ $? -eq 0 ]] && echo INFO || echo WARN)" "see raw/rocm_bandwidth_test.txt" "compare the H2D/D2H and P2P matrices across nodes"
    fi
    if [[ $RUN_RVS -eq 1 ]] && have rvs; then
        conf=$(ls /opt/rocm/share/rocm-validation-suite/conf/gst_single.conf /opt/rocm/share/rocm-validation-suite/conf/*/gst_single.conf 2>/dev/null | head -1)
        [[ -n $conf ]] && run rvs_gst 1800 rvs -c "$conf"
        res rvs_gst INFO "${conf:-no gst_single.conf found}" "raw/rvs_gst.txt"
    fi
    if [[ -n $GPU_IMAGE ]] && have docker; then
        log "GPU bench in $GPU_IMAGE"
        cat > "$TOOLS/gpu_bench.py" <<'PY'
import json, time, torch
out = []
for i in range(torch.cuda.device_count()):
    torch.cuda.set_device(i)
    torch.cuda.init(); torch.empty(1, device=i)
    free, total = torch.cuda.mem_get_info(i)
    nontorch = (total - free - torch.cuda.memory_reserved(i)) / 2**30   # driver/runtime + other users
    n = 8192
    a = torch.randn(n, n, device=i, dtype=torch.bfloat16); b = torch.randn(n, n, device=i, dtype=torch.bfloat16)
    for _ in range(5): a @ b
    torch.cuda.synchronize(i); t = time.perf_counter(); it = 50
    for _ in range(it): a @ b
    torch.cuda.synchronize(i); tflops = 2 * n**3 * it / (time.perf_counter() - t) / 1e12
    h = torch.empty(1 << 28, dtype=torch.uint8, pin_memory=True); g = torch.empty(1 << 28, dtype=torch.uint8, device=i)
    g.copy_(h); torch.cuda.synchronize(i); t = time.perf_counter()
    for _ in range(10): g.copy_(h, non_blocking=True)
    torch.cuda.synchronize(i); h2d = 10 * (1 << 28) / (time.perf_counter() - t) / 1e9
    t = time.perf_counter()
    for _ in range(10): h.copy_(g, non_blocking=True)
    torch.cuda.synchronize(i); d2h = 10 * (1 << 28) / (time.perf_counter() - t) / 1e9
    out.append({"gpu": i, "nontorch_gb": round(nontorch, 2), "bf16_tflops": round(tflops, 1), "h2d_gbps": round(h2d, 1), "d2h_gbps": round(d2h, 1)})
    del a, b, g, h; torch.cuda.empty_cache()
print(json.dumps(out))
PY
        timeout 900 docker run --rm --network host --ipc host --device /dev/kfd --device /dev/dri --group-add video \
            --security-opt seccomp=unconfined -v "$TOOLS:/nc" "$GPU_IMAGE" python3 /nc/gpu_bench.py > "$RAW/gpu_bench.txt" 2>&1
        js=$(grep '^\[' "$RAW/gpu_bench.txt" | tail -1)
        if [[ -n $js ]]; then
            echo "$js" > "$RAW/gpu_bench.json"
            for i in $(pyj "$RAW/gpu_bench.json" '" ".join(str(r["gpu"]) for r in d)'); do
                for k in nontorch_gb:lo bf16_tflops:hi h2d_gbps:hi d2h_gbps:hi; do
                    metric "gpu.bench.gpu$i.${k%%:*}" "$(pyj "$RAW/gpu_bench.json" "d[$i]['${k%%:*}']")" "${k##*:}"
                done
            done
            res gpu_bench INFO "TFLOPS $(pyj "$RAW/gpu_bench.json" '" ".join(str(r["bf16_tflops"]) for r in d)')" \
                "non-torch GB $(pyj "$RAW/gpu_bench.json" '" ".join(str(r["nontorch_gb"]) for r in d)'); slow nodes g14/g18 showed +5-7 GB"
            mx=$(pyj "$RAW/gpu_bench.json" 'max(r["nontorch_gb"] for r in d)')
            res gpu_nontorch_memory "$(cmp_status "$mx" 3 8 lo)" "max ${mx} GB" "healthy ~1-2 GB after torch init"
            metric gpu.bench.nontorch_gb_max "$mx" lo
            metric gpu.bench.tflops_min "$(pyj "$RAW/gpu_bench.json" 'min(r["bf16_tflops"] for r in d)')" hi
        else
            res gpu_bench WARN "failed" "raw/gpu_bench.txt"
        fi
    fi
fi

# ============================================================================= 7. RDMA loopback bandwidth

section rdma
ROCM_RDMA=0
ib_write_bw --help 2>&1 | grep -q -- '--use_rocm' && ROCM_RDMA=1
gid_index() { # DEV -> first RoCE v2 GID with an IPv4-mapped address
    local f i
    for f in /sys/class/infiniband/$1/ports/1/gid_attrs/types/*; do
        i=${f##*/}
        [[ $(cat "$f" 2>/dev/null) == "RoCE v2" ]] || continue
        grep -q '^0000:0000:0000:0000:0000:ffff:' "/sys/class/infiniband/$1/ports/1/gids/$i" 2>/dev/null && { echo "$i"; return; }
    done
}
# perf_loop TOOL DEV GPU|- PORT SECONDS TAG  -> starts a loopback pair in the background, client output in raw/
perf_loop() {
    local tool=$1 dev=$2 gpu=$3 port=$4 secs=$5 tag=$6 gid mem=()
    gid=$(gid_index "$dev")
    [[ $gpu != - ]] && mem=(--use_rocm="$gpu")
    local args=(-d "$dev" -i 1 ${gid:+-x "$gid"} -p "$port" -F --report_gbits -q 4 -s 1048576 -D "$secs" "${mem[@]}")
    timeout $((secs + 30)) "$tool" "${args[@]}" > "$RAW/${tag}_server.txt" 2>&1 &
    BG_PIDS+=($!)
    sleep 2
    timeout $((secs + 30)) "$tool" "${args[@]}" 127.0.0.1 > "$RAW/${tag}_client.txt" 2>&1 &
    BG_PIDS+=($!)
}
bw_of() { awk '$1 ~ /^[0-9]+$/ && NF>=4 {bw=$4} END{print bw}' "$RAW/$1_client.txt" 2>/dev/null; }

NICS=$(pyj "$RAW/topo.json" '" ".join(n["dev"]+":"+str(n["nearest_gpu"] if n["nearest_gpu"] is not None else "-") for n in d["nics"])')
RDMA_OK=0
if [[ $SKIP_RDMA -eq 1 || -z $NICS ]]; then
    res rdma SKIP "no RDMA devices or --skip-rdma"
elif [[ $IS_ROOT -eq 0 ]] || ! have ib_write_bw; then
    res rdma SKIP "needs root and perftest (ib_write_bw / ib_read_bw)"
else
    RDMA_OK=1
    res perftest_gpu_memory "$([[ $ROCM_RDMA -eq 1 ]] && echo PASS || echo WARN)" "$([[ $ROCM_RDMA -eq 1 ]] && echo 'perftest has --use_rocm' || echo 'perftest built without ROCm: host memory only')" \
        "the slow-node symptom is the NIC reading GPU memory; a ROCm build of perftest makes this test much more telling"
    tests="write"; [[ $MODE == full ]] && tests="write read"
    port=18515
    for tst in $tests; do
        for mem in gpu host; do
            [[ $mem == gpu && $ROCM_RDMA -eq 0 ]] && continue
            [[ $mem == host && $MODE == quick && $ROCM_RDMA -eq 1 ]] && continue
            vals=()
            for spec in $NICS; do
                dev=${spec%%:*}; g=${spec##*:}; [[ $mem == host ]] && g=-
                perf_loop "ib_${tst}_bw" "$dev" "$g" "$port" 6 "rdma_${tst}_${mem}_$dev"; port=$((port + 2))
                wait "${BG_PIDS[@]}" 2>/dev/null; BG_PIDS=()
                bw=$(bw_of "rdma_${tst}_${mem}_$dev")
                metric "rdma.$dev.${tst}_${mem}_gbps" "$bw" hi
                vals+=("$dev=${bw:-fail}")
            done
            mn=$(printf '%s\n' "${vals[@]}" | awk -F= '$2+0>0 {print $2}' | sort -n | head -1)
            nfail=$(printf '%s\n' "${vals[@]}" | grep -c '=fail')
            res "rdma_${tst}_${mem}_loopback" "$([[ $nfail -eq 0 ]] && echo INFO || echo FAIL)" "min ${mn:-n/a} Gb/s" "${vals[*]}"
            metric "rdma.${tst}_${mem}_gbps_min" "$mn" hi
        done
    done
fi

# ============================================================================= 8. CPU work under load

section load
# One measurement: the burst probe on every test CPU (sequentially) plus /proc deltas.
measure() { # NAME
    local name=$1 c f
    helpers procsnap "$RAW/proc_${name}_a"; local t0=$(date +%s.%N)
    for c in $TEST_CPUS; do
        helpers burst --cpu "$c" --gaps 0,0.2 --iters 2000 --msr --msr-iters 5000 > "$RAW/burst_${name}_cpu$c.json" 2>/dev/null
    done
    local t1=$(date +%s.%N); helpers procsnap "$RAW/proc_${name}_b"
    helpers procdelta "$RAW/proc_${name}_a" "$RAW/proc_${name}_b" "$(calc "$t1-$t0")" --cpu "$FIRST" > "$RAW/procdelta_${name}.json"
    local p02s=() ratios=() ipcs=() c base
    for c in $TEST_CPUS; do
        f=$RAW/burst_${name}_cpu$c.json
        local p=$(pyj "$f" 'd["latency"]["0.2ms"]["p50_us"]') i=$(pyj "$f" 'd["msr"].get("ipc")')
        base=$(pyj "$RAW/burst_base_cpu$c.json" 'd["latency"]["0.2ms"]["p50_us"]')
        [[ -n $p ]] && p02s+=("$p")
        [[ -n $p && -n $base ]] && ratios+=("$(calc "round($p/$base,3)")")
        [[ -n $i ]] && ipcs+=("$i")
        metric "load.$name.cpu$c.p50_us_gap0.2ms" "$p" lo; metric "load.$name.cpu$c.ipc" "$i" hi
    done
    local avg=$(calc "round(sum([${p02s[*]/%/,}])/max(1,len([${p02s[*]/%/,}])),2)")
    local rmax="" imin=""
    [[ ${#ratios[@]} -gt 0 ]] && rmax=$(calc "max([${ratios[*]/%/,}])")
    [[ ${#ipcs[@]} -gt 0 ]] && imin=$(calc "min([${ipcs[*]/%/,}])")
    local sirq=$(pyj "$RAW/procdelta_${name}.json" 'sum(v["total_per_s"] for v in d["softirq"].values())')
    local cpu_si=$(pyj "$RAW/procdelta_${name}.json" 'd["cpu_time_pct"].get("softirq")')
    metric "load.$name.p50_us_gap0.2ms_avg" "$avg" lo; metric "load.$name.ratio_max" "$rmax" lo
    metric "load.$name.ipc_min" "$imin" hi; metric "load.$name.softirq_per_s" "$sirq" lo; metric "load.$name.softirq_cpu_pct" "$cpu_si" lo
    if [[ $name == base ]]; then
        res "load_$name" INFO "burst p50 ${avg} us at 0.2 ms gap, IPC min ${imin:-n/a}" "softirq ${sirq}/s"
    else
        res "load_$name" "$(cmp_status "${rmax:-0}" 1.25 1.6 lo)" "burst x${rmax:-?} vs idle (p50 ${avg} us), IPC min ${imin:-n/a}" \
            "softirq ${sirq}/s (${cpu_si}% CPU); thresholds provisional, compare nodes"
    fi
}
start_cpu_hogs() { # all CPUs except the test CPUs
    local c
    for c in $(seq 0 $((NCPU - 1))); do
        [[ " $TEST_CPUS " == *" $c "* ]] && continue
        timeout $((LOAD_SECONDS + 10)) taskset -c "$c" python3 -c 'while 1: pass' & BG_PIDS+=($!)
    done
}
start_mem_hogs() { # [CPUS_TO_KEEP_FREE]  (default: the test CPUs)
    [[ -z $MEMBW ]] && return 1
    local others keep=${1:-$TEST_CPUS}
    others=$(seq 0 $((NCPU - 1)) | grep -vxF -f <(echo "$keep" | tr ' ' '\n') | paste -sd, -)
    OMP_NUM_THREADS=$((NCPU / 2)) timeout $((LOAD_SECONDS + 10)) taskset -c "$others" "$MEMBW" load "$LOAD_SECONDS" > /dev/null 2>&1 & BG_PIDS+=($!)
}
start_rdma_all() { # every NIC at once: write + read loopback from GPU memory when possible
    local spec dev g port=19515
    for spec in $NICS; do
        dev=${spec%%:*}; g=${spec##*:}; [[ $ROCM_RDMA -eq 0 ]] && g=-
        perf_loop ib_write_bw "$dev" "$g" "$port" "$LOAD_SECONDS" "load_write_$dev"; port=$((port + 2))
        perf_loop ib_read_bw "$dev" "$g" "$port" "$LOAD_SECONDS" "load_read_$dev"; port=$((port + 2))
    done
}
stop_load() { local p; for p in "${BG_PIDS[@]:-}"; do [[ -n $p ]] && kill "$p" 2>/dev/null; done; wait 2>/dev/null; BG_PIDS=(); sleep 2; }

for c in $TEST_CPUS; do cp "$RAW/burst_idle_cpu$c.json" "$RAW/burst_base_cpu$c.json"; done
measure base
if [[ $MODE == full ]]; then
    log "load: all other CPUs busy"; start_cpu_hogs; sleep 4; measure cpu; stop_load
    if start_mem_hogs; then log "load: memory bandwidth"; sleep 4; measure mem; stop_load; fi
fi
if [[ $RDMA_OK -eq 1 ]]; then
    log "load: RDMA loopback on every NIC ($([[ $ROCM_RDMA -eq 1 ]] && echo GPU || echo host) memory)"
    start_rdma_all; sleep 6; measure rdma; stop_load
    for spec in $NICS; do dev=${spec%%:*}; metric "load.rdma.$dev.write_gbps" "$(bw_of "load_write_$dev")" hi; done
fi
# Job-like: RDMA on every NIC + memory traffic + 8 CPUs running bursts at the same time
log "load: job-like mix (RDMA + memory + concurrent bursts)"
JOB_CPUS=$(echo "$TEST_CPUS $(seq 8 16 $((NCPU - 1)) | tr '\n' ' ')" | tr ' ' '\n' | grep . | sort -nu | head -8 | xargs)
[[ $RDMA_OK -eq 1 ]] && start_rdma_all
start_mem_hogs "$JOB_CPUS"
sleep 6
helpers procsnap "$RAW/proc_jobsim_a"; t0=$(date +%s.%N)
jpids=()
for c in $JOB_CPUS; do
    helpers burst --cpu "$c" --gaps 0.2 --iters 3000 --msr --msr-iters 5000 > "$RAW/burst_jobsim_cpu$c.json" 2>/dev/null & jpids+=($!)
done
wait "${jpids[@]}" 2>/dev/null
t1=$(date +%s.%N); helpers procsnap "$RAW/proc_jobsim_b"
helpers procdelta "$RAW/proc_jobsim_a" "$RAW/proc_jobsim_b" "$(calc "$t1-$t0")" > "$RAW/procdelta_jobsim.json"
stop_load
jp=(); ji=()
for c in $JOB_CPUS; do
    p=$(pyj "$RAW/burst_jobsim_cpu$c.json" 'd["latency"]["0.2ms"]["p50_us"]'); i=$(pyj "$RAW/burst_jobsim_cpu$c.json" 'd["msr"].get("ipc")')
    [[ -n $p ]] && jp+=("$p"); [[ -n $i ]] && ji+=("$i")
done
base_avg=$(pyj "$RAW/burst_base_cpu$FIRST.json" 'd["latency"]["0.2ms"]["p50_us"]')
javg=$(calc "round(sum([${jp[*]/%/,}])/max(1,len([${jp[*]/%/,}])),2)"); jmax=$(calc "max([${jp[*]/%/,}] or [0])")
jratio=$(calc "round($javg/$base_avg,3)" 2>/dev/null)
jimin=""; [[ ${#ji[@]} -gt 0 ]] && jimin=$(calc "min([${ji[*]/%/,}])")
jsirq=$(pyj "$RAW/procdelta_jobsim.json" 'sum(v["total_per_s"] for v in d["softirq"].values())')
metric load.jobsim.p50_us_avg "$javg" lo; metric load.jobsim.p50_us_max "$jmax" lo; metric load.jobsim.ratio "$jratio" lo
metric load.jobsim.ipc_min "$jimin" hi; metric load.jobsim.softirq_per_s "$jsirq" lo
res load_jobsim "$(cmp_status "${jratio:-0}" 1.25 1.6 lo)" "burst x${jratio} vs idle (avg ${javg} us, worst ${jmax} us on CPUs $JOB_CPUS)" \
    "IPC min ${jimin:-n/a}, softirq ${jsirq}/s; the closest test to the ATOM prefill host path. Thresholds provisional: compare with g37/g50"

# NIC error counters after the traffic
if [[ $RDMA_OK -eq 1 ]]; then
    for dev in $(pyj "$RAW/topo.json" '" ".join(n["dev"] for n in d["nics"])'); do
        [[ -d /sys/class/infiniband/$dev/ports/1/hw_counters ]] && grep -H . /sys/class/infiniband/$dev/ports/1/hw_counters/* 2>/dev/null | sed "s#.*/hw_counters/#$dev #" >> "$RAW/rdma_hw_counters_end.txt"
    done
    if [[ -s $RAW/rdma_hw_counters_start.txt && -s $RAW/rdma_hw_counters_end.txt ]]; then
        delta=$(awk 'NR==FNR {split($2,a,":"); s[$1" "a[1]]=a[2]; next} {split($2,a,":"); k=$1" "a[1]; d=a[2]-s[k]; if (d>0 && k ~ /(err|retr|timeout|seq|nak|drop|discard|cnp|ecn)/) print $1":"a[1]"+"d}' \
            "$RAW/rdma_hw_counters_start.txt" "$RAW/rdma_hw_counters_end.txt" | head -10 | xargs)
        res rdma_counter_delta "$([[ -z $delta ]] && echo PASS || echo WARN)" "${delta:-no error/retry/congestion counters moved}" "during this script's RDMA traffic"
    fi
fi

# ============================================================================= 9. report

printf 'end\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$META"
helpers summarize "$OUT"
tar -czf "$OUT.tar.gz" -C "$(dirname "$OUT")" "$(basename "$OUT")" 2>/dev/null
log "done: $OUT/report.txt  (send back $OUT.tar.gz)"
