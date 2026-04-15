#!/usr/bin/env bash
# =============================================================================
# Script 5: Benchmark Serving (SGLang built-in)
#
# Runs sglang.bench_serving against the running disagg cluster (proxy/router).
# Config: concurrency=32, input=8192, output=1024, random dataset, request_rate=inf
#
# Prerequisites:
#   - Prefill/decode servers + proxy are already running (scripts 1-3)
#
# Run this INSIDE the docker container.
#
# Usage:
#   bash 5_bench_serving.sh
#   CONCURRENCY=64 INPUT_LEN=4096 OUTPUT_LEN=512 bash 5_bench_serving.sh
# =============================================================================
set -euo pipefail

# ---- Configuration ----
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-8080}"
MODEL="${MODEL:-/it-share/models/deepseek-ai/DeepSeek-R1}"

CONCURRENCY="${CONCURRENCY:-32}"
INPUT_LEN="${INPUT_LEN:-8192}"
OUTPUT_LEN="${OUTPUT_LEN:-1024}"
NUM_PROMPTS="${NUM_PROMPTS:-$((CONCURRENCY * 10))}"
RANDOM_RANGE_RATIO="${RANDOM_RANGE_RATIO:-1.0}"
REQUEST_RATE="${REQUEST_RATE:-inf}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

RESULT_DIR="${LOG_DIR}"
RESULT_FILENAME="dsr1_fp8_disagg_isl${INPUT_LEN}_osl${OUTPUT_LEN}_conc${CONCURRENCY}"

echo ""
echo "============================================================"
echo "  Benchmark Serving - DeepSeek-R1 Disagg (sglang.bench_serving)"
echo "============================================================"
echo " Server:             http://${SERVER_HOST}:${SERVER_PORT}"
echo " Model:              ${MODEL}"
echo " Input length:       ${INPUT_LEN}"
echo " Output length:      ${OUTPUT_LEN}"
echo " Concurrency:        ${CONCURRENCY}"
echo " Num prompts:        ${NUM_PROMPTS}"
echo " Request rate:       ${REQUEST_RATE}"
echo " Random range ratio: ${RANDOM_RANGE_RATIO}"
echo " Result file:        ${RESULT_DIR}/${RESULT_FILENAME}.json"
echo "============================================================"

# ---- Wait for server ----
echo "[wait] Checking server at ${SERVER_HOST}:${SERVER_PORT}..."
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"
start=$(date +%s)
while true; do
    if curl -s -o /dev/null -w '%{http_code}' \
        "http://${SERVER_HOST}:${SERVER_PORT}/v1/models" 2>/dev/null | grep -qE '^[2-4]'; then
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

# ---- Run benchmark ----
echo "[bench] Starting benchmark: conc=${CONCURRENCY} isl=${INPUT_LEN} osl=${OUTPUT_LEN}"

python3 -m sglang.bench_serving \
    --model "${MODEL}" \
    --backend sglang \
    --host "${SERVER_HOST}" \
    --port "${SERVER_PORT}" \
    --dataset-name random \
    --random-input-len "${INPUT_LEN}" \
    --random-output-len "${OUTPUT_LEN}" \
    --random-range-ratio "${RANDOM_RANGE_RATIO}" \
    --num-prompts "${NUM_PROMPTS}" \
    --max-concurrency "${CONCURRENCY}" \
    --request-rate "${REQUEST_RATE}" \
    --ignore-eos \
    --save-result \
    --result-filename "${RESULT_DIR}/${RESULT_FILENAME}.json" \
    2>&1 | tee "${LOG_DIR}/bench_serving.log"

echo ""
echo "[done] Results saved to ${RESULT_DIR}/${RESULT_FILENAME}.json"
echo "[done] Log saved to ${LOG_DIR}/bench_serving.log"
