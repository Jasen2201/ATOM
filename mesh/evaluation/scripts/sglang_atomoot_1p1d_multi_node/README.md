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
  Node09 (Prefill)              Node07 (Decode)
  +----------------+         +----------------+
  | SGLang Server  |         | SGLang Server  |
  | disagg=prefill |         | disagg=decode  |
  | TP=8, 8 GPUs   |         | TP=8, 8 GPUs   |
  | :8010          |         | :8020          |
  +-------+--------+         +--------+-------+
          |                           |
          +------- Mooncake RDMA -----+
           (KV cache transfer via ionic)
```

## Node Configuration

| Role | Node | Management IP | RDMA IPs (ionic_0 - ionic_7) |
|------|------|---------------|------------------------------|
| Prefill | node09 | 10.2.224.4 | 192.168.100.9 - 192.168.107.9 |
| Decode | node07 | 10.2.224.6 | 192.168.100.7 - 192.168.107.7 |

> **Network notes:**
> - Management IPs are used for `SGLANG_HOST_IP` (Mooncake TCP handshake) and HTTP API.
> - RDMA IPs are used by the Mooncake TransferEngine for RoCEv2 data transfer via `--disaggregation-ib-device`.
> - Pensando ionic NICs only support RDMA verbs, NOT TCP. Never use RDMA IPs for `SGLANG_HOST_IP`.

## Step 1: Build Docker Image

Build the `rocm/atom-mesh:latest` image on **both** nodes. The NFS-shared script ensures identical builds.

```bash
# On node09 (local)
bash /mnt/nfs/yajizhan/code/MESH/docker/build_mesh.sh

# On node07
ssh node07 bash /mnt/nfs/yajizhan/code/MESH/docker/build_mesh.sh
```

See `docker/build_mesh.sh` for build options (ATOM branch, Mooncake commit, SGLang branch, etc.).

## Step 2: Start Containers and Configure RDMA Network

### 2.1 Start containers on both nodes

```bash
# On node09
docker run -d --cap-add=SYS_PTRACE \
  --cap-add=IPC_LOCK --cap-add=NET_RAW \
  --network=host --security-opt seccomp=unconfined \
  --name zyj_dev_mesh \
  --device=/dev/kfd --device=/dev/dri --device=/dev/infiniband \
  --shm-size 60g -v /mnt:/mnt \
  --group-add video --ipc=host \
  rocm/atom-mesh:latest \
  /bin/bash -c "while true; do sleep 3600; done"

# On node07
ssh node07 'docker run -d --cap-add=SYS_PTRACE \
  --cap-add=IPC_LOCK --cap-add=NET_RAW \
  --network=host --security-opt seccomp=unconfined \
  --name zyj_dev_mesh \
  --device=/dev/kfd --device=/dev/dri --device=/dev/infiniband \
  --shm-size 60g -v /mnt:/mnt \
  --group-add video --ipc=host \
  rocm/atom-mesh:latest \
  /bin/bash -c "while true; do sleep 3600; done"'
```

### 2.2 Configure RDMA IPv4 addresses

Each ionic RDMA NIC **must** have an IPv4 address so that Mooncake selects the correct RoCEv2 GID (GID index 2, IPv4-mapped). Without IPv4 addresses, Mooncake falls back to link-local fe80 GIDs which are not routable cross-node.

Verify whether IPv4 addresses are already configured:

```bash
ip addr show | grep -E "inet .*(enp9s0|enp25s0|enp105s0|enp121s0|enp137s0|enp153s0|enp233s0|enp249s0)"
```

If missing, configure them (run on the **host**, not inside the container):

```bash
# Node09 (suffix .9)
ip addr add 192.168.100.9/24 dev enp9s0    # ionic_0
ip addr add 192.168.101.9/24 dev enp25s0   # ionic_1
ip addr add 192.168.102.9/24 dev enp105s0  # ionic_2
ip addr add 192.168.103.9/24 dev enp121s0  # ionic_3
ip addr add 192.168.104.9/24 dev enp137s0  # ionic_4
ip addr add 192.168.105.9/24 dev enp153s0  # ionic_5
ip addr add 192.168.106.9/24 dev enp233s0  # ionic_6
ip addr add 192.168.107.9/24 dev enp249s0  # ionic_7

# Node07 (suffix .7)
ssh node07 'ip addr add 192.168.100.7/24 dev enp9s0 && \
  ip addr add 192.168.101.7/24 dev enp25s0 && \
  ip addr add 192.168.102.7/24 dev enp105s0 && \
  ip addr add 192.168.103.7/24 dev enp121s0 && \
  ip addr add 192.168.104.7/24 dev enp137s0 && \
  ip addr add 192.168.105.7/24 dev enp153s0 && \
  ip addr add 192.168.106.7/24 dev enp233s0 && \
  ip addr add 192.168.107.7/24 dev enp249s0'
