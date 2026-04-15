#!/usr/bin/env bash
# =============================================================================
# Script 3: Benchmark Serving - Standalone DeepSeek-R1
#
# Runs benchmark_serving.py against the standalone server.
# Supports arbitrary ISL/OSL for isolating prefill vs decode performance.
#
# Examples:
#   ISL=8192 OSL=1 bash 3_bench_serving.sh    # Pure prefill test
#   ISL=1    OSL=1024 bash 3_bench_serving.sh  # Near-pure decode test
#   ISL=8192 OSL=1024 bash 3_bench_serving.sh  # Full pipeline test
# =============================================================================
set -euo pipefail

# ---- Configuration ----
SERVER_HOST="${SERVER_HOST:-127.0.0.1}"
SERVER_PORT="${SERVER_PORT:-8013}"
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
RESULT_FILENAME="${RESULT_FILENAME:-dsr1_fp8_standalone_isl${INPUT_LEN}_osl${OUTPUT_LEN}_conc${CONCURRENCY}.json}"

# ---- Validate ----
if [[ ! -f "${BENCH_SCRIPT}" ]]; then
    echo "FATAL: benchmark_serving.py not found at ${BENCH_SCRIPT}"
    exit 1
fi

echo ""
echo "============================================================"
echo "  Benchmark Serving - DeepSeek-R1 Standalone"
echo "============================================================"
echo " Server:             http://${SERVER_HOST}:${SERVER_PORT}"
echo " Model:              ${MODEL}"
echo " Input length:       ${INPUT_LEN}"
echo " Output length:      ${OUTPUT_LEN}"
echo " Concurrency:        ${CONCURRENCY}"
echo " Num prompts:        ${NUM_PROMPTS}"
echo " Request rate:       ${REQUEST_RATE}"
echo " Result file:        ${RESULT_DIR}/${RESULT_FILENAME}"
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
        echo "FATAL: Server not reachable after ${TIMEOUT_SECONDS}s"
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
    --result-filename "${RESULT_FILENAME}" \
    2>&1 | tee "${LOG_DIR}/bench_serving.log"

echo ""
echo "[done] Results saved to ${RESULT_DIR}/${RESULT_FILENAME}"
