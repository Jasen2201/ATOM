#!/usr/bin/env bash
# =============================================================================
# Script 4: GSM8K Evaluation via PD Proxy - Single Node 1P1D
# =============================================================================
set -euo pipefail

# ---- Configuration ----
PROXY_HOST="${PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${PROXY_PORT:-8080}"
SERVED_MODEL="${SERVED_MODEL:-qwen3-8b}"
GSM8K_QUESTIONS="${GSM8K_QUESTIONS:-50}"
WORKERS="${WORKERS:-4}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-120}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

COMMON_DIR="$(cd "${SCRIPT_DIR}/../../common" && pwd)"
EVAL_STANDALONE="${COMMON_DIR}/eval_gsm8k.py"

echo ""
echo "============================================================"
echo "  GSM8K Evaluation - Single Node 1P1D"
echo "============================================================"
echo " Proxy:     http://${PROXY_HOST}:${PROXY_PORT}"
echo " Model:     ${SERVED_MODEL}"
echo " Questions: ${GSM8K_QUESTIONS}"
echo " Workers:   ${WORKERS}"
echo "============================================================"

# ---- Wait for proxy ----
echo "[wait] Checking proxy at ${PROXY_HOST}:${PROXY_PORT}..."
start=$(date +%s)
while true; do
    if curl -s -o /dev/null -w '%{http_code}' "http://${PROXY_HOST}:${PROXY_PORT}/" 2>/dev/null | grep -qE '^[2-4]'; then
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
if [[ -f "${EVAL_STANDALONE}" ]]; then
    echo "[eval] Running standalone GSM8K evaluator..."
    python3 "${EVAL_STANDALONE}" \
        --host "http://${PROXY_HOST}" \
        --port "${PROXY_PORT}" \
        --model "${SERVED_MODEL}" \
        --num-questions "${GSM8K_QUESTIONS}" \
        --workers "${WORKERS}" \
        --save-results "${LOG_DIR}/gsm8k_results.json" \
        2>&1 | tee "${LOG_DIR}/gsm8k_eval.log"
else
    echo "FATAL: Evaluation script not found at ${EVAL_STANDALONE}"
    exit 1
fi

echo ""
echo "[done] Results saved to ${LOG_DIR}/gsm8k_results.json"