```

> **Warning:** These addresses are ephemeral and lost on reboot. Add them to `/etc/netplan/` for persistence.

### 2.3 Apply SGLang IB device map patch

**Why this patch is required:**

Each of the 8 ionic NICs lives on a separate `/24` subnet (ionic_0 on 192.168.100.x, ionic_1 on 192.168.101.x, ..., ionic_7 on 192.168.107.x). RDMA transfers only work between NICs on the **same** subnet — e.g. prefill ionic_0 (192.168.100.9) can only reach decode ionic_0 (192.168.100.7).

Upstream SGLang's `--disaggregation-ib-device` only accepts a comma-separated device list (e.g. `ionic_0,ionic_1,...,ionic_7`), which is passed to all TP ranks. Mooncake then freely picks any NIC for each transfer, causing cross-subnet RDMA failures (`transport retry counter exceeded`).

The patch (`patch_sglang_ib.py`) extends `_validate_ib_devices` to also accept a **JSON file path**, so each TP rank is bound to exactly one ionic device via `ib_device_map.json`:

```json
{"0": "ionic_0", "1": "ionic_1", "2": "ionic_2", "3": "ionic_3",
 "4": "ionic_4", "5": "ionic_5", "6": "ionic_6", "7": "ionic_7"}
```

This ensures TP rank N on the prefill node always transfers to TP rank N on the decode node through the same-subnet ionic_N pair.

**Apply the patch** inside containers on **both** nodes:

```bash
docker exec zyj_dev_mesh python3 \
  /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/patch_sglang_ib.py

ssh node07 'docker exec zyj_dev_mesh python3 \
  /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/patch_sglang_ib.py'
```

## Step 3: Accuracy Evaluation (GSM8K)

### 3.1 Start prefill server (node09)

```bash
docker exec -it zyj_dev_mesh bash
bash /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/1_start_prefill.sh
```

Wait for: `The server is fired up and ready to roll!`

### 3.2 Start decode server (node07)

```bash
ssh node07
docker exec -it zyj_dev_mesh bash
bash /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/2_start_decode.sh
```

Wait for: `The server is fired up and ready to roll!`

### 3.3 Start SMG proxy (either node)

```bash
docker exec -it zyj_dev_mesh bash
bash /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/3_start_proxy_smg.sh
```

The proxy waits for both servers to be healthy before starting.

### 3.4 Run GSM8K evaluation

```bash
docker exec -it zyj_dev_mesh bash
bash /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/4_eval_gsm8k.sh
```

Expected result: accuracy >= 90% on 50 questions (typically ~96%).

## Step 4: Performance Benchmark

With the prefill, decode, and proxy servers still running from Step 3:

```bash
docker exec -it zyj_dev_mesh bash
bash /mnt/nfs/yajizhan/code/MESH/evaluation/scripts/sglang_atomoot_1p1d_multi_node/5_bench_serving.sh
```

Default config: concurrency=32, input_len=8192, output_len=1024, 320 prompts.

Override via environment variables:

```bash
CONCURRENCY=64 INPUT_LEN=4096 OUTPUT_LEN=512 bash 5_bench_serving.sh
```

## Scripts

| Script | Description | Run On |
|--------|-------------|--------|
| `1_start_prefill.sh` | SGLang prefill server (TP=8, port 8010) | Node09 |
| `2_start_decode.sh` | SGLang decode server (TP=8, port 8020) | Node07 |
| `3_start_proxy_smg.sh` | SMG PD proxy (port 8080) | Either |
| `4_eval_gsm8k.sh` | GSM8K accuracy evaluation (50 questions) | Either |
| `5_bench_serving.sh` | Performance benchmark via InferenceMAX | Either |
| `patch_sglang_ib.py` | Patch SGLang to accept JSON IB device map | Both |
| `ib_device_map.json` | TP rank -> ionic device mapping | Both |

## Configuration

| Variable | Default | Used By |
|----------|---------|---------|
| `MODEL` | `/mnt/nfs/huggingface/DeepSeek-R1` | 1, 2, 4, 5 |
| `TP_SIZE` | `8` | 1, 2 |
| `EP_SIZE` | `1` | 1, 2 |
| `PREFILL_PORT` | `8010` | 1, 3 |
| `DECODE_PORT` | `8020` | 2, 3 |
| `BOOTSTRAP_PORT` | `8998` | 1, 2, 3 |
| `PROXY_PORT` | `8080` | 3, 4, 5 |
| `PREFILL_HANDSHAKE_IP` | `10.2.224.4` | 1 |
| `DECODE_HANDSHAKE_IP` | `10.2.224.6` | 2 |
| `PREFILL_MGMT_IP` | `10.2.224.4` | 3 |
| `DECODE_MGMT_IP` | `10.2.224.6` | 3 |
| `MOONCAKE_PROTOCOL` | `rdma` | 1, 2 |
| `TRANSFER_BACKEND` | `mooncake` | 1, 2 |
| `GSM8K_QUESTIONS` | `50` | 4 |
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

## Troubleshooting

### "remote mooncake session is not alive"

Mooncake RDMA transfer fails because ionic NICs are missing IPv4 addresses. Verify GID index 2 exists:

```bash
cat /sys/class/infiniband/ionic_0/ports/1/gids/2
# Should show: 0000:0000:0000:0000:0000:ffff:c0a8:XXYY
# If all zeros or missing, configure IPv4 addresses (see Step 2.2)
```

### "transport retry counter exceeded" / cross-subnet RDMA failure

Mooncake is routing RDMA transfers across different ionic subnets (e.g. ionic_1 on 192.168.101.x trying to reach ionic_7 on 192.168.107.x). This happens when `--disaggregation-ib-device` is set to a comma-separated list instead of a JSON file. Apply `patch_sglang_ib.py` and use `ib_device_map.json` (see Step 2.3).

### "RDMA context setup failed: fork compatibility"

This is a benign warning (`ibv_fork_init` not supported by ionic driver). Mooncake falls back to SIEVE endpoint store and works correctly.
