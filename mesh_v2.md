# Mesh v2: 进展同步与下一步计划

> **日期**: 2026-04-01 | **作者**: AI Inference Team | **状态**: 进展同步

---

## 1. 项目定位

Mesh 是 AMD 面向 MI400 系列的 **PD 分离推理框架**，对标 NVIDIA Dynamo。以 ATOM Plugin 形式适配 vLLM 和 SGLang，以 SGLang `sgl-model-gateway`（SMG）为统一路由层，目标在 [InferenceX](https://inferencex.semianalysis.com/) 打榜。

---

## 2. 已完成工作

| 里程碑                                       | 状态     | 备注       |
| -------------------------------------------- | -------- | ---------- |
| 1P1D vLLM + Mooncake + ATOM Plugin           | ✅ 已完成 |            |
| SMG + Mooncake + SGLang + ATOM + OOT 集成    | ✅ 已完成 |            |
| 1P(TP8) / 1D(TP8) PD 分离                    | ✅ 已跑通 | 验证gsm8k  |
| 1P(TP4) / 1D(TP8) PD 分离                    | ✅ 已跑通 | 验证gsm8k  |
| 2P(TP4) / 1D(TP8) PD 分离                    | ✅ 已跑通 | 验证gsm8k  |
| 2P(TP4) / 2D(TP8) PD 分离                    | ✅ 已跑通 | 验证gsm8k  |
| InferMax 场景性能测试 (1P1D, conc=32, 8k/1k) | ✅ 已完成 | 数据见下方 |
| Decode Kernel Profiling (Vanilla vs ATOM)    | ✅ 已完成 |            |
| PD 吞吐推导与优化目标分析                    | ✅ 已完成 |            |

---

## 3. 当前性能与差距

### 测试环境

DeepSeek-R1 FP8 Dynamic, MI355X, 2 × 8-GPU 节点, Mooncake RDMA, InferMax: ISL=8192, OSL=1024, conc=32

### 1P(TP8) / 1D(TP8) InferMax 性能

| 指标                            | 当前 1P1D (TP8/TP8) | Baseline 参考 (TP4/TP8) | 差距   |
| ------------------------------- | ------------------- | ----------------------- | ------ |
| Token Throughput per GPU        | 670.73              | 1064.52                 | -37.0% |
| Input Token Throughput per GPU  | 1192.41             | 2838.38                 | -58.0% |
| Output Token Throughput per GPU | 149.05              | 177.59                  | -16.1% |

### 差距定位

**核心瓶颈：Decode TPOT。** 当前 21.73ms，目标 ≤ 18.6ms (8P+8D) 或 ≤ 17.8ms (4P+8D)，差距 14~18%。

- **吞吐推导模型**已通过双重验证（自测精度 95.5%，Baseline 方向一致） → 详见 [PD 分离吞吐推导与 Decode 优化目标]( C:\Users\yajizhan\OneDrive - Advanced Micro Devices Inc\文档\markdown wiki\PD\2026-03-31-pd_analysis.md)
- **Kernel Profiling** 定位了 ATOM OOT vs Vanilla SGLang 的逐 kernel 差距（+23.5us/layer kernel 差距 + ~2.5ms scheduler overhead） → 详见 [DeepSeek-R1 Decode Kernel Breakdown](C:\Users\yajizhan\OneDrive - Advanced Micro Devices Inc\文档\markdown wiki\PD\2026-03-31-decode-tp8-profling.md)

**Kernel 级优化方向（来自 Profiling 分析）：**

| 优化项               | 差距         | 方向                              |
| -------------------- | ------------ | --------------------------------- |
| DownProj KSplit 策略 | +4.0us/layer | ATOM 引入 KSplit 拆分大 GEMM      |
| MLA Decode Kernel    | +3.4us/layer | mla_a16w8 → mla_a8w8 或优化 a16w8 |
| CatCopy 消除         | +4.9us/layer | 消除额外的 KV cache copy          |
| AllReduce 方案       | +7.2us/layer | ReduceScatter 效率优化            |
| Fused Norm+Quant     | +4.7us/layer | 多个小 kernel 合并                |
| Scheduler/Overhead   | +2.5ms 总计  | 排查调度/框架层面开销             |

---

## 4. 接下来要做的事情

### 阶段一：Decode 模型优化，对齐 Baseline（最高优先级）

> **目标**：ISL=1, OSL=1k, conc=32 场景 TPOT ≤ 18.6ms，吞吐 ≥ 1,720 tok/s

| #    | 任务                    | 描述                                                         | 优先级 | 状态   |
| ---- | ----------------------- | ------------------------------------------------------------ | ------ | ------ |
| 1.1  | Decode 吞吐优化         | 1/1k conc=32 场景 decode 做到目标吞吐                        | P0     | 📋 TODO |
| 1.2  | Prefill TP4 Profiling   | 8k/1 场景 TP4 出 profiling 数据，达到目标 TTFT               | P0     | 📋 TODO |
| 1.3  | Scheduler Overhead 排查 | Kernel 差距仅 1.43ms 但实测差距 3.99ms，排查 ~2.5ms 非 kernel 开销 | P0     | 📋 TODO |

### 阶段二：DeepSeek FP4 模型

> **目标**：FP4 功能打通，按相同方法出 profiling、分析性能、对齐 baseline

| #    | 任务              | 描述                                                     | 优先级 | 状态   |
| ---- | ----------------- | -------------------------------------------------------- | ------ | ------ |
| 2.1  | FP4 功能打通      | DeepSeek FP4 模型端到端跑通                              | P0     | 📋 TODO |
| 2.2  | FP4 Decode 目标   | 1/1k conc=32 场景给出吞吐目标                            | P0     | 📋 TODO |
| 2.3  | FP4 Prefill 测试  | 8k/1 场景纯 prefill 测试数据，给出性能优化目标和切分方式 | P0     | 📋 TODO |
| 2.4  | FP4 Baseline 对齐 | 按相同方法出 profiling 挖掘优化点，分析性能对齐 baseline | P1     | 📋 TODO |

### 阶段三：EP 支持

> **目标**：EP4/EP8 跑通并达到 InferMax 最佳吞吐

| #    | 任务             | 描述                            | 优先级 | 状态   |
| ---- | ---------------- | ------------------------------- | ------ | ------ |
| 3.1  | EP 跑通          | 支持 EP4、EP8 功能验证          | P0     | 📋 TODO |
| 3.2  | EP Profiling     | EP 性能 profiling 分析          | P0     | 📋 TODO |
| 3.3  | EP InferMax 最优 | EP 配置下 InferMax 达到最佳吞吐 | P0     | 📋 TODO |

### 阶段四：Smart Router

> **目标**：复用 Dynamo 在 SGLang 预留的接口，多P多D场景做到 InferMax SOTA

| #    | 任务                   | 描述                                   | 优先级 | 状态   |
| ---- | ---------------------- | -------------------------------------- | ------ | ------ |
| 4.1  | Dynamo SGLang 接口对接 | 实时获取 KV cache 和负载信息做智能调度 | P0     | 📋 TODO |
| 4.2  | 智能调度实现           | 在多P多D场景做智能调度                 | P0     | 📋 TODO |
| 4.3  | InferMax SOTA          | 多P多D场景 InferMax 达到 SOTA          | P0     | 📋 TODO |

### 阶段五：PD 混合调度（待决策）

> **前提**：需先结合数据做理论分析，分析清楚再决定做不做

| #    | 任务             | 描述                                        | 优先级 | 状态   |
| ---- | ---------------- | ------------------------------------------- | ------ | ------ |
| 5.1  | 理论分析         | P 节点做 P+D 混合 / D 节点做混合的收益分析  | P1     | 📋 TODO |
| 5.2  | 决策             | 结合数据分析决定是否实施                    | P1     | 📋 TODO |
| 5.3  | 实现（如决定做） | kv_both 动态调度，参考 Dynamo DisaggPlanner | P2     | 📋 TODO |

### 后续规划（v1 文档中未完成项，持续叠加）

| #    | 任务                    | 描述                                          | 优先级 | 状态         |
| ---- | ----------------------- | --------------------------------------------- | ------ | ------------ |
| 6.1  | Wide EP Decode          | EP16/32 跨节点                                | P1     | 📋 TODO       |
| 6.2  | MoRI 替换 Mooncake      | 对接 SMG bootstrap 机制                       | P1     | 🔧 同步开发中 |
| 6.3  | 非阻塞传输优化          | KV 传输与 compute 最大 overlap                | P1     | 📋 TODO       |
| 6.4  | 异构 Pool 路由          | (ISL, TTFT) → P Pool, (ctx_len, ITL) → D Pool | P1     | 📋 TODO       |
| 6.5  | P/D Replica 动态扩缩    | 基于 SLA (TTFT/ITL) + 流量预测                | P2     | 📋 TODO       |
| 6.6  | MI GPU Profiling 自动化 | 采集 MI300X/MI355X kernel 性能数据            | P1     | 📋 TODO       |
| 6.7  | 配置搜索引擎            | (model, GPU, SLA) → 最优 PD 配置              | P2     | 📋 TODO       |
| 6.8  | 自动 Benchmark CI/CD    | 性能回归检测                                  | P2     | 📋 TODO       |
| 6.9  | K8s 部署                | SMG K8s 服务发现 + mesh 集群管理              | P1     | 📋 TODO       |
| 6.10 | MI400 适配              | MI400 系列特性优化                            | P0     | 📋 TODO       |

---

## 5. 架构

```
                    ┌──────────────┐
                    │   Client     │
                    └──────┬───────┘
                           │
              ┌────────────▼────────────┐
              │   SGLang Model Gateway  │  Rust, 统一路由层
              │                         │
              │  PD Router (HTTP+gRPC)  │  dual-dispatch + bootstrap KV
              │  P: cache_aware         │  独立路由策略
              │  D: power_of_two        │
              │  Mesh: gossip 同步      │  跨节点 cache tree 一致性
              │                         │
              │  [规划] Smart Router    │  Dynamo 接口获取 KV cache/负载
              │  [规划] Multi-Pool      │  异构 Pool 路由
              └──┬──────────────┬───────┘
                 │              │
        ┌────────▼──┐    ┌─────▼──────┐
        │ Prefill   │    │ Decode     │
        │ (TP4/TP8) │    │ (TP8/EP16) │
        │ ATOM+OOT  │    │ ATOM+OOT   │
        └─────┬─────┘    └────┬───────┘
              │               │
              └─── MoRI/Mooncake ───┘  bootstrap_host/port/room
```

---

## 6. SMG 能力矩阵（缺口分析）

| 目标能力               | SMG 现状 | 从哪补                                        |
| ---------------------- | -------- | --------------------------------------------- |
| PD 路由                | ✅        | —                                             |
| 非阻塞 KV 传输         | ✅        | 需验证 MoRI 对接                              |
| Cache-Aware 路由       | ✅        | —                                             |
| 负载均衡 (8 种策略)    | ✅        | —                                             |
| 熔断/重试/限流         | ✅        | —                                             |
| 集群管理 (gossip)      | ✅        | —                                             |
| 多模型路由 (IGW)       | ✅        | —                                             |
| 可观测性 (40+ metrics) | ✅        | —                                             |
| K8s 服务发现           | ✅        | —                                             |
| kv_both 动态调度       | ❌        | Dynamo DisaggPlanner（待决策）                |
| 异构 Pool 路由         | ⚠️        | Dynamo GlobalRouter                           |
| P/D Replica 动态扩缩   | ❌        | Dynamo PrefillPlanner/DecodePlanner           |
| Wide EP Decode         | ❌        | llm-d Wide EP 指南                            |
| 配置自动搜索           | ❌        | aiconfigurator                                |
| MI GPU Profiling       | ❌        | Dynamo profiler/                              |
| DP Routing             | ❌        | vLLM Router `--intra-node-data-parallel-size` |

---

## 7. InferenceX 打榜策略

### NVIDIA 策略

```
低并发 (c=1~16):   纯 TP8
中并发 (c=16~64):  PD 分离 + 中等 EP
高并发 (c=64+):    PD 分离 + Wide EP16/32/64

P/D 异构: P 用 TP4/TP8, D 用 TP8/EP16/32
配置自动化: aiconfigurator 搜索最优组合
```

### AMD 现状与差距

| 项目                     | 状态                           |
| ------------------------ | ------------------------------ |
| FP8 单节点               | **竞争力强**，MI355X 接近 B200 |
| PD 分离                  | 中等（Mooncake/MoRI vs NIXL）  |
| Decode 性能              | **核心瓶颈**，TPOT 差距 14~18% |
| Wide EP                  | 大（支持有限）                 |
| 组合优化 (PD+WideEP+FP4) | **核心差距**                   |

---

## 8. 关联文档

- [DeepSeek-R1 Decode Kernel Breakdown: Vanilla SGLang vs ATOM OOT](链接待补) — Kernel 级逐层对比分析
- [PD 分离吞吐推导与 Decode 优化目标](链接待补) — 吞吐模型、验证、优化目标推导
- [Mesh v1 技术调研与路线图](链接待补) — 初始技术调研文档

---

## 9. 参考资源

**SGLang 生态**

- [SGLang Model Gateway](https://github.com/sgl-project/sglang/tree/main/sgl-model-gateway) | [文档](https://docs.sglang.io/advanced_features/sgl_model_gateway.html)
- [SGLang PD 分离](https://docs.sglang.ai/advanced_features/pd_disaggregation.html) | [PD on AMD](https://rocm.docs.amd.com/projects/ai-developer-hub/en/latest/notebooks/inference/SGlang_PD_Disagg_On_AMD_GPU.html)

**开源项目**

- [InferenceX](https://inferencex.semianalysis.com/) | [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo) | [aiconfigurator](https://github.com/ai-dynamo/aiconfigurator) | [llm-d](https://github.com/llm-d/llm-d)

**AMD 技术栈**

- [MoRI](https://github.com/ROCm/mori) | [MI355X 性能](https://www.amd.com/en/developer/resources/technical-articles/2026/distributed-inference-performance-on-instinct-mi355x-gpu.html)

**KV 传输**

- [Mooncake](https://github.com/kvcache-ai/Mooncake) | [Disagg Inference Retrospective](https://haoailab.com/blogs/distserve-retro/)
