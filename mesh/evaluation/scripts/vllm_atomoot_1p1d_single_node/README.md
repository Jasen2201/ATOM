# vLLM + Mooncake 1P1D Single-Node Demo

Single-node Prefill-Decode disaggregation demo using vLLM (with Mooncake KV transfer) and sgl-model-gateway (smg) as the PD proxy.

## Architecture

```
Client (GSM8K eval)
       │
       ▼
┌──────────────┐
│  SMG Proxy   │  :8080  (PD routing)
│  (Script 3)  │
└──────┬───────┘
       │
  ┌────┴────┐
  ▼         ▼
┌──────┐  ┌──────┐
│Prefill│  │Decode│
│GPU 0 │  │GPU 1 │
│:8010 │  │:8020 │
└──────┘  └──────┘
  kv_producer ──Mooncake──▶ kv_consumer
```

## Prerequisites

- **GPU**: 2x AMD MI-series GPUs (or NVIDIA GPUs, adjust `HIP_VISIBLE_DEVICES` → `CUDA_VISIBLE_DEVICES`)
- **vLLM**: `pip install vllm` (tested with v0.1.dev)
- **Mooncake**: KV transfer library installed (`/opt/venv/lib/python3.12/site-packages/mooncake`)
- **Model**: Qwen3-8B-FP8-dynamic (default: `/mnt/raid0/RedHatAI/Qwen3-8B-FP8-dynamic`)
- **smg binary**: Built from the mesh project root

### Build smg

```bash
# From project root (/home/yajizhan/code/mesh)
cargo build --release

# Verify
./target/release/smg --version
```

The scripts auto-detect the binary at `<project_root>/target/release/smg`. Override with `SMG_BIN` env var if needed.

## Usage

Open **4 separate terminals** and run the scripts in order:

```bash
# Terminal 1: Start prefill server (GPU 0)
bash 1_start_prefill.sh

# Terminal 2: Start decode server (GPU 1)
bash 2_start_decode.sh

# Terminal 3: Start SMG PD proxy (waits for both servers)
bash 3_start_proxy_smg.sh

# Terminal 4: Run GSM8K evaluation
bash 4_eval_gsm8k.sh
```

Scripts 3 and 4 will automatically wait for upstream services to be ready before proceeding.

## Scripts

| Script | Description | Default Port |
|--------|-------------|:---:|
| `1_start_prefill.sh` | vLLM prefill server (kv_producer, GPU 0) | 8010 |
| `2_start_decode.sh` | vLLM decode server (kv_consumer, GPU 1) | 8020 |
| `3_start_proxy_smg.sh` | SMG PD proxy, routes prefill→decode | 8080 |
| `4_eval_gsm8k.sh` | GSM8K 5-shot evaluation (50 questions) | — |
| `eval_gsm8k_standalone.py` | Standalone evaluator (no vLLM source dependency) | — |

## Configuration

All scripts support environment variable overrides:

```bash
# Example: use a different model and ports
MODEL=/path/to/model \
SERVED_MODEL=my-model \
PREFILL_PORT=9010 \
DECODE_PORT=9020 \
PROXY_PORT=9080 \
GPU_IDS=2 \
bash 1_start_prefill.sh
```

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/mnt/raid0/RedHatAI/Qwen3-8B-FP8-dynamic` | 1, 2 |
| `SERVED_MODEL` | `qwen3-8b` | 1, 2, 4 |
| `TP_SIZE` | `1` | 1, 2 |
| `GPU_IDS` | `0` (prefill), `1` (decode) | 1, 2 |
| `PREFILL_PORT` | `8010` | 1, 3 |
| `DECODE_PORT` | `8020` | 2, 3 |
| `BOOTSTRAP_PORT` | `8998` | 1, 3 |
| `PROXY_PORT` | `8080` | 3, 4 |
| `POLICY` | `round_robin` | 3 |
| `SMG_BIN` | `<project_root>/target/release/smg` | 3 |
| `GPU_MEM_UTIL` | `0.9` | 1, 2 |
| `MAX_MODEL_LEN` | `4096` | 1, 2 |
| `MOONCAKE_PROTOCOL` | `local` | 1, 2 |
| `GSM8K_QUESTIONS` | `50` | 4 |
| `WORKERS` | `4` (concurrent requests) | 4 |

## Expected Output

```
============================================================
Results
============================================================
  Accuracy:       0.8400 (84.0%)
  Correct:        42/50
  Invalid:        0/50
  Total time:     11.6s
  ~Throughput:    352 tokens/s
============================================================
PASS - Accuracy is in expected range
```

Results are saved to `logs/gsm8k_results.json`.

## Logs

All logs are written to the `logs/` subdirectory:
- `logs/prefill.log`
- `logs/decode.log`
- `logs/proxy.log`
- `logs/gsm8k_eval.log`
- `logs/gsm8k_results.json`
