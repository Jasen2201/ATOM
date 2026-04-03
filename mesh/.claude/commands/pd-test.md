# PD Disaggregation 自动化测试

用户输入: $ARGUMENTS

你是一个 PD disaggregation 自动化测试助手。根据用户描述的配置，自动完成以下完整流程：清理旧进程 → 启动 prefill → 启动 decode → 启动 proxy → 运行 GSM8K → 收集结果。

---

## 1. 解析用户配置

从 `$ARGUMENTS` 中提取以下参数（未指定的用默认值）：

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| P数量 | prefill 实例数 | 1 | 1P, 2P |
| D数量 | decode 实例数 | 1 | 1D, 2D |
| Prefill TP | prefill 的 tensor parallel 大小 | 4 | TP4, TP8 |
| Decode TP | decode 的 tensor parallel 大小 | 8 | TP8 |
| NIC 配置 | 8网卡（默认且推荐） | 8网卡 | 8网卡/8NIC |
| Prefill 节点 | prefill 运行的节点 | mia1-p01-g05 | g05 |
| Decode 节点 | decode 运行的节点（多个逗号分隔） | mia1-p01-g07 | g07, g07+g05 |
| 路由策略 | proxy 的路由策略 | random | random, round_robin |
| 额外测试 | 除 GSM8K 外的测试 | 无 | bench, 8k-transfer |

**解析示例**：
- `2P2D TP4+TP8 g05+g07` → 2 prefill(TP4) + 2 decode(TP8), 8×NIC, decode on g05+g07
- `1P1D TP8` → 1 prefill(TP8) + 1 decode(TP8), 8×NIC
- `1P1D TP4+TP8` → 1 prefill(TP4) + 1 decode(TP8), 8×NIC

---

## 2. 节点信息

当前集群节点配置（换机器时更新此表）：

| 节点 | 管理IP (eno1) | 管理IP (eno0) | Docker容器 | GPU | Docker镜像 |
|------|--------------|--------------|-----------|-----|-----------|
| mia1-p01-g05 | 10.28.104.181 | 10.24.112.181 | mesh_dev | 8× MI355X | rocm/atom-mesh:latest |
| mia1-p01-g07 | 10.28.104.183 | 10.24.112.183 | mesh_dev | 8× MI355X | rocm/atom-mesh:latest |

**RDMA 网卡**：每节点 8 个 RDMA NIC（rdma0~rdma7），任意 NIC 间可跨节点 RDMA 通信。

**网络规则**：
- 直接通过逗号分隔传入全部 RDMA 设备：`--disaggregation-ib-device rdma0,rdma1,rdma2,rdma3,rdma4,rdma5,rdma6,rdma7`
- `SGLANG_HOST_IP` 和 Mooncake TCP handshake 使用**管理IP (eno1)**
- 8 NIC 并行聚合带宽实测 ~1.18 Tb/s

---

## 3. 端口和资源分配

| 实例 | Port | Bootstrap Port | GPU IDs | Mooncake Config 文件名 |
|------|------|----------------|---------|----------------------|
| Prefill-1 | 8010 | 8998 | TP8: 0,1,2,3,4,5,6,7 / TP4: 0,1,2,3 | mooncake_prefill.json |
| Prefill-2 | 8011 | 8999 | 4,5,6,7 | mooncake_prefill2.json |
| Decode (任意节点) | 8020 | 8998 | 0,1,2,3,4,5,6,7 | — |
| Proxy | 8080 | — | — | — |

---

## 4. 脚本目录

脚本目录：`evaluation/scripts/sglang_atomoot_1p1d_multi_node/`

脚本需要通过 `docker cp` 复制到容器内执行（无 NFS 共享存储）。`run_pd_test.sh` 会自动完成 sync。

容器内脚本路径：`/workspace/pd_scripts/`
容器内公共脚本路径：`/workspace/common/`

---

## 5. Docker 容器管理

在每个节点上运行 `0_setup_docker.sh` 创建容器：

```bash
bash 0_setup_docker.sh
```

或手动创建：
```bash
docker run -d \
    --name mesh_dev \
    --network host \
    --ipc host \
    --privileged \
    --device /dev/kfd \
    --device /dev/dri \
    -v /mnt:/mnt \
    -v /it-share:/it-share \
    --group-add video \
    --group-add render \
    --cap-add IPC_LOCK \
    --cap-add NET_ADMIN \
    rocm/atom-mesh:latest \
    sleep infinity
```

---

## 6. 执行步骤

