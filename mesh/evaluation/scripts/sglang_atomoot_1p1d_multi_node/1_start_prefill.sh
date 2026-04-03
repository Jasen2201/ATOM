#!/usr/bin/env bash
# =============================================================================
# Script 1: Start Prefill Server - SGLang PD Multi-Node
# Run this INSIDE the docker container on the prefill node.
#
# Verified config: TP=4, aiter, DeepSeek-R1, MI355X
#
# Usage:
#   PREFILL_HANDSHAKE_IP=<mgmt_ip> bash 1_start_prefill.sh
#
#   # Second prefill instance (GPUs 4-7)
#   PREFILL_HANDSHAKE_IP=<mgmt_ip> GPU_IDS=4,5,6,7 \
#       PREFILL_PORT=8011 BOOTSTRAP_PORT=8999 \
#       MOONCAKE_CONFIG_FILE=mooncake_prefill2.json \
#       LOG_FILE=prefill2.log bash 1_start_prefill.sh
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/it-share/models/deepseek-ai/DeepSeek-R1}"
TP_SIZE="${TP_SIZE:-4}"
GPU_IDS="${GPU_IDS:-0,1,2,3}"
PREFILL_PORT="${PREFILL_PORT:-8010}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
MEM_FRACTION="${MEM_FRACTION:-0.85}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e4m3}"
TRANSFER_BACKEND="${TRANSFER_BACKEND:-mooncake}"
MOONCAKE_PROTOCOL="${MOONCAKE_PROTOCOL:-rdma}"
QUICK_REDUCE_QUANT="${QUICK_REDUCE_QUANT:-INT4}"
PAGE_SIZE="${PAGE_SIZE:-1}"
CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-16384}"
MAX_RUNNING_REQUESTS="${MAX_RUNNING_REQUESTS:-128}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-aiter}"
WATCHDOG_TIMEOUT="${WATCHDOG_TIMEOUT:-3600}"
LOG_LEVEL="${LOG_LEVEL:-warning}"

# RDMA devices: comma-separated list
IB_DEVICE="${IB_DEVICE:-rdma0,rdma1,rdma2,rdma3,rdma4,rdma5,rdma6,rdma7}"

# IP for Mooncake Transfer Engine P2P handshake (must be TCP-reachable cross-node)
PREFILL_HANDSHAKE_IP="${PREFILL_HANDSHAKE_IP:?ERROR: Set PREFILL_HANDSHAKE_IP to this node management IP}"

# Log and config file names (support multiple prefill instances)
MOONCAKE_CONFIG_FILE="${MOONCAKE_CONFIG_FILE:-mooncake_prefill.json}"
LOG_FILE="${LOG_FILE:-prefill.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo ""
echo "============================================================"
echo "  SGLang Prefill Server - PD Disaggregation"
echo "============================================================"
echo " Model:            ${MODEL}"
echo " TP size:          ${TP_SIZE}"
echo " GPU IDs:          ${GPU_IDS}"
echo " Port:             ${PREFILL_PORT}"
echo " Bootstrap port:   ${BOOTSTRAP_PORT}"
echo " Handshake IP:     ${PREFILL_HANDSHAKE_IP}"
echo " Transfer:         ${TRANSFER_BACKEND} (${MOONCAKE_PROTOCOL})"
echo " Attention backend: ${ATTENTION_BACKEND}"
echo " IB devices:       ${IB_DEVICE}"
echo " Log file:         ${LOG_DIR}/${LOG_FILE}"
echo "============================================================"

# ---- Environment (aligned with verified single-node config) ----
export HIP_VISIBLE_DEVICES="${GPU_IDS}"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.model_wrapper
export SGLANG_USE_AITER=1
export SGLANG_AITER_FP8_PREFILL_ATTN=0
export AITER_QUICK_REDUCE_QUANTIZATION="${QUICK_REDUCE_QUANT}"
export ATOM_ENABLE_DS_QKNORM_QUANT_FUSION=1
export SGLANG_HOST_IP="${PREFILL_HANDSHAKE_IP}"

# ---- LD_LIBRARY_PATH ----
MOONCAKE_LIB="${MOONCAKE_LIB:-/opt/venv/lib/python3.12/site-packages/mooncake}"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

# ---- Generate Mooncake config for RDMA protocol ----
export MOONCAKE_CONFIG_PATH="${SCRIPT_DIR}/${MOONCAKE_CONFIG_FILE}"
cat > "${MOONCAKE_CONFIG_PATH}" <<MCEOF
{
    "prefill_url": "${PREFILL_HANDSHAKE_IP}:${PREFILL_PORT}",
    "protocol": "${MOONCAKE_PROTOCOL}"
}
MCEOF
echo "[config] Mooncake config written to ${MOONCAKE_CONFIG_PATH}"

echo "[launch] Starting Prefill server (TP=${TP_SIZE}, attention=${ATTENTION_BACKEND})..."
python3 -m sglang.launch_server \
    --model-path "${MODEL}" \
    --host 0.0.0.0 \
    --port "${PREFILL_PORT}" \
    --trust-remote-code \
    --tp-size "${TP_SIZE}" \
    --kv-cache-dtype "${KV_CACHE_DTYPE}" \
    --attention-backend "${ATTENTION_BACKEND}" \
    --mem-fraction-static "${MEM_FRACTION}" \
    --page-size "${PAGE_SIZE}" \
    --chunked-prefill-size "${CHUNKED_PREFILL_SIZE}" \
    --max-running-requests "${MAX_RUNNING_REQUESTS}" \
    --disable-radix-cache \
    --log-level "${LOG_LEVEL}" \
    --watchdog-timeout "${WATCHDOG_TIMEOUT}" \
    --disaggregation-mode prefill \
    --disaggregation-transfer-backend "${TRANSFER_BACKEND}" \
    --disaggregation-bootstrap-port "${BOOTSTRAP_PORT}" \
    --disaggregation-ib-device "${IB_DEVICE}" \
    2>&1 | tee "${LOG_DIR}/${LOG_FILE}"
