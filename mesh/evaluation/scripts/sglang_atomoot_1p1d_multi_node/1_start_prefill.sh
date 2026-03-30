#!/usr/bin/env bash
# =============================================================================
# Script 1: Start Prefill Server - SGLang 1P1D Multi-Node
# Run this INSIDE the docker container on the prefill node (node09).
# Model: DeepSeek-R1, TP=8, EP=8, 8 GPUs
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/mnt/nfs/huggingface/DeepSeek-R1}"
TP_SIZE="${TP_SIZE:-8}"
EP_SIZE="${EP_SIZE:-1}"
GPU_IDS="${GPU_IDS:-0,1,2,3,4,5,6,7}"
PREFILL_PORT="${PREFILL_PORT:-8010}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
MEM_FRACTION="${MEM_FRACTION:-0.8}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e4m3}"
TRANSFER_BACKEND="${TRANSFER_BACKEND:-mooncake}"
MOONCAKE_PROTOCOL="${MOONCAKE_PROTOCOL:-rdma}"
QUICK_REDUCE_QUANT="${QUICK_REDUCE_QUANT:-INT4}"
PAGE_SIZE="${PAGE_SIZE:-1}"
CUDA_GRAPH_BS_START="${CUDA_GRAPH_BS_START:-1}"
CUDA_GRAPH_BS_END="${CUDA_GRAPH_BS_END:-32}"
CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-16384}"
MAX_RUNNING_REQUESTS="${MAX_RUNNING_REQUESTS:-128}"
SCRIPT_DIR_FOR_IB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IB_DEVICE="${IB_DEVICE:-${SCRIPT_DIR_FOR_IB}/ib_device_map.json}"

# IP for Mooncake Transfer Engine P2P handshake (must be TCP-reachable cross-node)
# Use management IP for handshake/RPC; RDMA data transfer uses --disaggregation-ib-device
PREFILL_HANDSHAKE_IP="${PREFILL_HANDSHAKE_IP:-10.2.224.4}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo ""
echo "============================================================"
echo "  SGLang Prefill Server - Multi-Node 1P1D"
echo "============================================================"
echo " Model:          ${MODEL}"
echo " TP size:        ${TP_SIZE}"
echo " EP size:        ${EP_SIZE}"
echo " GPU IDs:        ${GPU_IDS}"
echo " Port:           ${PREFILL_PORT}"
echo " Bootstrap port: ${BOOTSTRAP_PORT}"
echo " Handshake IP:   ${PREFILL_HANDSHAKE_IP}"
echo " Transfer:       ${TRANSFER_BACKEND} (${MOONCAKE_PROTOCOL})"
echo "============================================================"

# ---- Environment ----
export HIP_VISIBLE_DEVICES="${GPU_IDS}"
export AITER_QUICK_REDUCE_QUANTIZATION="${QUICK_REDUCE_QUANT}"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.model_wrapper
export SGLANG_USE_AITER=1
export SGLANG_AITER_FP8_PREFILL_ATTN=0
export PYTHONFAULTHANDLER=1
export TORCHINDUCTOR_COMPILE_THREADS=128
export AMD_SERIALIZE_KERNEL=1
# Mooncake TransferEngine P2P handshake needs TCP-reachable IP (management network)
# RDMA data path is configured via --disaggregation-ib-device
export SGLANG_HOST_IP="${PREFILL_HANDSHAKE_IP}"

# ---- LD_LIBRARY_PATH ----
MOONCAKE_LIB="${MOONCAKE_LIB:-/opt/venv/lib/python3.12/site-packages/mooncake}"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

# ---- Generate Mooncake config for RDMA protocol ----
export MOONCAKE_CONFIG_PATH="${SCRIPT_DIR}/mooncake_prefill.json"
cat > "${MOONCAKE_CONFIG_PATH}" <<MCEOF
{
    "prefill_url": "${PREFILL_HANDSHAKE_IP}:${PREFILL_PORT}",
    "protocol": "${MOONCAKE_PROTOCOL}"
}
MCEOF
echo "[config] Mooncake config written to ${MOONCAKE_CONFIG_PATH}"

echo "[launch] Starting Prefill server..."
python3 -m sglang.launch_server \
    --model-path "${MODEL}" \
    --host 0.0.0.0 \
    --port "${PREFILL_PORT}" \
    --trust-remote-code \
    --tensor-parallel-size "${TP_SIZE}" \
    --expert-parallel-size "${EP_SIZE}" \
    --kv-cache-dtype "${KV_CACHE_DTYPE}" \
    --attention-backend aiter \
    --mem-fraction-static "${MEM_FRACTION}" \
    --page-size "${PAGE_SIZE}" \
    --cuda-graph-bs $(seq ${CUDA_GRAPH_BS_START} ${CUDA_GRAPH_BS_END}) \
    --chunked-prefill-size "${CHUNKED_PREFILL_SIZE}" \
    --max-running-requests "${MAX_RUNNING_REQUESTS}" \
    --disable-radix-cache \
    --disaggregation-mode prefill \
    --disaggregation-transfer-backend "${TRANSFER_BACKEND}" \
    --disaggregation-bootstrap-port "${BOOTSTRAP_PORT}" \
    --disaggregation-ib-device "${IB_DEVICE}" \
    2>&1 | tee "${LOG_DIR}/prefill.log"
