# vLLM + Mooncake 1P1D Multi-Node Demo

Multi-node Prefill-Decode disaggregation demo using vLLM (with Mooncake RDMA KV transfer) and sgl-model-gateway (smg) as the PD proxy.

Two physical nodes: one runs the prefill server (kv_producer), the other runs the decode server (kv_consumer). The smg proxy can run on either node.

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
            +-------------+-------------+
            |                           |
            v                           v
  Node A (g38)                Node B (g52)
  +----------------+         +----------------+
  | Prefill Server |         | Decode Server  |
  | kv_producer    |         | kv_consumer    |
  | TP=8, 8 GPUs   |         | TP=8, 8 GPUs   |
  | :8010          |         | :8020          |
  +-------+--------+         +--------+-------+
          |                           |
          +------- Mooncake RDMA -----+
              (KV cache transfer)
```

## Prerequisites

- **Nodes**: 2x nodes with AMD MI-series GPUs (8 GPUs each)
- **Docker image**: `rocm/atom-vllm-dev` (built via `build_OOT_vLLM.sh`)
- **vLLM**: Installed inside the container with Mooncake support
- **Mooncake**: KV transfer library with RDMA support
- **Model**: DeepSeek-R1-FP8-Dynamic (default: `/mnt/raid0/deepseek-r1-FP8-Dynamic`)
- **smg binary**: Pre-installed at `/usr/local/bin/smg` in the container
- **Network**: RDMA-capable network between nodes

### Default Node Configuration

| Role | Node | Mgmt IP | RDMA IP |
|------|------|---------|---------|
| Prefill | g38 | 10.36.41.138 | 10.103.38.101 |
| Decode | g52 | 10.36.40.122 | 10.103.52.101 |

## Usage

Run scripts **inside the docker container** on the respective nodes:

```bash
# Node A (g38) - Terminal 1: Start prefill server
bash 1_start_prefill.sh

# Node B (g52) - Terminal 2: Start decode server
bash 2_start_decode.sh

# Either node - Terminal 3: Start SMG PD proxy (waits for both servers)
bash 3_start_proxy_smg.sh

# Either node - Terminal 4: Run GSM8K evaluation
bash 4_eval_gsm8k.sh
```

Scripts 3 and 4 automatically wait for upstream services to be ready before proceeding.

### Standalone baseline (no PD)

```bash
# Single node, TP=8, no disaggregation
bash 5_start_standalone.sh
```

## Scripts

| Script | Description | Run On | Default Port |
|--------|-------------|--------|:---:|
| `1_start_prefill.sh` | vLLM prefill server (kv_producer, TP=8) | Node A (g38) | 8010 |
| `2_start_decode.sh` | vLLM decode server (kv_consumer, TP=8) | Node B (g52) | 8020 |
| `3_start_proxy_smg.sh` | SMG PD proxy, routes prefill -> decode | Either node | 8080 |
| `4_eval_gsm8k.sh` | GSM8K 5-shot evaluation (50 questions) | Either node | -- |
| `5_start_standalone.sh` | Standalone vLLM baseline (no PD) | Either node | 8000 |

The GSM8K evaluator (`eval_gsm8k.py`) is shared across all evaluation configs and lives in `evaluation/common/`.

## Configuration

All scripts support environment variable overrides:

```bash
# Example: use a different model and ports
MODEL=/path/to/model \
SERVED_MODEL=my-model \
PREFILL_PORT=9010 \
DECODE_PORT=9020 \
PROXY_PORT=9080 \
bash 3_start_proxy_smg.sh
```

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/mnt/raid0/deepseek-r1-FP8-Dynamic` | 1, 2, 5 |
| `SERVED_MODEL` | `deepseek-r1` | 1, 2, 4, 5 |
| `TP_SIZE` | `8` | 1, 2, 5 |
| `GPU_IDS` | `0,1,2,3,4,5,6,7` | 1, 2, 5 |
| `PREFILL_PORT` | `8010` | 1, 3 |
| `DECODE_PORT` | `8020` | 2, 3 |
| `BOOTSTRAP_PORT` | `8998` | 1, 3 |
| `PROXY_PORT` | `8080` | 3, 4 |
| `PREFILL_MGMT_IP` | `10.36.41.138` | 3 |
| `DECODE_MGMT_IP` | `10.36.40.122` | 3 |
| `PREFILL_RDMA_IP` | `10.103.38.101` | 1 |
| `DECODE_RDMA_IP` | `10.103.52.101` | 2 |
| `MOONCAKE_PROTOCOL` | `rdma` | 1, 2 |
| `POLICY` | `round_robin` | 3 |
| `BACKEND` | `vllm` | 3 |
| `SMG_BIN` | `/usr/local/bin/smg` | 3 |
| `GPU_MEM_UTIL` | `0.9` | 1, 2, 5 |
| `MAX_MODEL_LEN` | `4096` (PD) / `16384` (standalone) | 1, 2, 5 |
| `GSM8K_QUESTIONS` | `50` | 4 |
| `WORKERS` | `4` (concurrent requests) | 4 |
| `PROXY_HOST` | `10.36.41.138` | 4 |

## Expected Output

```
============================================================
Results
============================================================
  Accuracy:       0.9600 (96.0%)
  Correct:        48/50
  Invalid:        0/50
  Total time:     27.1s
  ~Throughput:    131 tokens/s
============================================================
PASS - Accuracy is in expected range
```

Results are saved to `logs/gsm8k_results.json`.

## Logs

All logs are written to the `logs/` subdirectory:
- `logs/prefill.log`
- `logs/decode.log`
- `logs/proxy_smg.log`
- `logs/gsm8k_eval.log`
- `logs/gsm8k_results.json`
- `logs/standalone.log`

## Directory Structure

```
evaluation/
  common/
    eval_gsm8k.py                # Shared evaluator (used by all configs)
  scripts/
    vllm_atomoot_1p1d_single_node/   # Single-node 1P1D (Qwen3-8B, 2 GPUs)
    vllm_atomoot_1p1d_multi_node/    # Multi-node 1P1D (DeepSeek-R1, 2x8 GPUs)
```
