# Mesh v3: 讨论会议文档

> **日期**: 2026-04-10 | **作者**: AI Inference Team | **状态**: 会议讨论

---

## 1. 近期进展回顾

### 性能突破：Mesh 已超越 MoRI 基准

| 指标                            | Mesh + ATOM + SGLang OOT | MoRI Baseline | 提升    |
| ------------------------------- | ------------------------ | ------------- | ------- |
| Decode Throughput per GPU       | **204 tok/s**            | 177 tok/s     | +15.3%  |
| Prefill Throughput per GPU      | **3283 tok/s**           | 2838 tok/s    | +15.7%  |

### 已完成工作

| 里程碑                                    | 状态     | 备注                              |
| ----------------------------------------- | -------- | --------------------------------- |
| Mesh + ATOM + SGLang OOT 集成             | ✅ 已完成 | Decode / Prefill 均超越 MoRI 基准 |
| Mesh PR 提交                              | ✅ 已提交 | [ROCm/ATOM#502](https://github.com/ROCm/ATOM/pull/502) |
| v2 阶段 Decode Kernel 优化                | ✅ 已完成 | TPOT 差距已解决                   |
| 功能裁剪（25 项删除）                     | ✅ 已完成 | 参考 [FEATURE_ANALYSIS.md](https://github.com/Jasen2201/ATOM/blob/Atom_mesh/mesh/FEATURE_ANALYSIS.md)，已删除全部 25 项非核心功能 |
| 全功能单元测试                            | ✅ 已完成 | [TEST_REPORT.md](https://github.com/Jasen2201/ATOM/blob/Atom_mesh/mesh/e2e_test/TEST_REPORT.md) |

### 当前进行中

| 任务                       | 状态         | 备注                         |
| -------------------------- | ------------ | ---------------------------- |
| DeepSeek MTP 模型优化      | 🔧 进行中    |                              |
| DeepSeek R1 FP4            | 🔧 进行中    |                              |
| DeepSeek DP & EP           | 🔧 进行中    |                              |
| PD 自动化集群测试脚本      | 🔧 进行中    |                              |

---

## 2. 今日讨论议题

### 议题一：Mesh 功能裁剪 — 保留 vs 删除

> **参考文档**: [FEATURE_ANALYSIS.md](https://github.com/Jasen2201/ATOM/blob/Atom_mesh/mesh/FEATURE_ANALYSIS.md)

**目标**: 明确 Mesh 的功能边界，去除冗余功能，聚焦核心能力。

**当前状态**: 已按分析文档完成 25 项非核心功能的删除（OpenAI 模式、Harmony 协议、WASM 中间件、认证、MCP 等），保留 42 项核心功能，剩余 4 项待讨论。

**讨论要点**:

- 确认已删除的 25 项功能是否有遗漏或误删
- 4 项待讨论功能的最终决策：
  - **4.8 Conversations API** — 对话管理是否是长期产品需求？
  - **8.2 OpenTelemetry** — 分布式追踪对多节点调试有价值
  - **9.3 Concurrency Limiter** — 打榜时防 OOM 有用，但默认禁用可保留代码
  - **16.1 Python Binding** — 取决于 ATOM 的集成方式
- 依赖裁剪确认（`openai-harmony`, `mesh-mcp`, `mesh-wasm`, `mesh-auth` 等）

**期望产出**: 4 项待讨论功能的最终决策 + 确认依赖裁剪清单。

---

### 议题二：ATOM 如何接入 Mesh

> **参考文档**: [atom_grpc_integration.md](https://github.com/Jasen2201/ATOM/blob/Atom_mesh/mesh/docs/atom_grpc_integration.md)

**目标**: 确定 ATOM 与 Mesh 的集成方案，对齐接口设计。

**讨论要点**:

- gRPC 集成方案的可行性与性能影响
- ATOM 侧需要做哪些适配改动？
- 与现有 SMG 路由层的交互方式
- 集成后的端到端测试验证方案

**期望产出**: 确认集成方案，明确 ATOM 侧改动范围。

---

### 议题三：Mesh 最终产品形态 & PR 合入路径

> **参考**: [ROCm/ATOM#502](https://github.com/ROCm/ATOM/pull/502) PR 描述

**目标**: 对齐 Mesh 作为产品交付的最终形态，明确 PR 合入前的剩余工作。

**讨论要点**:

- Mesh 的交付形态：独立组件 vs ATOM 内置模块？
- PR 当前状态 review：代码质量、测试覆盖、文档完整度
- PR 合入的前置条件和阻塞项有哪些？
- 合入后的维护责任和迭代计划

**期望产出**: PR 合入 checklist + 时间线。

---

## 3. 后续规划（待会议确认后更新）

| 阶段     | 目标                              | 优先级 | 状态   |
| -------- | --------------------------------- | ------ | ------ |
| 阶段一   | 功能裁剪 + PR 合入                | P0     | 📋 TODO |
| 阶段二   | DeepSeek MTP / FP4 / DP / EP 优化 | P0     | 🔧 进行中 |
| 阶段三   | PD 自动化集群测试                 | P0     | 🔧 进行中 |
| 阶段四   | Smart Router + 多P多D 调度        | P1     | 📋 TODO |
| 阶段五   | InferenceX 打榜                   | P1     | 📋 TODO |
