#!/usr/bin/env bash
# =============================================================================
# Script 2: Start Decode Server - SGLang 1P1D Single Node
# Model: Qwen3-8B-FP8-dynamic, single GPU (GPU 1)
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/mnt/raid0/RedHatAI/Qwen3-235B-A22B-FP8-dynamic/}"
TP_SIZE="${TP_SIZE:-4}"
EP_SIZE="${EP_SIZE:-4}"
GPU_IDS="${GPU_IDS:-4,5,6,7}"
DECODE_PORT="${DECODE_PORT:-8020}"
PREFILL_PORT="${PREFILL_PORT:-8010}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
MEM_FRACTION="${MEM_FRACTION:-0.8}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e4m3}"
TRANSFER_BACKEND="${TRANSFER_BACKEND:-mooncake}"
MOONCAKE_PROTOCOL="${MOONCAKE_PROTOCOL:-local}"
QUICK_REDUCE_QUANT="${QUICK_REDUCE_QUANT:-INT4}"

# Single-node: use localhost
DECODE_IP="${DECODE_IP:-127.0.0.1}"
PREFILL_IP="${PREFILL_IP:-127.0.0.1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo ""
echo "============================================================"
echo "  SGLang Decode Server - Single Node 1P1D"
echo "============================================================"
echo " Model:      ${MODEL}"
echo " TP size:    ${TP_SIZE}"
echo " GPU IDs:    ${GPU_IDS}"
echo " Port:       ${DECODE_PORT}"
echo " IP:         ${DECODE_IP}"
echo " Transfer:   ${TRANSFER_BACKEND} (${MOONCAKE_PROTOCOL})"
echo "============================================================"

# ---- Environment ----
export HIP_VISIBLE_DEVICES="${GPU_IDS}"
export AITER_QUICK_REDUCE_QUANTIZATION="${QUICK_REDUCE_QUANT}"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.model_wrapper
export PYTHONFAULTHANDLER=1
export TORCHINDUCTOR_COMPILE_THREADS=128
export AMD_SERIALIZE_KERNEL=1

echo "[launch] Starting Decode server..."
python3 -m sglang.launch_server \
    --model-path "${MODEL}" \
    --host 0.0.0.0 \
    --port "${DECODE_PORT}" \
    --trust-remote-code \
    --tensor-parallel-size "${TP_SIZE}" \
    --expert-parallel-size "${EP_SIZE}" \
    --kv-cache-dtype "${KV_CACHE_DTYPE}" \
    --mem-fraction-static "${MEM_FRACTION}" \
    --page-size 1024 \
    --cuda-graph-max-bs 16 \
    --disaggregation-mode decode \
    --disaggregation-transfer-backend "${TRANSFER_BACKEND}" \
    --disaggregation-bootstrap-port "${BOOTSTRAP_PORT}" \
    2>&1 | tee "${LOG_DIR}/decode.log"
