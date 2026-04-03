# SGLang + ATOM OOT 1P1D Multi-Node Demo

Multi-node Prefill-Decode disaggregation using SGLang with Mooncake RDMA KV transfer and sgl-model-gateway (smg) as the PD proxy.

Two physical nodes: one runs the prefill server, the other runs the decode server. The smg proxy can run on either node.

## Architecture

```
                    Client (GSM8K / Benchmark)
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
  Node-Prefill              Node-Decode
  +----------------+         +----------------+
  | SGLang Server  |         | SGLang Server  |
  | disagg=prefill |         | disagg=decode  |
  | TP=4/8, GPUs   |         | TP=8, 8 GPUs   |
  | :8010          |         | :8020          |
  +-------+--------+         +--------+-------+
          |                           |
          +------- Mooncake RDMA -----+
           (KV cache transfer via RDMA NICs)
```

## Quick Start

### Step 1: Start Docker Containers (on each node)

SSH into each node, then:

```bash
bash 0_setup_docker.sh
```

This starts a `mesh_dev` container with GPU/RDMA access. Override image/name via `DOCKER_IMAGE` / `CONTAINER_NAME`.

### Step 2: Start Prefill Server (on prefill node)

```bash
docker exec -it mesh_dev bash
PREFILL_HANDSHAKE_IP=<this_node_mgmt_ip> bash 1_start_prefill.sh
```

Wait for: `The server is fired up and ready to roll!`

### Step 3: Start Decode Server (on decode node)

```bash
docker exec -it mesh_dev bash
DECODE_HANDSHAKE_IP=<this_node_mgmt_ip> bash 2_start_decode.sh
```

Wait for: `The server is fired up and ready to roll!`

### Step 4: Start SMG Proxy (on either node)

```bash
docker exec -it mesh_dev bash
PREFILL_MGMT_IP=<prefill_ip> DECODE_MGMT_IP=<decode_ip> bash 3_start_proxy_smg.sh
```

The proxy waits for both servers to be healthy before starting.

### Step 5: Run GSM8K Evaluation

```bash
docker exec -it mesh_dev bash
bash 4_eval_gsm8k.sh
```

Expected result: accuracy >= 90% on 50 questions (typically ~96%).

### Step 6: Run Performance Benchmark

With the prefill, decode, and proxy servers still running:

```bash
docker exec -it mesh_dev bash
bash 5_bench_serving.sh
```

Default config: concurrency=32, input_len=8192, output_len=1024, 320 prompts.

Override via environment variables:

```bash
CONCURRENCY=64 INPUT_LEN=4096 OUTPUT_LEN=512 bash 5_bench_serving.sh
```

## Scripts

| Script | Description | Run On |
|--------|-------------|--------|
| `0_setup_docker.sh` | Start docker container with GPU/RDMA access | Each node (host) |
| `1_start_prefill.sh` | SGLang prefill server (TP=4/8, port 8010) | Prefill node (container) |
| `2_start_decode.sh` | SGLang decode server (TP=8, port 8020) | Decode node (container) |
| `3_start_proxy_smg.sh` | SMG PD proxy (port 8080) | Either node (container) |
| `4_eval_gsm8k.sh` | GSM8K accuracy evaluation (50 questions) | Either node (container) |
| `5_bench_serving.sh` | Performance benchmark via sglang.bench_serving | Either node (container) |

## Configuration

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/it-share/models/deepseek-ai/DeepSeek-R1` | 1, 2, 4, 5 |
| `TP_SIZE` | `4` (prefill) / `8` (decode) | 1, 2 |
| `PREFILL_PORT` | `8010` | 1, 3 |
| `DECODE_PORT` | `8020` | 2, 3 |
| `BOOTSTRAP_PORT` | `8998` | 1, 2, 3 |
| `PROXY_PORT` | `8080` | 3, 4, 5 |
| `PREFILL_HANDSHAKE_IP` | (required) | 1 |
| `DECODE_HANDSHAKE_IP` | (required) | 2 |
| `PREFILL_MGMT_IP` | (required) | 3 |
| `DECODE_MGMT_IP` | (required) | 3 |
| `IB_DEVICE` | `rdma0,...,rdma7` | 1, 2 |
| `CONCURRENCY` | `32` | 5 |
| `INPUT_LEN` | `8192` | 5 |
| `OUTPUT_LEN` | `1024` | 5 |

## Logs

All logs are written to the `logs/` subdirectory:

- `logs/prefill.log`
- `logs/decode.log`
- `logs/proxy_smg.log`
- `logs/gsm8k_eval.log`
- `logs/gsm8k_results.json`
- `logs/bench_serving.log`

## Automated Test Orchestrator

`run_pd_test.sh` runs the full pipeline from a HOST machine via SSH:

```bash
# Default 1P1D TP4+TP8, prefill=g32, decode=g17
bash run_pd_test.sh

# 2P1D
NUM_PREFILLS=2 PREFILL_TP=4 bash run_pd_test.sh

# With benchmark
RUN_BENCH=1 bash run_pd_test.sh
```
