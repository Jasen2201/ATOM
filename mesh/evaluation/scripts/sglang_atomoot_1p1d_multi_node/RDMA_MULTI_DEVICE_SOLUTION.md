# Mooncake 多 Ionic 设备 RDMA 跨节点通信方案

## 1. 问题描述

在 node09 / node07 之间使用 Mooncake TransferEngine 做 PD 分离（1P1D）时，传入多个 ionic 设备会导致 RDMA 连接失败：

```
local_nic: ionic_2, peer_nic: 10.2.224.6:16796@ionic_4: transport retry counter exceeded
```

当前脚本默认只使用单设备 `ionic_0`，虽然能工作但浪费了 7/8 的 RDMA 带宽。

## 2. 根因分析

### 2.1 网络拓扑

每个节点有 8 个 ionic NIC，每个 NIC 在独立的 /24 子网上，**仅同编号设备之间有 L2 物理连通性**：

```
node09                              node07
ionic_0  192.168.100.9/24  ←────→  192.168.100.7/24  ionic_0   (物理直连)
ionic_1  192.168.101.9/24  ←────→  192.168.101.7/24  ionic_1   (物理直连)
ionic_2  192.168.102.9/24  ←────→  192.168.102.7/24  ionic_2   (物理直连)
ionic_3  192.168.103.9/24  ←────→  192.168.103.7/24  ionic_3   (物理直连)
ionic_4  192.168.104.9/24  ←────→  192.168.104.7/24  ionic_4   (物理直连)
ionic_5  192.168.105.9/24  ←────→  192.168.105.7/24  ionic_5   (物理直连)
ionic_6  192.168.106.9/24  ←────→  192.168.106.7/24  ionic_6   (物理直连)
ionic_7  192.168.107.9/24  ←────→  192.168.107.7/24  ionic_7   (物理直连)

ionic_2@node09 ←──✗──→ ionic_4@node07   (跨子网，物理不通)
```

### 2.2 PCIe 亲和关系

每个 GPU 与同编号的 ionic NIC 在 PCIe 拓扑上紧邻，形成 1:1 亲和：

```
NUMA 0                                     NUMA 1
GPU0 (0000:05:00) ↔ ionic_0 (0000:09:00)   GPU4 (0000:85:00) ↔ ionic_4 (0000:89:00)
GPU1 (0000:15:00) ↔ ionic_1 (0000:19:00)   GPU5 (0000:95:00) ↔ ionic_5 (0000:99:00)
GPU2 (0000:54:00) ↔ ionic_2 (0000:69:00)   GPU6 (0000:e5:00) ↔ ionic_6 (0000:e9:00)
GPU3 (0000:65:00) ↔ ionic_3 (0000:79:00)   GPU7 (0000:f5:00) ↔ ionic_7 (0000:f9:00)
```

只用 `ionic_0` 时，GPU4-7 的数据需要跨 NUMA 传输，额外延迟 + 带宽瓶颈。

### 2.3 Mooncake 的设备选择机制

通过阅读 Mooncake C++ 源码（`topology.cpp`, `rdma_transport.cpp`, `worker_pool.cpp`），确认其设备选择逻辑为：

1. **初始化阶段**：`Topology::discover(filter)` 根据传入的设备名过滤可用 NIC，并通过 `discoverCudaTopology()` 按 PCIe 距离计算每个 GPU 的 `preferred_hca` 和 `avail_hca`
2. **传输阶段**：`selectDevice()` 根据 buffer 所在的 GPU 选出 preferred NIC（本地和远端独立决策）
3. **建连阶段**：`worker_pool.cpp` 中用 `MakeNicPath(peer_name, peer_devices[device_id].name)` 构建对端 NIC 路径，建立 QP 连接

**关键点**：本地和远端的 NIC 选择是独立的。如果传入全部 8 个 ionic 设备，Mooncake 可能选出 `ionic_2@node09 → ionic_4@node07` 这样的跨子网 QP，导致连接失败。

### 2.4 SGLang 的 per-GPU 设备映射能力

SGLang 的 `get_ib_devices_for_gpu()` 函数（`mooncake_transfer_engine.py:15`）已经支持 3 种格式：

| 格式 | 示例 | 行为 |
|---|---|---|
| 逗号分隔字符串 | `"ionic_0,ionic_1,...,ionic_7"` | 所有 GPU 使用相同的设备列表 |
| JSON dict 字符串 | `'{"0":"ionic_0","1":"ionic_1",...}'` | 每个 GPU 使用独立的设备列表 |
| JSON 文件路径 | `"/path/to/ib_map.json"` | 从文件读取 JSON dict |

每个 TP worker 进程调用 `init_mooncake_transfer_engine(hostname, gpu_id, ib_device)` 时，会通过 `get_ib_devices_for_gpu(ib_device, gpu_id)` 取出该 GPU 专属的设备列表，传给 `TransferEngine.initialize()`。

## 3. 解决方案

### 3.1 方案一：TP8 Prefill + TP8 Decode（推荐）

每个 GPU 绑定自己亲和的唯一 ionic 设备，保证所有 QP 连接都在同子网内。

创建 JSON 配置文件 `ib_device_map.json`：

```json
{
    "0": "ionic_0",
    "1": "ionic_1",
    "2": "ionic_2",
    "3": "ionic_3",
    "4": "ionic_4",
    "5": "ionic_5",
    "6": "ionic_6",
    "7": "ionic_7"
}
```

Prefill 和 Decode 两端使用相同的配置文件。

