#!/usr/bin/env bash
# =============================================================================
# Script 3: Start PD Proxy - SGLang 1P1D Multi-Node (sgl-model-gateway)
# Routes requests through Prefill -> Decode across two nodes via smg.
# Run this INSIDE the docker container on either node.
# =============================================================================
set -euo pipefail

# ---- Configuration ----
# Management IPs (HTTP API reachable)
PREFILL_MGMT_IP="${PREFILL_MGMT_IP:-10.2.224.4}"
DECODE_MGMT_IP="${DECODE_MGMT_IP:-10.2.224.6}"
PREFILL_PORT="${PREFILL_PORT:-8010}"
DECODE_PORT="${DECODE_PORT:-8020}"
PROXY_PORT="${PROXY_PORT:-8080}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLICY="${POLICY:-round_robin}"
BACKEND="${BACKEND:-sglang}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-29100}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# sgl-model-gateway binary path
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
SMG_BIN="${SMG_BIN:-/usr/local/bin/smg}"
if [[ ! -x "${SMG_BIN}" ]]; then
    SMG_BIN="${PROJECT_ROOT}/target/release/smg"
fi

echo ""
echo "============================================================"
echo "  PD Proxy - SGLang 1P1D Multi-Node (sgl-model-gateway)"
echo "============================================================"
echo " Prefill:   http://${PREFILL_MGMT_IP}:${PREFILL_PORT}"
echo " Decode:    http://${DECODE_MGMT_IP}:${DECODE_PORT}"
echo " Proxy:     0.0.0.0:${PROXY_PORT}"
echo " Bootstrap: ${BOOTSTRAP_PORT}"
echo " Policy:    ${POLICY}"
echo " Backend:   ${BACKEND}"
echo " SMG bin:   ${SMG_BIN}"
echo "============================================================"

# ---- Verify smg binary exists ----
if [[ ! -x "${SMG_BIN}" ]]; then
    echo "FATAL: smg binary not found at ${SMG_BIN}"
    exit 1
fi

# ---- Wait for both servers ----
wait_for_server() {
    local ip=$1
    local port=$2
    local name=$3
    local start=$(date +%s)
    echo "[wait] Waiting for ${name} on ${ip}:${port}..."
    while true; do
        if curl -s "http://${ip}:${port}/v1/models" > /dev/null 2>&1; then
            local elapsed=$(( $(date +%s) - start ))
            echo "[wait] ${name} is ready (${elapsed}s)."
            return 0
        fi
        local now=$(date +%s)
        if (( now - start >= TIMEOUT_SECONDS )); then
            echo "[wait] TIMEOUT waiting for ${name} (${TIMEOUT_SECONDS}s)"
            return 1
        fi
        sleep 5
    done
}

wait_for_server "${PREFILL_MGMT_IP}" "${PREFILL_PORT}" "Prefill" || {
    echo "FATAL: Prefill server not reachable at ${PREFILL_MGMT_IP}:${PREFILL_PORT}"
    exit 1
}

wait_for_server "${DECODE_MGMT_IP}" "${DECODE_PORT}" "Decode" || {
    echo "FATAL: Decode server not reachable at ${DECODE_MGMT_IP}:${DECODE_PORT}"
    exit 1
}

# ---- Launch sgl-model-gateway in PD mode ----
echo "[launch] Starting sgl-model-gateway PD proxy on port ${PROXY_PORT}..."

"${SMG_BIN}" launch \
    --host 0.0.0.0 \
    --port "${PROXY_PORT}" \
    --pd-disaggregation \
    --prefill "http://${PREFILL_MGMT_IP}:${PREFILL_PORT}" "${BOOTSTRAP_PORT}" \
    --decode "http://${DECODE_MGMT_IP}:${DECODE_PORT}" \
    --policy "${POLICY}" \
    --backend "${BACKEND}" \
    --log-dir "${LOG_DIR}" \
    --log-level info \
    --disable-health-check \
    --prometheus-port "${PROMETHEUS_PORT}" \
    2>&1 | tee "${LOG_DIR}/proxy_smg.log"