### Step 0: 同步脚本到容器

`run_pd_test.sh` 自动通过 `scp` + `docker cp` 将脚本同步到容器内 `/workspace/pd_scripts/`。

### Step 1: 清理所有旧进程

在**所有相关节点**上执行：
```bash
ssh <node_ip> "docker exec <container> bash -c 'pkill -f \"sglang.launch_server\" 2>/dev/null; pkill -f \"smg launch\" 2>/dev/null'"
```
等待 5 秒后确认进程已清理。

### Step 2: 启动 Prefill

**关键约束**：
- **TP=4 必须使用 `--attention-backend triton`**（aiter ASM MLA 仅支持 num_q_heads/num_kv_heads == 16 或 128，TP=4 下 ratio=32 不支持，会 Fatal Abort）
- TP=8 使用默认 aiter backend
- TP=4 时不能用 `1_start_prefill.sh`（脚本硬编码了 `--attention-backend aiter`），需要直接 `docker exec` 运行完整命令

**TP=4 Prefill 启动命令**（直接 docker exec，不走脚本）：
```bash
docker exec -d <container> bash -c '
export HIP_VISIBLE_DEVICES="<gpu_ids>"
export SGLANG_EXTERNAL_MODEL_PACKAGE=atom.plugin.sglang.models
export SGLANG_USE_AITER=1
export SGLANG_AITER_FP8_PREFILL_ATTN=0
export PYTHONFAULTHANDLER=1
export TORCHINDUCTOR_COMPILE_THREADS=128
export AMD_SERIALIZE_KERNEL=1
export SGLANG_HOST_IP="<管理IP>"
export AITER_QUICK_REDUCE_QUANTIZATION=INT4
MOONCAKE_LIB="/opt/venv/lib/python3.12/site-packages/mooncake"
export LD_LIBRARY_PATH="${MOONCAKE_LIB}:/opt/rocm/lib:${LD_LIBRARY_PATH:-}"

SCRIPT_DIR="/workspace/pd_scripts"
export MOONCAKE_CONFIG_PATH="${SCRIPT_DIR}/<mooncake_config_file>"
cat > "${MOONCAKE_CONFIG_PATH}" <<MCEOF
{
    "prefill_url": "<管理IP>:<port>",
    "protocol": "rdma"
}
MCEOF

python3 -m sglang.launch_server \
    --model-path /it-share/models/deepseek-ai/DeepSeek-R1 \
    --host 0.0.0.0 \
    --port <port> \
    --trust-remote-code \
    --tensor-parallel-size 4 \
    --expert-parallel-size 1 \
    --kv-cache-dtype fp8_e4m3 \
    --attention-backend triton \
    --mem-fraction-static 0.8 \
    --page-size 1 \
    --cuda-graph-bs 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 \
    --chunked-prefill-size 16384 \
    --max-running-requests 128 \
    --disable-radix-cache \
    --disaggregation-mode prefill \
    --disaggregation-transfer-backend mooncake \
    --disaggregation-bootstrap-port <bootstrap_port> \
    --disaggregation-ib-device rdma0,rdma1,rdma2,rdma3,rdma4,rdma5,rdma6,rdma7 \
    --enable-metrics \
    --enable-request-time-stats-logging \
    2>&1 | tee ${SCRIPT_DIR}/logs/<log_file>
'
```

**TP=8 Prefill 启动命令**（通过脚本）：
```bash
ssh <node_ip> "docker exec -d <container> bash -c 'PREFILL_HANDSHAKE_IP=<管理IP> MODEL=/it-share/models/deepseek-ai/DeepSeek-R1 bash /workspace/pd_scripts/1_start_prefill.sh > /workspace/pd_scripts/logs/<log_file> 2>&1'"
```

### Step 3: 启动 Decode

通过 `2_start_decode.sh` 启动：
```bash
ssh <decode_node_ip> "docker exec -d <container> bash -c 'DECODE_HANDSHAKE_IP=<decode节点管理IP> MODEL=/it-share/models/deepseek-ai/DeepSeek-R1 bash /workspace/pd_scripts/2_start_decode.sh > /workspace/pd_scripts/logs/<log_file> 2>&1'"
```

### Step 4: 等待所有服务就绪

轮询检查，等待 `/v1/models` 返回 200：
```bash
ssh <node_ip> "docker exec <container> curl -s http://127.0.0.1:<port>/v1/models"
```
超时时间：5 分钟。如果超时，检查日志最后 30 行寻找错误。

### Step 5: 启动 Proxy

