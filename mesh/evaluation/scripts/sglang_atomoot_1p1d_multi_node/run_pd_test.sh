#!/usr/bin/env bash
# =============================================================================
# PD Disaggregation End-to-End Test Orchestrator
#
# Runs the full pipeline from the HOST machine via SSH:
#   sync scripts → cleanup → prefill → decode → proxy → GSM8K eval → collect results
#
# Cluster: mia1-p01-g05 / mia1-p01-g07, 8× MI355X, rdma0-7
#
# Usage:
#   # Default 1P1D TP4+TP8, prefill=g05, decode=g07
#   bash run_pd_test.sh
#
#   # 1P1D TP4 prefill + TP8 decode
#   PREFILL_TP=4 bash run_pd_test.sh
#
#   # 2P1D TP4 prefill (2 instances on g05) + TP8 decode on g07
#   NUM_PREFILLS=2 PREFILL_TP=4 bash run_pd_test.sh
#
#   # 1P1D + benchmark serving
#   RUN_BENCH=1 bash run_pd_test.sh
#
# Environment overrides:
#   PREFILL_NODE     - prefill node shortname (default: g05)
#   DECODE_NODE      - decode node shortname (default: g07)
#   PREFILL_TP       - prefill TP size (default: 4)
#   DECODE_TP        - decode TP size (default: 8)
#   NUM_PREFILLS     - number of prefill instances (default: 1)
#   NUM_DECODES      - number of decode instances (default: 1)
#   CONTAINER        - docker container name (default: mesh_dev)
#   RUN_BENCH        - also run benchmark serving (default: 0)
#   SKIP_CLEANUP     - skip cleanup step (default: 0)
#   SKIP_EVAL        - skip GSM8K eval (default: 0)
# =============================================================================
set -euo pipefail

# ---- Node Map ----
declare -A NODE_MGMT_IP=(
    ["g05"]="10.28.104.181"
    ["g07"]="10.28.104.183"
)

# ---- Configuration ----
PREFILL_NODE="${PREFILL_NODE:-g05}"
DECODE_NODE="${DECODE_NODE:-g07}"
PREFILL_TP="${PREFILL_TP:-4}"
DECODE_TP="${DECODE_TP:-8}"
NUM_PREFILLS="${NUM_PREFILLS:-1}"
NUM_DECODES="${NUM_DECODES:-1}"
CONTAINER="${CONTAINER:-mesh_dev}"
MODEL="${MODEL:-/it-share/models/deepseek-ai/DeepSeek-R1}"
RUN_BENCH="${RUN_BENCH:-0}"
SKIP_CLEANUP="${SKIP_CLEANUP:-0}"
SKIP_EVAL="${SKIP_EVAL:-0}"
POLICY="${POLICY:-random}"

# Resolve IPs
PREFILL_IP="${NODE_MGMT_IP[$PREFILL_NODE]}"
DECODE_IP="${NODE_MGMT_IP[$DECODE_NODE]}"

# Local script directory (on the machine running this orchestrator)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Common eval scripts (eval_gsm8k.py etc.)
COMMON_DIR="$(cd "${SCRIPT_DIR}/../../common" && pwd)"

# Path inside the container where scripts will be copied to
REMOTE_SCRIPT_DIR="/workspace/pd_scripts"
REMOTE_COMMON_DIR="/workspace/common"
LOG_DIR="${REMOTE_SCRIPT_DIR}/logs"

echo ""
echo "################################################################"
echo "  PD Disaggregation Test - ${NUM_PREFILLS}P${NUM_DECODES}D  TP${PREFILL_TP}+TP${DECODE_TP}"
echo "################################################################"
echo " Prefill node: ${PREFILL_NODE} (${PREFILL_IP}), TP=${PREFILL_TP}, instances=${NUM_PREFILLS}"
echo " Decode node:  ${DECODE_NODE} (${DECODE_IP}), TP=${DECODE_TP}, instances=${NUM_DECODES}"
echo " Container:    ${CONTAINER}"
echo " Model:        ${MODEL}"
echo " Policy:       ${POLICY}"
echo "################################################################"
echo ""

# ---- Helper: run command in docker on a node ----
docker_exec() {
    local node_ip=$1
    shift
    ssh "${node_ip}" "docker exec ${CONTAINER} bash -c '$*'"
}

docker_exec_d() {
    local node_ip=$1
    shift
    ssh "${node_ip}" "docker exec -d ${CONTAINER} bash -c '$*'"
}

