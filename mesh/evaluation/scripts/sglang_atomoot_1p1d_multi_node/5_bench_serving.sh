#!/usr/bin/env bash
# =============================================================================
# Script 5: InferenceMAX-style Benchmark Serving (single config)
#
# Runs benchmark_serving.py against the running disagg cluster (proxy/router).
# Config: concurrency=32, input=8192, output=1024, random dataset, request_rate=inf
#
# Prerequisites:
#   - Prefill/decode servers + proxy are already running (scripts 1-3)
#   - benchmark_serving.py from InferenceMAX_rocm repo
#
# Usage:
#   bash 5_bench_serving.sh
#
# Environment overrides:
#   SERVER_HOST    - proxy host  (default: 127.0.0.1)
#   SERVER_PORT    - proxy port  (default: 8080)
#   MODEL          - model path  (default: /mnt/nfs/huggingface/DeepSeek-R1)
#   CONCURRENCY    - max concurrency (default: 32)
#   INPUT_LEN      - random input length (default: 8192)
#   OUTPUT_LEN     - random output length (default: 1024)
#   NUM_PROMPTS    - total prompts (default: concurrency * 10 = 320)
#   RANDOM_RANGE_RATIO - random range ratio (default: 1.0)
#   INFERMAX_ROOT  - InferenceMAX_rocm repo root
#                    (default: /mnt/nfs/yajizhan/code/InferenceMAX_rocm)
# =============================================================================
set -euo pipefail

# ---- Configuration ----
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-8080}"
MODEL="${MODEL:-/mnt/nfs/huggingface/DeepSeek-R1}"

CONCURRENCY="${CONCURRENCY:-32}"
INPUT_LEN="${INPUT_LEN:-8192}"
OUTPUT_LEN="${OUTPUT_LEN:-1024}"
NUM_PROMPTS="${NUM_PROMPTS:-$((CONCURRENCY * 10))}"
RANDOM_RANGE_RATIO="${RANDOM_RANGE_RATIO:-1.0}"
REQUEST_RATE="${REQUEST_RATE:-inf}"

INFERMAX_ROOT="${INFERMAX_ROOT:-/mnt/nfs/yajizhan/code/InferenceMAX_rocm}"
BENCH_SCRIPT="${INFERMAX_ROOT}/utils/bench_serving/benchmark_serving.py"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

RESULT_DIR="${LOG_DIR}"
RESULT_FILENAME="dsr1_fp8_disagg_isl${INPUT_LEN}_osl${OUTPUT_LEN}_conc${CONCURRENCY}"

# ---- Validate ----
if [[ ! -f "${BENCH_SCRIPT}" ]]; then
    echo "FATAL: benchmark_serving.py not found at ${BENCH_SCRIPT}"
    echo "  Set INFERMAX_ROOT to your InferenceMAX_rocm repo root."
    exit 1
fi

echo ""
echo "============================================================"
echo "  InferenceMAX Benchmark Serving - DeepSeek-R1 Disagg"
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

python3 "${BENCH_SCRIPT}" \
    --model "${MODEL}" \
    --backend openai \
    --base-url "http://${SERVER_HOST}:${SERVER_PORT}" \
    --dataset-name random \
    --random-input-len "${INPUT_LEN}" \
    --random-output-len "${OUTPUT_LEN}" \
    --random-range-ratio "${RANDOM_RANGE_RATIO}" \
    --num-prompts "${NUM_PROMPTS}" \
    --max-concurrency "${CONCURRENCY}" \
    --request-rate "${REQUEST_RATE}" \
    --ignore-eos \
    --save-result \
    --num-warmups "$((2 * CONCURRENCY))" \
    --percentile-metrics 'ttft,tpot,itl,e2el' \
    --result-dir "${RESULT_DIR}" \
    --result-filename "${RESULT_FILENAME}.json" \
    2>&1 | tee "${LOG_DIR}/bench_serving.log"

echo ""
echo "[done] Results saved to ${RESULT_DIR}/${RESULT_FILENAME}.json"
echo "[done] Log saved to ${LOG_DIR}/bench_serving.log"
