#!/usr/bin/env bash
# =============================================================================
# Script 5: Start Standalone vLLM Server (single node, no PD disaggregation)
# Run this INSIDE the docker container on a single node (e.g., g38).
#
# Non-PD baseline: plain vLLM with TP=8 on a single node.
# No MooncakeConnector, no kv-transfer, no proxy needed.
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/mnt/raid0/deepseek-r1-FP8-Dynamic}"
SERVED_MODEL="${SERVED_MODEL:-deepseek-r1}"
TP_SIZE="${TP_SIZE:-8}"
GPU_IDS="${GPU_IDS:-0,1,2,3,4,5,6,7}"
STANDALONE_PORT="${STANDALONE_PORT:-8000}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.9}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

EAGER_FLAG=""
if [[ "${ENFORCE_EAGER}" == "1" ]]; then
    EAGER_FLAG="--enforce-eager"
fi

echo ""
echo "============================================================"
echo "  Standalone vLLM Server - $(hostname)"
echo "============================================================"
echo " Model:          ${MODEL}"
echo " Served name:    ${SERVED_MODEL}"
echo " TP size:        ${TP_SIZE}"
echo " GPU IDs:        ${GPU_IDS}"
echo " Port:           ${STANDALONE_PORT}"
echo " Max model len:  ${MAX_MODEL_LEN}"
echo " GPU mem util:   ${GPU_MEM_UTIL}"
echo "============================================================"

export HIP_VISIBLE_DEVICES="${GPU_IDS}"

echo "[launch] Starting Standalone vLLM server..."
python -m vllm.entrypoints.openai.api_server \
    --model "${MODEL}" \
    --served-model-name "${SERVED_MODEL}" \
    --tensor-parallel-size "${TP_SIZE}" \
    --gpu-memory-utilization "${GPU_MEM_UTIL}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --trust-remote-code \
    ${EAGER_FLAG} \
    --port "${STANDALONE_PORT}" \
    --host 0.0.0.0 \
    2>&1 | tee "${LOG_DIR}/standalone.log"
