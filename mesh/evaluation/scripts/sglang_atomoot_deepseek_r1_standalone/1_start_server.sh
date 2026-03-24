#!/usr/bin/env bash
# =============================================================================
# Script 1: Standalone SGLang Server for DeepSeek-R1 (671B MoE)
# Run this INSIDE the docker container (rocm/atom-mesh).
# Model: DeepSeek-R1, TP=8, EP=8, single-node 8-GPU
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/mnt/nfs/huggingface/DeepSeek-R1}"
TP_SIZE="${TP_SIZE:-8}"
EP_SIZE="${EP_SIZE:-1}"
SERVER_PORT="${SERVER_PORT:-8013}"
SERVER_HOST="${SERVER_HOST:-localhost}"
KV_CACHE_DTYPE="${KV_CACHE_DTYPE:-fp8_e4m3}"
MEM_FRACTION="${MEM_FRACTION:-0.8}"
PAGE_SIZE="${PAGE_SIZE:-1}"
CUDA_GRAPH_MAX_BS="${CUDA_GRAPH_MAX_BS:-16}"
DISABLE_CUDA_GRAPH="${DISABLE_CUDA_GRAPH:-0}"
QUICK_REDUCE_QUANT="${QUICK_REDUCE_QUANT:-INT4}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo ""
echo "============================================================"
echo "  DeepSeek-R1 Standalone SGLang Server - $(hostname)"
echo "============================================================"
echo " Model:              ${MODEL}"
echo " TP size:            ${TP_SIZE}"
echo " EP size:            ${EP_SIZE}"
echo " Port:               ${SERVER_PORT}"
echo " Host:               ${SERVER_HOST}"
echo " KV cache dtype:     ${KV_CACHE_DTYPE}"
echo " Mem fraction:       ${MEM_FRACTION}"
echo " Page size:          ${PAGE_SIZE}"
echo " CUDA graph max BS:  ${CUDA_GRAPH_MAX_BS}"
echo " Quick reduce quant: ${QUICK_REDUCE_QUANT}"
echo " Disable CUDA graph: ${DISABLE_CUDA_GRAPH}"
echo "============================================================"

# ---- Environment ----
export AITER_QUICK_REDUCE_QUANTIZATION="${QUICK_REDUCE_QUANT}"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.oot
export SGLANG_AITER_FP8_PREFILL_ATTN=0
export PYTHONFAULTHANDLER=1
export TORCHINDUCTOR_COMPILE_THREADS=128
export AMD_SERIALIZE_KERNEL=1

echo "[launch] Starting SGLang server for DeepSeek-R1..."
python3 -m sglang.launch_server \
    --model-path "${MODEL}" \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}" \
    --trust-remote-code \
    --tensor-parallel-size "${TP_SIZE}" \
    --expert-parallel-size "${EP_SIZE}" \
    --kv-cache-dtype "${KV_CACHE_DTYPE}" \
    --mem-fraction-static "${MEM_FRACTION}" \
    --page-size "${PAGE_SIZE}" \
    --cuda-graph-max-bs "${CUDA_GRAPH_MAX_BS}" \
    $([ "${DISABLE_CUDA_GRAPH}" = "1" ] && echo "--disable-cuda-graph") \
    2>&1 | tee "${LOG_DIR}/server.log"