# ---- Helper: wait for server ----
wait_for_server() {
    local node_ip=$1
    local port=$2
    local name=$3
    local timeout=${4:-300}
    local start=$(date +%s)

    echo "[wait] Waiting for ${name} at ${node_ip}:${port} (timeout=${timeout}s)..."
    while true; do
        if ssh "${node_ip}" "docker exec ${CONTAINER} curl -s http://127.0.0.1:${port}/v1/models" > /dev/null 2>&1; then
            local elapsed=$(( $(date +%s) - start ))
            echo "[ok] ${name} is ready (${elapsed}s)"
            return 0
        fi
        local now=$(date +%s)
        if (( now - start >= timeout )); then
            echo "[FAIL] TIMEOUT waiting for ${name} (${timeout}s)"
            echo "[FAIL] Last 30 lines of log:"
            ssh "${node_ip}" "docker exec ${CONTAINER} tail -30 ${LOG_DIR}/${name,,}.log 2>/dev/null" || true
            return 1
        fi
        sleep 10
    done
}

# ============================================================
# Step 0: Sync scripts into containers via docker cp
# ============================================================
echo "=== Step 0: Sync scripts to containers ==="
for node_ip in "${PREFILL_IP}" "${DECODE_IP}"; do
    echo "[sync] Copying scripts to ${node_ip}:${CONTAINER}:${REMOTE_SCRIPT_DIR}..."
    ssh "${node_ip}" "docker exec ${CONTAINER} mkdir -p ${REMOTE_SCRIPT_DIR} ${REMOTE_COMMON_DIR}"
    # Copy script files
    for f in "${SCRIPT_DIR}"/*.sh; do
        scp -q "${f}" "${node_ip}:/tmp/_pd_script_$(basename "${f}")"
        ssh "${node_ip}" "docker cp /tmp/_pd_script_$(basename "${f}") ${CONTAINER}:${REMOTE_SCRIPT_DIR}/$(basename "${f}") && rm -f /tmp/_pd_script_$(basename "${f}")"
    done
    # Copy common eval scripts
    if [[ -d "${COMMON_DIR}" ]]; then
        for f in "${COMMON_DIR}"/*.py; do
            [[ -f "${f}" ]] || continue
            scp -q "${f}" "${node_ip}:/tmp/_pd_common_$(basename "${f}")"
            ssh "${node_ip}" "docker cp /tmp/_pd_common_$(basename "${f}") ${CONTAINER}:${REMOTE_COMMON_DIR}/$(basename "${f}") && rm -f /tmp/_pd_common_$(basename "${f}")"
        done
    fi
done
echo "[ok] Scripts synced."
echo ""

# ============================================================
# Step 1: Cleanup
# ============================================================
if [[ "${SKIP_CLEANUP}" != "1" ]]; then
    echo "=== Step 1: Cleanup old processes ==="
    for node_ip in "${PREFILL_IP}" "${DECODE_IP}"; do
        echo "[cleanup] Killing old processes on ${node_ip}..."
        ssh "${node_ip}" "docker exec ${CONTAINER} bash -c 'pkill -f \"sglang.launch_server\" 2>/dev/null; pkill -f \"smg launch\" 2>/dev/null'" || true
    done
    echo "[cleanup] Waiting 5s for processes to terminate..."
    sleep 5

    # Verify
    for node_ip in "${PREFILL_IP}" "${DECODE_IP}"; do
        remaining=$(ssh "${node_ip}" "docker exec ${CONTAINER} pgrep -f 'sglang.launch_server' 2>/dev/null | wc -l" || echo "0")
        if [[ "${remaining}" -gt 0 ]]; then
            echo "[warn] ${remaining} sglang processes still running on ${node_ip}, force killing..."
            ssh "${node_ip}" "docker exec ${CONTAINER} pkill -9 -f 'sglang.launch_server' 2>/dev/null" || true
            sleep 2
        fi
    done
    echo "[ok] Cleanup done."
    echo ""
fi

# ============================================================
# Step 2: Start Prefill
# ============================================================
echo "=== Step 2: Start Prefill (${NUM_PREFILLS} instance(s), TP=${PREFILL_TP}) ==="

# Ensure logs dir exists
ssh "${PREFILL_IP}" "docker exec ${CONTAINER} mkdir -p ${LOG_DIR}"

if [[ "${NUM_PREFILLS}" -eq 1 ]]; then
    # Single prefill
    if [[ "${PREFILL_TP}" -eq 8 ]]; then
        GPU_IDS="0,1,2,3,4,5,6,7"
    else
        GPU_IDS="0,1,2,3"
    fi
    echo "[prefill] Starting prefill-1: TP=${PREFILL_TP}, GPUs=${GPU_IDS}, port=8010"
    ssh "${PREFILL_IP}" "docker exec -d ${CONTAINER} bash -c '\
        PREFILL_HANDSHAKE_IP=${PREFILL_IP} \
        MODEL=${MODEL} \
        TP_SIZE=${PREFILL_TP} \
        GPU_IDS=${GPU_IDS} \
        PREFILL_PORT=8010 \
        BOOTSTRAP_PORT=8998 \
        LOG_FILE=prefill.log \
        bash ${REMOTE_SCRIPT_DIR}/1_start_prefill.sh \
        > ${LOG_DIR}/prefill.log 2>&1'"
elif [[ "${NUM_PREFILLS}" -eq 2 ]]; then
    # Two prefill instances (TP=4 each, split GPUs)
    echo "[prefill] Starting prefill-1: TP=${PREFILL_TP}, GPUs=0,1,2,3, port=8010"
    ssh "${PREFILL_IP}" "docker exec -d ${CONTAINER} bash -c '\
        PREFILL_HANDSHAKE_IP=${PREFILL_IP} \
        MODEL=${MODEL} \
        TP_SIZE=${PREFILL_TP} \
        GPU_IDS=0,1,2,3 \
        PREFILL_PORT=8010 \
        BOOTSTRAP_PORT=8998 \
        MOONCAKE_CONFIG_FILE=mooncake_prefill.json \
        LOG_FILE=prefill.log \
        bash ${REMOTE_SCRIPT_DIR}/1_start_prefill.sh \
        > ${LOG_DIR}/prefill.log 2>&1'"

    sleep 3

    echo "[prefill] Starting prefill-2: TP=${PREFILL_TP}, GPUs=4,5,6,7, port=8011"
    ssh "${PREFILL_IP}" "docker exec -d ${CONTAINER} bash -c '\
        PREFILL_HANDSHAKE_IP=${PREFILL_IP} \
        MODEL=${MODEL} \
        TP_SIZE=${PREFILL_TP} \
        GPU_IDS=4,5,6,7 \
        PREFILL_PORT=8011 \
        BOOTSTRAP_PORT=8999 \
        MOONCAKE_CONFIG_FILE=mooncake_prefill2.json \
        LOG_FILE=prefill2.log \
        bash ${REMOTE_SCRIPT_DIR}/1_start_prefill.sh \
        > ${LOG_DIR}/prefill2.log 2>&1'"
fi
echo ""

# ============================================================
# Step 3: Start Decode
# ============================================================
echo "=== Step 3: Start Decode (${NUM_DECODES} instance(s), TP=${DECODE_TP}) ==="

ssh "${DECODE_IP}" "docker exec ${CONTAINER} mkdir -p ${LOG_DIR}"

echo "[decode] Starting decode-1: TP=${DECODE_TP}, port=8020"
ssh "${DECODE_IP}" "docker exec -d ${CONTAINER} bash -c '\
    DECODE_HANDSHAKE_IP=${DECODE_IP} \
    MODEL=${MODEL} \
    TP_SIZE=${DECODE_TP} \
    LOG_FILE=decode.log \
    bash ${REMOTE_SCRIPT_DIR}/2_start_decode.sh \
    > ${LOG_DIR}/decode.log 2>&1'"

if [[ "${NUM_DECODES}" -ge 2 ]]; then
    # Second decode on a different node or same node different GPUs
    DECODE_NODE_2="${DECODE_NODE_2:-${PREFILL_NODE}}"
    DECODE_IP_2="${NODE_MGMT_IP[$DECODE_NODE_2]}"
    echo "[decode] Starting decode-2 on ${DECODE_NODE_2}: TP=${DECODE_TP}, port=8020"
    ssh "${DECODE_IP_2}" "docker exec -d ${CONTAINER} bash -c '\
        DECODE_HANDSHAKE_IP=${DECODE_IP_2} \
        MODEL=${MODEL} \
        TP_SIZE=${DECODE_TP} \
        LOG_FILE=decode2.log \
        bash ${REMOTE_SCRIPT_DIR}/2_start_decode.sh \
        > ${LOG_DIR}/decode2.log 2>&1'"
fi
echo ""

# ============================================================
# Step 4: Wait for all servers
# ============================================================
echo "=== Step 4: Wait for servers to be ready ==="

wait_for_server "${PREFILL_IP}" 8010 "Prefill" 300 || exit 1

if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
    wait_for_server "${PREFILL_IP}" 8011 "Prefill2" 300 || exit 1
fi

wait_for_server "${DECODE_IP}" 8020 "Decode" 300 || exit 1

if [[ "${NUM_DECODES}" -ge 2 ]]; then
    DECODE_IP_2="${NODE_MGMT_IP[${DECODE_NODE_2:-${PREFILL_NODE}}]}"
    wait_for_server "${DECODE_IP_2}" 8020 "Decode2" 300 || exit 1
fi

echo "[ok] All servers ready."
echo ""

# ============================================================
# Step 5: Start Proxy
# ============================================================
echo "=== Step 5: Start Proxy ==="

PROXY_ENV="PREFILL_MGMT_IP=${PREFILL_IP} DECODE_MGMT_IP=${DECODE_IP} POLICY=${POLICY}"

if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
    PROXY_ENV="${PROXY_ENV} NUM_PREFILLS=2 PREFILL_PORT_2=8011 BOOTSTRAP_PORT_2=8999"
fi

if [[ "${NUM_DECODES}" -ge 2 ]]; then
    DECODE_IP_2="${NODE_MGMT_IP[${DECODE_NODE_2:-${PREFILL_NODE}}]}"
    PROXY_ENV="${PROXY_ENV} NUM_DECODES=2 DECODE_MGMT_IP_2=${DECODE_IP_2}"
fi

echo "[proxy] Starting proxy on ${PREFILL_NODE}..."
ssh "${PREFILL_IP}" "docker exec -d ${CONTAINER} bash -c '\
    ${PROXY_ENV} \
    bash ${REMOTE_SCRIPT_DIR}/3_start_proxy_smg.sh \
    > ${LOG_DIR}/proxy_smg.log 2>&1'"

# Wait for proxy
sleep 5
wait_for_server "${PREFILL_IP}" 8080 "Proxy" 120 || exit 1
echo ""

# ============================================================
# Step 6: Run GSM8K Evaluation
# ============================================================
if [[ "${SKIP_EVAL}" != "1" ]]; then
    echo "=== Step 6: Run GSM8K Evaluation ==="
    ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
        PROXY_HOST=127.0.0.1 MODEL=${MODEL} \
        bash ${REMOTE_SCRIPT_DIR}/4_eval_gsm8k.sh'"
    echo ""
fi

# ============================================================
# Step 7: Run Benchmark (optional)
# ============================================================
if [[ "${RUN_BENCH}" == "1" ]]; then
    echo "=== Step 7: Run Benchmark Serving ==="
    ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
        SERVER_HOST=127.0.0.1 MODEL=${MODEL} \
        bash ${REMOTE_SCRIPT_DIR}/5_bench_serving.sh'"
    echo ""
fi

# ============================================================
# Step 8: Collect Results
# ============================================================
echo "=== Collect Results ==="

echo ""
echo "--- KV Transfer Speed (from prefill logs) ---"
ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
    grep \"Req Time Stats\" ${LOG_DIR}/prefill.log 2>/dev/null \
    | grep -v \"HEALTH_CHECK\|input len=4,\" \
    | grep -oP \"transfer_speed=\S+\" \
    | tail -10'" || echo "(no transfer stats found)"

if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
    echo ""
    echo "--- Prefill-2 KV Transfer Speed ---"
    ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
        grep \"Req Time Stats\" ${LOG_DIR}/prefill2.log 2>/dev/null \
        | grep -v \"HEALTH_CHECK\|input len=4,\" \
        | grep -oP \"transfer_speed=\S+\" \
        | tail -10'" || echo "(no transfer stats found)"

    echo ""
    echo "--- Request Distribution ---"
    p1_count=$(ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
        grep \"Req Time Stats\" ${LOG_DIR}/prefill.log 2>/dev/null \
        | grep -v \"HEALTH_CHECK\|input len=4,\" | wc -l'" || echo "0")
    p2_count=$(ssh "${PREFILL_IP}" "docker exec ${CONTAINER} bash -c '\
        grep \"Req Time Stats\" ${LOG_DIR}/prefill2.log 2>/dev/null \
        | grep -v \"HEALTH_CHECK\|input len=4,\" | wc -l'" || echo "0")
    echo "  Prefill-1: ${p1_count} requests"
    echo "  Prefill-2: ${p2_count} requests"
fi

echo ""
echo "--- GSM8K Results ---"
ssh "${PREFILL_IP}" "docker exec ${CONTAINER} cat ${LOG_DIR}/gsm8k_results.json 2>/dev/null" || echo "(no results file)"

echo ""
echo "################################################################"
echo "  Test Complete: ${NUM_PREFILLS}P${NUM_DECODES}D  TP${PREFILL_TP}+TP${DECODE_TP}"
echo "  Prefill: ${PREFILL_NODE}, Decode: ${DECODE_NODE}"
echo "################################################################"
