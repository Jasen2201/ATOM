#!/usr/bin/env bash
# =============================================================================
# Script 2: Start Decode Server - SGLang PD Multi-Node
# Run this INSIDE the docker container on the decode node.
#
# Verified config: TP=8, aiter, DeepSeek-R1, MI355X, cuda-graph enabled
#
# Usage:
#   DECODE_HANDSHAKE_IP=<mgmt_ip> bash 2_start_decode.sh
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/it-share/models/deepseek-ai/DeepSeek-R1}"
TP_SIZE="${TP_SIZE:-8}"
GPU_IDS="${GPU_IDS:-0,1,2,3,4,5,6,7}"
DECODE_PORT="${DECODE_PORT:-8020}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
MEM_FRACTION="${MEM_FRACTION:-0.85}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e4m3}"
TRANSFER_BACKEND="${TRANSFER_BACKEND:-mooncake}"
MOONCAKE_PROTOCOL="${MOONCAKE_PROTOCOL:-rdma}"
QUICK_REDUCE_QUANT="${QUICK_REDUCE_QUANT:-INT4}"
PAGE_SIZE="${PAGE_SIZE:-1}"
MAX_RUNNING_REQUESTS="${MAX_RUNNING_REQUESTS:-128}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-aiter}"
CUDA_GRAPH_BS_START="${CUDA_GRAPH_BS_START:-1}"
CUDA_GRAPH_BS_END="${CUDA_GRAPH_BS_END:-32}"
WATCHDOG_TIMEOUT="${WATCHDOG_TIMEOUT:-3600}"
LOG_LEVEL="${LOG_LEVEL:-warning}"

# RDMA devices: comma-separated list
IB_DEVICE="${IB_DEVICE:-rdma0,rdma1,rdma2,rdma3,rdma4,rdma5,rdma6,rdma7}"

# IP for Mooncake Transfer Engine P2P handshake
DECODE_HANDSHAKE_IP="${DECODE_HANDSHAKE_IP:?ERROR: Set DECODE_HANDSHAKE_IP to this node management IP}"

LOG_FILE="${LOG_FILE:-decode.log}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo ""
echo "============================================================"
echo "  SGLang Decode Server - PD Disaggregation"
echo "============================================================"
echo " Model:            ${MODEL}"
echo " TP size:          ${TP_SIZE}"
echo " GPU IDs:          ${GPU_IDS}"
echo " Port:             ${DECODE_PORT}"
echo " Handshake IP:     ${DECODE_HANDSHAKE_IP}"
echo " Transfer:         ${TRANSFER_BACKEND} (${MOONCAKE_PROTOCOL})"
echo " Attention backend: ${ATTENTION_BACKEND}"
echo " IB devices:       ${IB_DEVICE}"
echo " Log file:         ${LOG_DIR}/${LOG_FILE}"
echo "============================================================"

# ---- Environment (aligned with verified single-node config) ----
export HIP_VISIBLE_DEVICES="${GPU_IDS}"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.models
export SGLANG_USE_AITER=1
export SGLANG_AITER_FP8_PREFILL_ATTN=0
export AITER_QUICK_REDUCE_QUANTIZATION="${QUICK_REDUCE_QUANT}"
export ATOM_ENABLE_DS_QKNORM_QUANT_FUSION=1
export SGLANG_HOST_IP="${DECODE_HANDSHAKE_IP}"

# ---- LD_LIBRARY_PATH ----
MOONCAKE_LIB="${MOONCAKE_LIB:-/opt/venv/lib/python3.12/site-packages/mooncake}"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

echo "[launch] Starting Decode server (TP=${TP_SIZE}, attention=${ATTENTION_BACKEND})..."
python3 -m sglang.launch_server \
    --model-path "${MODEL}" \
    --host 0.0.0.0 \
    --port "${DECODE_PORT}" \
    --trust-remote-code \
    --tp-size "${TP_SIZE}" \
    --kv-cache-dtype "${KV_CACHE_DTYPE}" \
    --attention-backend "${ATTENTION_BACKEND}" \
    --mem-fraction-static "${MEM_FRACTION}" \
    --page-size "${PAGE_SIZE}" \
    --max-running-requests "${MAX_RUNNING_REQUESTS}" \
    --cuda-graph-bs $(seq ${CUDA_GRAPH_BS_START} ${CUDA_GRAPH_BS_END}) \
    --disable-radix-cache \
    --log-level "${LOG_LEVEL}" \
    --watchdog-timeout "${WATCHDOG_TIMEOUT}" \
    --disaggregation-mode decode \
    --disaggregation-transfer-backend "${TRANSFER_BACKEND}" \
    --disaggregation-bootstrap-port "${BOOTSTRAP_PORT}" \
    --disaggregation-ib-device "${IB_DEVICE}" \
    2>&1 | tee "${LOG_DIR}/${LOG_FILE}"
