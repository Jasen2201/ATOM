# E2E Test Report — AMD ROCm MI355X

- **Date**: 2026-04-09
- **Platform**: AMD Instinct MI355X × 8 GPUs (288GB each)
- **Image**: `rocm/atom-mesh:latest` (ROCm 7.2, PyTorch 2.9.1, SGLang, ATOM)
- **Branch**: `main`
- **Commits tested**:
  - `b98619e` fix: Response API tool call status and streaming events
  - `2fadfca` fix: adapt e2e tests for AMD ROCm and removed features

## Summary

| Metric | Count |
|--------|-------|
| Total tests | 74 |
| Passed | 71 |
| Skipped | 3 |
| Failed | 0 |
| Not runnable (ATOM plugin crash) | 4 |

## Detailed Results

### router/

| Test File | Tests | Passed | Skipped | Failed |
|-----------|-------|--------|---------|--------|
| `test_worker_api.py` | 6 | 6 | 0 | 0 |
| `test_mmlu.py` | 4 | 4 | 0 | 0 |
| `test_pd_mmlu.py` | 1 | 1 | 0 | 0 |

### chat_completions/

| Test File | Tests | Passed | Skipped | Failed |
|-----------|-------|--------|---------|--------|
| `test_openai_server.py` | 13 | 12 | 1 | 0 |
| `test_validation.py` | 2 | 2 | 0 | 0 |
| `test_reasoning_content.py` | 5 | 5 | 0 | 0 |
| `test_function_calling.py` | 28 | 27 | 1 | 0 |
| `test_enable_thinking.py` | 4 | — | — | — |

> `test_enable_thinking.py`: Not runnable — Qwen3-30B-A3B crashes in ATOM plugin (see Known Issues #1).

### responses/

| Test File | Tests | Passed | Skipped | Failed |
|-----------|-------|--------|---------|--------|
| `test_state_management.py` | 5 | 4 | 1 | 0 |
| `test_streaming_events.py` | 1 | 1 | 0 | 0 |
| `test_structured_output.py` | 1 | 1 | 0 | 0 |
| `test_tools_call.py` | 5 | 5 | 0 | 0 |

### benchmarks/

| Test File | Tests | Passed | Skipped | Failed |
|-----------|-------|--------|---------|--------|
| `test_regular_perf.py` | 2 | 2 | 0 | 0 |
| `test_pd_perf.py` | 1 | 1 | 0 | 0 |

## Benchmark Numbers

| Scenario | TTFT (s) | E2E Latency (s) | Input Throughput (tok/s) | Output Throughput (tok/s) |
|----------|----------|------------------|--------------------------|---------------------------|
| Regular HTTP (4 workers, cache_aware) | 0.392 | 1.070 | 11,203.9 | 149.7 |
| Regular gRPC (4 workers, cache_aware) | 0.390 | 1.064 | 11,224.9 | 149.9 |
| PD 2P+2D (round_robin) | 0.407 | 0.875 | 12,517.7 | 210.7 |

Config: `D(4000,100)`, 32 concurrency, Llama-3.1-8B-Instruct, MI355X.

## Models Used

| Model ID | Model Path | TP | Status |
|----------|------------|-----|--------|
| llama-8b | meta-llama/Llama-3.1-8B-Instruct | 1 | OK |
| qwen-7b | Qwen/Qwen2.5-7B-Instruct | 1 | OK |
| qwen-14b | Qwen/Qwen2.5-14B-Instruct | 2 | OK |
| deepseek-7b | deepseek-ai/DeepSeek-R1-Distill-Qwen-7B | 1 | OK |
| mistral-7b | mistralai/Mistral-7B-Instruct-v0.3 | 1 | OK |
| qwen-30b | Qwen/Qwen3-30B-A3B | 4 | **CRASH** |
| llama-1b | meta-llama/Llama-3.2-1B-Instruct | 1 | N/A (no HF format locally) |

## Bugs Fixed During Testing

### 1. Response API tool call status (`b98619e`)

`finish_reason: "tool_calls"` was mapped to `ResponseStatus::InProgress` instead of `Completed`.
The OpenAI Responses API spec treats completed function call responses as `status: "completed"`.

**Files changed**:
- `mesh/src/routers/grpc/regular/responses/conversions.rs`
- `mesh/src/routers/grpc/regular/responses/streaming.rs`

### 2. Response API streaming tool call events (`b98619e`)

`ResponseStreamEventEmitter::process_chunk` had no handling for `choice.delta.tool_calls`.
No `response.function_call_arguments.delta` events were emitted during streaming.

**File changed**: `mesh/src/routers/grpc/common/responses/streaming.rs`

### 3. IB device detection for AMD (`2fadfca`)

`detect_ib_device()` was hardcoded to only check NVIDIA `mlx5_N` devices.
AMD uses `rdmaN` naming convention.

**File changed**: `mesh/e2e_test/infra/process_utils.py`

### 4. Removed `--history-backend memory` (`2fadfca`)

Feature was deleted (per `FEATURE_ANALYSIS.md`), but e2e test gateway markers still passed it.

**Files changed**: 10 test files across `chat_completions/` and `responses/`.

## Known Issues

### 1. Qwen3-30B-A3B crashes in ATOM plugin

```
RuntimeError: shape '[8556446, 1, 0, 128, 8]' is invalid for input of size 1095225088
```

Location: `atom/plugin/sglang/attention_backend/sgl_attn_backend.py:144` in `reshape_and_cache_shuffle_triton`.
The `0` dimension suggests a KV head count mismatch for this MoE model architecture.
Happens both with and without `--disable-cuda-graph`.

### 2. `sgl-kernel` must be rebuilt from source

The pre-built `sglang-kernel` wheel in `rocm/atom-mesh:latest` causes SIGSEGV (`torch::jit::initJITBindings`).
Fix: `pip uninstall sglang-kernel && cd /app/sglang/sgl-kernel && python setup_rocm.py install`

### 3. `smg-grpc-servicer` import incompatibility

SGLang moved `get_zmq_socket` from `sglang.srt.utils` to `sglang.srt.utils.network`.
Runtime fix: add `from sglang.srt.utils.network import get_zmq_socket` re-export in `sglang/srt/utils/__init__.py`.

### 4. Llama-3.2-1B-Instruct only in Meta format

Local copy at `/it-share/models/meta-llama/Llama-3.2-1B-Instruct/` only contains `original/` directory (Meta format), missing `config.json` for HuggingFace format. Tests requiring `llama-1b` are skipped.

## Container Setup Notes

```bash
# Required before running tests:
pip uninstall sglang-kernel -y
cd /app/sglang/sgl-kernel && python setup_rocm.py install

# Add get_zmq_socket compatibility re-export:
echo "from sglang.srt.utils.network import get_zmq_socket" >> \
  /app/sglang/python/sglang/srt/utils/__init__.py

# Set environment:
export ROUTER_LOCAL_MODEL_PATH=/it-share/models

# Install genai-bench for benchmark tests:
pip install genai-bench
```
