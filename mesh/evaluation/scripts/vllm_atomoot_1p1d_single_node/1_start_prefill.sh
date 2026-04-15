#!/usr/bin/env bash
# =============================================================================
# Script 1: Start Prefill Server (kv_producer) - Single Node 1P1D
# Model: Qwen3-8B-FP8-dynamic, single GPU (GPU 0)
# =============================================================================
set -euo pipefail

# ---- Configuration ----
MODEL="${MODEL:-/mnt/raid0/RedHatAI/Qwen3-8B-FP8-dynamic}"
SERVED_MODEL="${SERVED_MODEL:-qwen3-8b}"
TP_SIZE="${TP_SIZE:-1}"
GPU_IDS="${GPU_IDS:-0}"
PREFILL_PORT="${PREFILL_PORT:-8010}"
BOOTSTRAP_PORT="${BOOTSTRAP_PORT:-8998}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.9}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
MOONCAKE_PROTOCOL="${MOONCAKE_PROTOCOL:-local}"

# Single-node: use localhost
PREFILL_IP="${PREFILL_IP:-127.0.0.1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# ---- LD_LIBRARY_PATH ----
MOONCAKE_LIB="${MOONCAKE_LIB:-/opt/venv/lib/python3.12/site-packages/mooncake}"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

# ---- KV transfer config ----
KV_CONFIG="{\"kv_connector\":\"MooncakeConnector\",\"kv_role\":\"kv_producer\",\"kv_connector_extra_config\":{\"mooncake_protocol\":\"${MOONCAKE_PROTOCOL}\"}}"

EAGER_FLAG=""
if [[ "${ENFORCE_EAGER}" == "1" ]]; then
    EAGER_FLAG="--enforce-eager"
fi

echo ""
echo "============================================================"
echo "  Prefill Server (kv_producer) - Single Node 1P1D"
echo "============================================================"
echo " Model:          ${MODEL}"
echo " Served name:    ${SERVED_MODEL}"
echo " TP size:        ${TP_SIZE}"
echo " GPU IDs:        ${GPU_IDS}"
echo " Port:           ${PREFILL_PORT}"
echo " Bootstrap port: ${BOOTSTRAP_PORT}"
echo " IP:             ${PREFILL_IP}"
echo " Protocol:       ${MOONCAKE_PROTOCOL}"
echo "============================================================"

export HIP_VISIBLE_DEVICES="${GPU_IDS}"
export VLLM_MOONCAKE_BOOTSTRAP_PORT="${BOOTSTRAP_PORT}"
export VLLM_HOST_IP="${PREFILL_IP}"

echo "[launch] Starting Prefill server..."
python -m vllm.entrypoints.openai.api_server \
    --model "${MODEL}" \
    --served-model-name "${SERVED_MODEL}" \
    --tensor-parallel-size "${TP_SIZE}" \
    --gpu-memory-utilization "${GPU_MEM_UTIL}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --trust-remote-code \
    ${EAGER_FLAG} \
    --port "${PREFILL_PORT}" \
    --host 0.0.0.0 \
    --no-enable-prefix-caching \
    --kv-transfer-config "${KV_CONFIG}" \
    2>&1 | tee "${LOG_DIR}/prefill.log"
