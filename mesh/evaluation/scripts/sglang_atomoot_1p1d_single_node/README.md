# SGLang + ATOM OOT 1P1D Single-Node Demo

Single-node Prefill-Decode disaggregation demo using SGLang (with Mooncake KV transfer) and sgl-model-gateway (smg) as the PD proxy.

## Architecture

```
Client (GSM8K eval)
       |
       v
+--------------+
|  SMG Proxy   |  :8080  (PD routing)
|  (Script 3)  |
+------+-------+
       |
  +----+----+
  v         v
+------+  +------+
|Prefill|  |Decode |
|GPU0-3|  |GPU4-7|
|:8010 |  |:8020  |
+------+  +------+
  disagg-mode=prefill --> mooncake --> disagg-mode=decode
```

## Prerequisites

- **Docker image**: `rocm/atom-mesh:latest` (built via `docker/build_mesh.sh`)
- **GPUs**: 8x AMD MI-series GPUs (4 for prefill, 4 for decode)
- **Model**: Qwen3-235B-A22B-FP8-dynamic (default: `/mnt/raid0/RedHatAI/Qwen3-235B-A22B-FP8-dynamic/`)
- **smg binary**: Pre-installed at `/usr/local/bin/smg` in the container

## Usage

Open **4 separate terminals** inside the docker container and run:

```bash
# Terminal 1: Start prefill server (GPU 0-3, TP=4, EP=4)
bash 1_start_prefill.sh

# Terminal 2: Start decode server (GPU 4-7, TP=4, EP=4)
bash 2_start_decode.sh

# Terminal 3: Start SMG PD proxy (waits for both servers)
bash 3_start_proxy_smg.sh

# Terminal 4: Run GSM8K evaluation
bash 4_eval_gsm8k.sh
```

### Standalone baseline (no PD)

```bash
# Single GPU, no disaggregation
bash 5_start_standalone.sh
```

## Scripts

| Script | Description | Default Port |
|--------|-------------|:---:|
| `1_start_prefill.sh` | SGLang prefill server (disagg-mode=prefill, GPU 0-3) | 8010 |
| `2_start_decode.sh` | SGLang decode server (disagg-mode=decode, GPU 4-7) | 8020 |
| `3_start_proxy_smg.sh` | SMG PD proxy, routes prefill->decode | 8080 |
| `4_eval_gsm8k.sh` | GSM8K 5-shot evaluation (50 questions) | -- |
| `5_start_standalone.sh` | Standalone SGLang baseline (no PD) | 8013 |

## Configuration

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/mnt/raid0/RedHatAI/Qwen3-235B-A22B-FP8-dynamic/` | 1, 2, 4, 5 |
| `TP_SIZE` | `4` | 1, 2, 5 |
| `EP_SIZE` | `4` | 1, 2 |
| `GPU_IDS` | `0,1,2,3` (prefill), `4,5,6,7` (decode) | 1, 2, 5 |
| `PREFILL_PORT` | `8010` | 1, 3 |
| `DECODE_PORT` | `8020` | 2, 3 |
| `BOOTSTRAP_PORT` | `8998` | 1, 2, 3 |
| `PROXY_PORT` | `8080` | 3, 4 |
| `TRANSFER_BACKEND` | `mooncake` | 1, 2 |
| `MOONCAKE_PROTOCOL` | `local` | 1, 2 |
| `BACKEND` | `sglang` | 3 |
| `GSM8K_QUESTIONS` | `50` | 4 |
| `WORKERS` | `8` | 4 |
| `GSM8K_DATASET` | _(empty, downloads from HF)_ | 4 |
| `PROMETHEUS_PORT` | `29100` | 3 |

## Logs

All logs are written to the `logs/` subdirectory:
- `logs/prefill.log`
- `logs/decode.log`
- `logs/proxy.log`
- `logs/gsm8k_eval.log`
- `logs/gsm8k_results.json`
- `logs/server.log` (standalone)
