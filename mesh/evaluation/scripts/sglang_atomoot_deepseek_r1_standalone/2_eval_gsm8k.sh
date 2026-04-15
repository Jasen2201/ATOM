#!/usr/bin/env bash
# =============================================================================
# Script 2: GSM8K Evaluation for DeepSeek-R1 Standalone Server
# Run this AFTER the server (script 1) is up and ready.
# Uses the shared eval_gsm8k.py from evaluation/common/.
# =============================================================================
set -euo pipefail

# ---- Configuration ----
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-8013}"
MODEL="${MODEL:-/mnt/nfs/huggingface/DeepSeek-R1}"
GSM8K_QUESTIONS="${GSM8K_QUESTIONS:-50}"
MAX_TOKENS="${MAX_TOKENS:-2048}"
WORKERS="${WORKERS:-1}"
API_TIMEOUT="${API_TIMEOUT:-600}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
GSM8K_DATASET="${GSM8K_DATASET:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
COMMON_DIR="$(cd "${SCRIPT_DIR}/../../common" && pwd)"
mkdir -p "${LOG_DIR}"

EVAL_SCRIPT="${COMMON_DIR}/eval_gsm8k.py"

echo ""
echo "============================================================"
echo "  GSM8K Evaluation - DeepSeek-R1 Standalone"
echo "============================================================"
echo " Server:     http://${SERVER_HOST}:${SERVER_PORT}"
echo " Model:      ${MODEL}"
echo " Questions:  ${GSM8K_QUESTIONS}"
echo " Max tokens: ${MAX_TOKENS}"
echo " Workers:    ${WORKERS}"
echo "============================================================"

# ---- Wait for server ----
echo "[wait] Checking server at ${SERVER_HOST}:${SERVER_PORT}..."
start=$(date +%s)
while true; do
    if curl -s -o /dev/null -w '%{http_code}' "http://${SERVER_HOST}:${SERVER_PORT}/v1/models" 2>/dev/null | grep -qE '^[2-4]'; then
        elapsed=$(( $(date +%s) - start ))
        echo "[wait] Server is ready (${elapsed}s)."
        break
    fi
    now=$(date +%s)
    if (( now - start >= TIMEOUT_SECONDS )); then
        echo "FATAL: Server not reachable at ${SERVER_HOST}:${SERVER_PORT} after ${TIMEOUT_SECONDS}s"
        exit 1
    fi
    sleep 5
done

# ---- Run evaluation ----
if [[ -f "${EVAL_SCRIPT}" ]]; then
    echo "[eval] Running GSM8K evaluation against DeepSeek-R1..."
    DATASET_ARGS=""
    if [[ -n "${GSM8K_DATASET}" ]]; then
        DATASET_ARGS="--dataset-path ${GSM8K_DATASET}"
    fi
    python3 "${EVAL_SCRIPT}" \
        --host "http://${SERVER_HOST}" \
        --port "${SERVER_PORT}" \
        --model "${MODEL}" \
        --num-questions "${GSM8K_QUESTIONS}" \
        --max-tokens "${MAX_TOKENS}" \
        --workers "${WORKERS}" \
        --timeout "${API_TIMEOUT}" \
        --save-results "${LOG_DIR}/gsm8k_results.json" \
        ${DATASET_ARGS} \
        2>&1 | tee "${LOG_DIR}/gsm8k_eval.log"
else
    echo "FATAL: Evaluation script not found at ${EVAL_SCRIPT}"
    exit 1
fi

echo ""
echo "[done] Results saved to ${LOG_DIR}/gsm8k_results.json"
