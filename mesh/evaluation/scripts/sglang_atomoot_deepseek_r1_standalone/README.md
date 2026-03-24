# SGLang + ATOM OOT DeepSeek-R1 Standalone Demo

Standalone (no PD disaggregation) demo for DeepSeek-R1 671B using SGLang with ATOM OOT plugin on a single 8-GPU node.

## Prerequisites

- **Docker image**: `rocm/atom-mesh:latest` (built via `docker/build_mesh.sh`)
- **GPUs**: 8x AMD MI-series GPUs
- **Model**: DeepSeek-R1 (default: `/mnt/nfs/huggingface/DeepSeek-R1`)

## Usage

Open **2 terminals** inside the docker container:

```bash
# Terminal 1: Start the server (TP=8, EP=8)
bash 1_start_server.sh

# Terminal 2: Run GSM8K evaluation (after server is ready)
bash 2_eval_gsm8k.sh
```

## Scripts

| Script | Description | Default Port |
|--------|-------------|:---:|
| `1_start_server.sh` | Standalone SGLang server (TP=8, EP=8) | 8013 |
| `2_eval_gsm8k.sh` | GSM8K 5-shot evaluation (50 questions) | -- |

## Configuration

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/mnt/nfs/huggingface/DeepSeek-R1` | 1, 2 |
| `TP_SIZE` | `8` | 1 |
| `EP_SIZE` | `8` | 1 |
| `SERVER_PORT` | `8013` | 1, 2 |
| `SERVER_HOST` | `localhost` | 1, 2 |
| `KV_CACHE_DTYPE` | `fp8_e4m3` | 1 |
| `MEM_FRACTION` | `0.8` | 1 |
| `PAGE_SIZE` | `1` | 1 |
| `CUDA_GRAPH_MAX_BS` | `16` | 1 |
| `QUICK_REDUCE_QUANT` | `INT4` | 1 |
| `GSM8K_QUESTIONS` | `50` | 2 |
| `MAX_TOKENS` | `2048` | 2 |
| `WORKERS` | `8` | 2 |
| `GSM8K_DATASET` | _(empty, downloads from HF)_ | 2 |

## Logs

All logs are written to the `logs/` subdirectory:
- `logs/server.log`
- `logs/gsm8k_eval.log`
- `logs/gsm8k_results.json`
