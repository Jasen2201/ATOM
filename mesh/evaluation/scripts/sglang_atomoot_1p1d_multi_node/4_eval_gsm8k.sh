#!/usr/bin/env bash
# =============================================================================
# Script 4: GSM8K Evaluation via PD Proxy - SGLang Multi-Node
# Run this INSIDE the docker container after the proxy (script 3) is up.
# Uses the shared eval_gsm8k.py from evaluation/common/.
# =============================================================================
set -euo pipefail

# ---- Configuration ----
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-8080}"
MODEL="${MODEL:-/it-share/models/deepseek-ai/DeepSeek-R1}"
GSM8K_QUESTIONS="${GSM8K_QUESTIONS:-50}"
MAX_TOKENS="${MAX_TOKENS:-2048}"
WORKERS="${WORKERS:-4}"
API_TIMEOUT="${API_TIMEOUT:-600}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"
GSM8K_DATASET="${GSM8K_DATASET:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

# Find eval_gsm8k.py: check sibling common/ dir first, then relative repo path
if [[ -f "${SCRIPT_DIR}/../common/eval_gsm8k.py" ]]; then
    EVAL_SCRIPT="${SCRIPT_DIR}/../common/eval_gsm8k.py"
elif [[ -f "${SCRIPT_DIR}/../../common/eval_gsm8k.py" ]]; then
    EVAL_SCRIPT="$(cd "${SCRIPT_DIR}/../../common" && pwd)/eval_gsm8k.py"
else
    EVAL_SCRIPT="/workspace/common/eval_gsm8k.py"
fi

echo ""
echo "============================================================"
echo "  GSM8K Evaluation - SGLang PD Multi-Node"
echo "============================================================"
echo " Proxy:     http://${PROXY_HOST}:${PROXY_PORT}"
echo " Model:     ${MODEL}"
echo " Questions: ${GSM8K_QUESTIONS}"
echo " Max tokens: ${MAX_TOKENS}"
echo " Workers:   ${WORKERS}"
echo "============================================================"

# ---- Wait for proxy ----
echo "[wait] Checking proxy at ${PROXY_HOST}:${PROXY_PORT}..."
start=$(date +%s)
while true; do
    if curl -s -o /dev/null -w '%{http_code}' "http://${PROXY_HOST}:${PROXY_PORT}/v1/models" 2>/dev/null | grep -qE '^[2-4]'; then
        elapsed=$(( $(date +%s) - start ))
        echo "[wait] Proxy is ready (${elapsed}s)."
        break
    fi
    now=$(date +%s)
    if (( now - start >= TIMEOUT_SECONDS )); then
        echo "FATAL: Proxy not reachable at ${PROXY_HOST}:${PROXY_PORT} (${TIMEOUT_SECONDS}s)"
        exit 1
    fi
    sleep 3
done

# ---- Run evaluation ----
if [[ -f "${EVAL_SCRIPT}" ]]; then
    echo "[eval] Running GSM8K evaluation against DeepSeek-R1 (PD multi-node)..."
    DATASET_ARGS=""
    if [[ -n "${GSM8K_DATASET}" ]]; then
        DATASET_ARGS="--dataset-path ${GSM8K_DATASET}"
    fi
    python3 "${EVAL_SCRIPT}" \
        --host "http://${PROXY_HOST}" \
        --port "${PROXY_PORT}" \
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
