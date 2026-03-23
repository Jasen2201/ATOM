#!/usr/bin/env bash
# =============================================================================
# Script 4: GSM8K Evaluation via PD Proxy - SGLang 1P1D Single Node
# Run this after the proxy (script 3) is up.
# Uses the shared eval_gsm8k.py from evaluation/common/.
# =============================================================================
set -euo pipefail

# ---- Configuration ----
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-8080}"
MODEL="${MODEL:-/mnt/raid0/RedHatAI/Qwen3-235B-A22B-FP8-dynamic/}"
GSM8K_QUESTIONS="${GSM8K_QUESTIONS:-50}"
WORKERS="${WORKERS:-8}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"
GSM8K_DATASET="${GSM8K_DATASET:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
COMMON_DIR="$(cd "${SCRIPT_DIR}/../../common" && pwd)"
mkdir -p "${LOG_DIR}"

EVAL_SCRIPT="${COMMON_DIR}/eval_gsm8k.py"

echo ""
echo "============================================================"
echo "  GSM8K Evaluation - SGLang 1P1D Single Node"
echo "============================================================"
echo " Server:    http://${PROXY_HOST}:${PROXY_PORT}"
echo " Model:     ${MODEL}"
echo " Questions: ${GSM8K_QUESTIONS}"
echo " Workers:   ${WORKERS}"
echo "============================================================"

# ---- Wait for server ----
echo "[wait] Checking server at ${PROXY_HOST}:${PROXY_PORT}..."
start=$(date +%s)
while true; do
    if curl -s -o /dev/null -w '%{http_code}' "http://${PROXY_HOST}:${PROXY_PORT}/v1/models" 2>/dev/null | grep -qE '^[2-4]'; then
        elapsed=$(( $(date +%s) - start ))
        echo "[wait] Server is ready (${elapsed}s)."
        break
    fi
    now=$(date +%s)
    if (( now - start >= TIMEOUT_SECONDS )); then
        echo "FATAL: Server not reachable at ${PROXY_HOST}:${PROXY_PORT} (${TIMEOUT_SECONDS}s)"
        exit 1
    fi
    sleep 3
done

# ---- Run evaluation ----
if [[ -f "${EVAL_SCRIPT}" ]]; then
    echo "[eval] Running standalone GSM8K evaluator..."
    DATASET_ARGS=""
    if [[ -n "${GSM8K_DATASET}" ]]; then
        DATASET_ARGS="--dataset-path ${GSM8K_DATASET}"
    fi
    python3 "${EVAL_SCRIPT}" \
        --host "http://${PROXY_HOST}" \
        --port "${PROXY_PORT}" \
        --model "${MODEL}" \
        --num-questions "${GSM8K_QUESTIONS}" \
        --workers "${WORKERS}" \
        --save-results "${LOG_DIR}/gsm8k_results.json" \
        ${DATASET_ARGS} \
        2>&1 | tee "${LOG_DIR}/gsm8k_eval.log"
else
    echo "FATAL: Evaluation script not found at ${EVAL_SCRIPT}"
    exit 1
fi

echo ""
echo "[done] Results saved to ${LOG_DIR}/gsm8k_results.json"