使用 `smg launch`，支持多个 `--prefill` 和 `--decode`：
```bash
docker exec -d <container> bash -c '
/usr/local/bin/smg launch \
    --host 0.0.0.0 \
    --port 8080 \
    --pd-disaggregation \
    --prefill "http://<prefill_mgmt_ip>:<port>" "<bootstrap_port>" \
    [--prefill "http://<prefill_mgmt_ip>:<port2>" "<bootstrap_port2>"] \
    --decode "http://<decode_mgmt_ip>:8020" \
    [--decode "http://<decode2_mgmt_ip>:8020"] \
    --policy "<routing_policy>" \
    --backend "sglang" \
    --log-dir "/workspace/pd_scripts/logs" \
    --log-level info \
    --disable-health-check \
    --prometheus-port 29100 \
    2>&1 | tee /workspace/pd_scripts/logs/proxy.log
'
```

### Step 6: 运行 GSM8K 评测

```bash
docker exec <container> bash -c 'PROXY_HOST=127.0.0.1 MODEL=/it-share/models/deepseek-ai/DeepSeek-R1 bash /workspace/pd_scripts/4_eval_gsm8k.sh'
```
- 50 题, 4 workers, max_tokens=2048
- 期望正确率 >= 90%

### Step 7: 收集结果

1. **从 prefill 日志提取 KV 传输速度**：
```bash
grep "Req Time Stats" <prefill_log> | grep -v "HEALTH_CHECK\|input len=4," | grep -oP 'transfer_speed=\S+'
```

2. **统计请求分布**（多 prefill 时）：
```bash
grep "Req Time Stats" <prefill1_log> | grep -v "HEALTH_CHECK\|input len=4," | wc -l
grep "Req Time Stats" <prefill2_log> | grep -v "HEALTH_CHECK\|input len=4," | wc -l
```

3. **输出汇总表格**并追加到 `experiment_results.md`

---

## 7. 可选测试

### 8K Token KV 传输耗时测试

在 GSM8K 之前或之后，发送精确 8192 token 的请求测量 KV 传输延迟：

1. 生成精确 token 数的 prompt（数字序列方式，二分搜索匹配 proxy tokenizer）：
```python
# 在 docker 内执行，二分搜索找到 proxy tokenizer 下精确 8192 token 的 prompt
words = [str(i) for i in range(N)]  # N 约 3064 for DeepSeek-R1
```

2. 通过 proxy 发送请求，`max_tokens=1, temperature=0`

3. 从 prefill 日志提取 `transfer_speed` 和 `transfer_total`

**DeepSeek-R1 FP8 MLA 参考值**：~33.5 KB/token KV cache，8K tokens ≈ 274 MB

### Benchmark Serving 压测

```bash
docker exec <container> bash -c 'SERVER_HOST=127.0.0.1 CONCURRENCY=32 INPUT_LEN=8192 OUTPUT_LEN=1024 bash /workspace/pd_scripts/5_bench_serving.sh'
```

---

## 8. 常见错误和处理

| 错误 | 原因 | 处理 |
|------|------|------|
| `mla_prefill_asm_fwd: only support num_q_heads/num_kv_heads==16 or 128` | TP=4 使用了 aiter backend | 改用 `--attention-backend triton` |
| `RDMA context setup failed: fork compatibility: Invalid argument [22]` | ibverbs fork 警告（非致命） | 忽略，后续会正常初始化 RDMA device |
| `ValueError: Invalid IB devices specified: ['...']` | 设备名不在 `/sys/class/infiniband/` | 检查 `ls /sys/class/infiniband/`，设备名为 `rdma0`~`rdma7` |
| `bootstrap room id` 缺失 | 直接向 prefill 发请求而非通过 proxy | 必须通过 proxy (8080) 发送请求 |
| Mooncake GID mismatch | 节点缺少 RDMA IPv4 地址 | 检查 `ip addr show` 确认 RDMA NIC 有 IPv4 |
| `ibv_devices` 在容器内为空 | 容器未以 `--privileged` 启动 | 重新创建容器，确保 `--privileged` 和 `--network host` |

---

## 9. 输出格式

测试完成后，输出 markdown 表格：

```markdown
| 配置 | Prefill | Decode | NIC | GSM8K正确率 |
|------|---------|--------|-----|------------|
| <config> | <detail> | <detail> | <nic> | <accuracy> |
```

并追加到 `evaluation/scripts/sglang_atomoot_1p1d_multi_node/experiment_results.md`。
