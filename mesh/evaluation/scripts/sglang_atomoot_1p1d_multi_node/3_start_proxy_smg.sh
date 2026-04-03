#!/usr/bin/env bash
# =============================================================================
# Script 3: Start PD Proxy - SGLang Multi-Node (sgl-model-gateway)
# Routes requests through Prefill -> Decode across nodes via smg.
# Run this INSIDE the docker container on the prefill node.
#
# Supports multi-prefill and multi-decode via environment variables.
#
# Environment:
#   mia1-p01-g05 (10.28.104.181) / mia1-p01-g07 (10.28.104.183)
#
# Usage:
#   # 1P1D (default)
#   PREFILL_MGMT_IP=10.28.104.181 DECODE_MGMT_IP=10.28.104.183 bash 3_start_proxy_smg.sh
#
#   # 2P1D (two prefill instances on same node)
#   PREFILL_MGMT_IP=10.28.104.181 DECODE_MGMT_IP=10.28.104.183 \
#       NUM_PREFILLS=2 PREFILL_PORT_2=8011 BOOTSTRAP_PORT_2=8999 \
#       bash 3_start_proxy_smg.sh
#
#   # 1P2D (two decode nodes)
#   PREFILL_MGMT_IP=10.28.104.181 \
#       DECODE_MGMT_IP=10.28.104.183 DECODE_MGMT_IP_2=10.28.104.181 \
#       NUM_DECODES=2 bash 3_start_proxy_smg.sh
# =============================================================================
set -euo pipefail

# ---- Configuration ----
PREFILL_MGMT_IP="${PREFILL_MGMT_IP:?ERROR: Set PREFILL_MGMT_IP (g05=10.28.104.181, g07=10.28.104.183)}"
DECODE_MGMT_IP="${DECODE_MGMT_IP:?ERROR: Set DECODE_MGMT_IP (g05=10.28.104.181, g07=10.28.104.183)}"

PREFILL_PORT="${PREFILL_PORT:-8010}"
DECODE_PORT="${DECODE_PORT:-8020}"
PROXY_PORT="${PROXY_PORT:-8080}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLICY="${POLICY:-random}"
BACKEND="${BACKEND:-sglang}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-29100}"

# Multi-instance support
NUM_PREFILLS="${NUM_PREFILLS:-1}"
NUM_DECODES="${NUM_DECODES:-1}"
PREFILL_PORT_2="${PREFILL_PORT_2:-8011}"
BOOTSTRAP_PORT_2="${BOOTSTRAP_PORT_2:-8999}"
PREFILL_MGMT_IP_2="${PREFILL_MGMT_IP_2:-${PREFILL_MGMT_IP}}"
DECODE_MGMT_IP_2="${DECODE_MGMT_IP_2:-}"
DECODE_PORT_2="${DECODE_PORT_2:-8020}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# smg binary
SMG_BIN="${SMG_BIN:-/usr/local/bin/smg}"
if [[ ! -x "${SMG_BIN}" ]]; then
    PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
    SMG_BIN="${PROJECT_ROOT}/target/release/smg"
fi

echo ""
echo "============================================================"
echo "  PD Proxy - SGLang Multi-Node (sgl-model-gateway)"
echo "============================================================"
echo " Prefill 1:  http://${PREFILL_MGMT_IP}:${PREFILL_PORT} (bootstrap=${BOOTSTRAP_PORT})"
if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
echo " Prefill 2:  http://${PREFILL_MGMT_IP_2}:${PREFILL_PORT_2} (bootstrap=${BOOTSTRAP_PORT_2})"
fi
echo " Decode 1:   http://${DECODE_MGMT_IP}:${DECODE_PORT}"
if [[ "${NUM_DECODES}" -ge 2 ]] && [[ -n "${DECODE_MGMT_IP_2}" ]]; then
echo " Decode 2:   http://${DECODE_MGMT_IP_2}:${DECODE_PORT_2}"
fi
echo " Proxy:      0.0.0.0:${PROXY_PORT}"
echo " Policy:     ${POLICY}"
echo " Backend:    ${BACKEND}"
echo " SMG bin:    ${SMG_BIN}"
echo "============================================================"

# ---- Verify smg binary ----
if [[ ! -x "${SMG_BIN}" ]]; then
    echo "FATAL: smg binary not found at ${SMG_BIN}"
    exit 1
fi

# ---- Wait for servers ----
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

wait_for_server "${PREFILL_MGMT_IP}" "${PREFILL_PORT}" "Prefill-1" || exit 1

if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
    wait_for_server "${PREFILL_MGMT_IP_2}" "${PREFILL_PORT_2}" "Prefill-2" || exit 1
fi

wait_for_server "${DECODE_MGMT_IP}" "${DECODE_PORT}" "Decode-1" || exit 1

if [[ "${NUM_DECODES}" -ge 2 ]] && [[ -n "${DECODE_MGMT_IP_2}" ]]; then
    wait_for_server "${DECODE_MGMT_IP_2}" "${DECODE_PORT_2}" "Decode-2" || exit 1
fi

# ---- Build smg command ----
SMG_CMD=(
    "${SMG_BIN}" launch
    --host 0.0.0.0
    --port "${PROXY_PORT}"
    --pd-disaggregation
    --prefill "http://${PREFILL_MGMT_IP}:${PREFILL_PORT}" "${BOOTSTRAP_PORT}"
)

if [[ "${NUM_PREFILLS}" -ge 2 ]]; then
    SMG_CMD+=(--prefill "http://${PREFILL_MGMT_IP_2}:${PREFILL_PORT_2}" "${BOOTSTRAP_PORT_2}")
fi

SMG_CMD+=(--decode "http://${DECODE_MGMT_IP}:${DECODE_PORT}")

if [[ "${NUM_DECODES}" -ge 2 ]] && [[ -n "${DECODE_MGMT_IP_2}" ]]; then
    SMG_CMD+=(--decode "http://${DECODE_MGMT_IP_2}:${DECODE_PORT_2}")
fi

SMG_CMD+=(
    --policy "${POLICY}"
    --backend "${BACKEND}"
    --log-dir "${LOG_DIR}"
    --log-level info
    --disable-health-check
    --prometheus-port "${PROMETHEUS_PORT}"
)

echo "[launch] Starting sgl-model-gateway PD proxy on port ${PROXY_PORT}..."
"${SMG_CMD[@]}" 2>&1 | tee "${LOG_DIR}/proxy_smg.log"