**通信模式**：

```
Prefill GPU_N (ionic_N) ──→ Decode GPU_N (ionic_N)   全部同子网
```

**效果**：
- 8 条 RDMA 链路并行，总带宽 ~1.6 Tbps（vs 单设备 ~200 Gbps）
- 每个 GPU 走自己亲和的 NIC，零跨 NUMA/PCIe switch 开销

### 3.2 方案二：TP4 Prefill + TP8 Decode

Prefill 使用 4 个 GPU，每个 GPU 需要向 2 个 Decode GPU 发送 KV cache。因此 Prefill 端每个 GPU 需要绑定 2 个 ionic 设备，覆盖对端的两个子网。

**Prefill 端** `ib_device_map_prefill.json`：

```json
{
    "0": "ionic_0,ionic_1",
    "1": "ionic_2,ionic_3",
    "2": "ionic_4,ionic_5",
    "3": "ionic_6,ionic_7"
}
```

**Decode 端** `ib_device_map_decode.json`：

```json
{
    "0": "ionic_0",
    "1": "ionic_1",
    "2": "ionic_2",
    "3": "ionic_3",
    "4": "ionic_4",
    "5": "ionic_5",
    "6": "ionic_6",
    "7": "ionic_7"
}
```

**通信模式**（由 SGLang `_resolve_rank_mapping` 决定）：

```
Prefill GPU0 (ionic_0,ionic_1) ──→ Decode GPU0 (ionic_0)   同子网 100.x
                               └──→ Decode GPU1 (ionic_1)   同子网 101.x

Prefill GPU1 (ionic_2,ionic_3) ──→ Decode GPU2 (ionic_2)   同子网 102.x
                               └──→ Decode GPU3 (ionic_3)   同子网 103.x

Prefill GPU2 (ionic_4,ionic_5) ──→ Decode GPU4 (ionic_4)   同子网 104.x
                               └──→ Decode GPU5 (ionic_5)   同子网 105.x

Prefill GPU3 (ionic_6,ionic_7) ──→ Decode GPU6 (ionic_6)   同子网 106.x
                               └──→ Decode GPU7 (ionic_7)   同子网 107.x
```

**原理**：Prefill GPU0 的 TransferEngine 初始化时传入 `ionic_0,ionic_1`，Mooncake 的 `Topology::discover(filter=["ionic_0","ionic_1"])` 会将两个 NIC 都加入可用列表。传输时：
- 远端 Decode GPU0 的 buffer 注册在 `ionic_0` 对应的 segment 上，`selectDevice` 选出 `device_id` 指向 `ionic_0`，QP 建在 `ionic_0↔ionic_0`（同子网）
- 远端 Decode GPU1 的 buffer 注册在 `ionic_1` 对应的 segment 上，`selectDevice` 选出 `device_id` 指向 `ionic_1`，QP 建在 `ionic_1↔ionic_1`（同子网）

**注意**：Prefill GPU0 绑定的两个 NIC 中 `ionic_1` 不是 GPU0 亲和的（亲和的是 `ionic_0`），走 `ionic_1` 传输时会有一跳 PCIe switch 开销，但仍远好于跨 NUMA。

## 4. 脚本改动

### 4.1 创建配置文件

TP8+TP8 场景（Prefill 和 Decode 共用）：

```bash
cat > ib_device_map.json << 'EOF'
{
    "0": "ionic_0",
    "1": "ionic_1",
    "2": "ionic_2",
    "3": "ionic_3",
    "4": "ionic_4",
    "5": "ionic_5",
    "6": "ionic_6",
    "7": "ionic_7"
}
EOF
```

### 4.2 修改启动脚本

`1_start_prefill.sh` 和 `2_start_decode.sh` 中，将：

```bash
IB_DEVICE="${IB_DEVICE:-ionic_0}"
```

改为：

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IB_DEVICE="${IB_DEVICE:-${SCRIPT_DIR}/ib_device_map.json}"
```

`--disaggregation-ib-device` 参数保持不变，SGLang 会自动检测 `.json` 后缀并按 per-GPU 映射解析。

## 5. 与 InferenceMAX (MoRI) 的对比

| | Mooncake (本方案) | MoRI (InferenceMAX) |
|---|---|---|
| 设备传参 | `--disaggregation-ib-device` + JSON 映射 | `NCCL_IB_HCA=ionic_0,...,ionic_7` |
| 设备选择 | SGLang per-GPU 映射 → Mooncake Topology | MoRI 内部同编号配对 |
| 多设备支持 | 需要 JSON 映射保证同子网配对 | 原生支持，自动同编号配对 |
| TP 不等场景 | 需要 Prefill 端绑定多设备 | MoRI 内部处理 |

## 6. 验证方法

配置完成后，可通过以下方式验证：

1. 启动 Prefill / Decode 后检查日志中 Mooncake 初始化的设备列表
2. 观察是否有 `transport retry counter exceeded` 错误
3. 使用 `rdma_test/test_mooncake_rdma.py` 逐设备对测试连通性：
   ```bash
   # 在 node09 上
   python test_mooncake_rdma.py --mode server --local-ip 192.168.100.9 --device ionic_0
   # 在 node07 上
   python test_mooncake_rdma.py --mode client --local-ip 192.168.100.7 --remote-ip 192.168.100.9 --remote-rpc-port <PORT> --device ionic_0
   ```
4. 对比单设备 vs 多设备的 KV cache 传输吞吐（TTFT 指标）
