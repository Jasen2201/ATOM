# Mesh Router 项目功能分析

> 本文档基于代码逐文件分析，列举 mesh router 项目的所有功能。
> 每个功能提供三个选项供讨论：保留 / 删除 / 待讨论。
> 目标：以最小化功能原则迁移到 PD 分离项目中。

---

## 1. 路由模式 (Routing Modes)

### 1.1 Regular 模式 (常规路由)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/config/types.rs:153` (`RoutingMode::Regular`)，`src/routers/http/router.rs`，`src/routers/grpc/router.rs`

**详细说明**: 常规路由模式，将请求转发到一组同质的 worker 节点。所有 worker 被视为等价的，通过负载均衡策略（如 round_robin、random 等）选择目标 worker。适用于不做 prefill/decode 分离的场景。支持 HTTP 和 gRPC 两种通信协议。通过 `--worker-urls` 指定 worker 列表。

---

### 1.2 PrefillDecode 模式 (PD 分离路由)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/config/types.rs:156` (`RoutingMode::PrefillDecode`)，`src/routers/http/pd_router.rs`，`src/routers/grpc/pd_router.rs`

**详细说明**: PD（Prefill-Decode）分离路由模式，这是本项目的核心功能。将推理请求拆分为 prefill 阶段和 decode 阶段，分别路由到不同类型的 worker。prefill worker 处理输入 token 的计算，decode worker 处理自回归生成。支持为 prefill 和 decode 分别配置独立的路由策略（`--prefill-policy`、`--decode-policy`）。每个 prefill worker 可以配置可选的 bootstrap port（用于 mooncake 等 KV cache 传输实现）。HTTP PD Router 实现了完整的双阶段转发逻辑：先向 prefill worker 发送请求，再从 decode worker 获取流式响应。gRPC PD Router 通过 gRPC pipeline 实现同样的双阶段调度。

---

### 1.3 OpenAI 模式 (OpenAI 兼容路由)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/config/types.rs:166` (`RoutingMode::OpenAI`)，`src/routers/openai/router.rs`

**详细说明**: OpenAI 兼容路由模式，作为代理将请求转发到 OpenAI 兼容的后端（如 OpenAI API、Anthropic API 等）。支持多模型注册与发现（通过 ModelCard 和 ProviderRegistry）。实现了 OpenAI Responses API（`/v1/responses`）的完整 CRUD，包括创建、获取、取消、删除 response 以及列出 input items。支持 MCP tool calling 的多轮循环调用。支持 streaming（SSE）和 non-streaming 响应。通过 `--backend openai` 或 `--backend anthropic` 激活。

---

## 2. 通信协议层

### 2.1 HTTP 路由转发
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/routers/http/router.rs`，`src/routers/http/pd_router.rs`，`src/routers/http/pd_types.rs`

**详细说明**: 基于 HTTP/HTTPS 的请求转发。使用 `reqwest` 客户端向后端 worker 发送 HTTP 请求。支持流式响应（SSE）的透传，通过 `UnboundedReceiverStream` 转发 chunk 数据。HTTP PD Router 实现了完整的 prefill→decode 两阶段请求流：先向 prefill worker POST 请求，再将 decode worker 的流式响应返回客户端。支持自定义 HTTP header 转发（如 `X-SMG-Routing-Key`、`X-SMG-Target-Worker`）。

---

### 2.2 gRPC 路由转发
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/routers/grpc/`（整个目录），`src/routers/grpc/client.rs`，`src/routers/grpc/pipeline.rs`

**详细说明**: 基于 gRPC（tonic）的请求转发。通过 `smg-grpc-client` crate 封装 SGLang 调度器的 gRPC proto。实现了 `RequestPipeline` 作为统一的请求处理流水线，包含以下阶段：
- **client_acquisition**: 获取 gRPC 客户端连接
- **worker_selection**: 基于策略选择目标 worker
- **request_execution**: 执行 gRPC 调用
- **response_processing**: 处理和格式化响应

支持 chat completion、generate、embedding、classify 等多种请求类型的 gRPC 转发。支持流式和非流式响应。proto 定义来自 SGLang scheduler。

---

### 2.3 Harmony 协议支持
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/routers/grpc/harmony/`（整个目录）

**详细说明**: Harmony 是 GPT-OSS 模型使用的编码/解析协议，基于 channel 的方式组织输出：
- **analysis channel**: 推理/思考内容（可选）
- **commentary channel**: 工具调用（可选）
- **final channel**: 最终响应文本（必需）

实现包括：
- `HarmonyDetector`: 检测模型是否支持 Harmony 协议
- `HarmonyBuilder`: 将 Chat/Responses 请求编码为 input_ids
- `HarmonyParserAdapter`: 将 output_ids 解析为各 channel 的内容
- `HarmonyStreamingProcessor`: 处理 Harmony 流式响应
- `HarmonyResponseProcessor`: 处理多轮工具调用的迭代

依赖外部 crate `openai-harmony`。

---

## 3. 负载均衡策略 (Load Balancing Policies)

### 3.1 Random 策略
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/policies/random.rs`

**详细说明**: 随机选择策略。从所有健康的 worker 中随机选择一个进行请求转发。最简单的负载均衡策略，不考虑 worker 负载。

---

### 3.2 Round Robin 策略
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/policies/round_robin.rs`

**详细说明**: 轮询选择策略。按照固定顺序依次选择 worker，使用原子计数器 `AtomicUsize` 实现线程安全的轮询。保证请求在 worker 之间均匀分配。

---

### 3.3 CacheAware 策略
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/policies/cache_aware.rs`，`src/policies/tree.rs`

**详细说明**: 缓存感知路由策略。基于 Radix Tree（前缀树）实现 KV cache 感知的路由决策。核心思想是将相似前缀的请求路由到同一个 worker，以最大化 KV cache 命中率。主要参数：
- `cache_threshold` (0.0-1.0): 缓存匹配阈值，超过该比例认为有效匹配
- `balance_abs_threshold`: 负载差异绝对阈值
- `balance_rel_threshold`: 负载差异相对阈值
- `eviction_interval_secs`: 缓存淘汰周期
- `max_tree_size`: 前缀树最大节点数

需要请求文本（`needs_request_text() = true`）来计算前缀匹配。

---

### 3.4 PowerOfTwo 策略
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/policies/power_of_two.rs`

**详细说明**: "两选一最轻"策略。随机选择两个 worker，然后选择负载较低的那个。在保持简单性的同时能较好地平衡负载。通过 `LoadMonitor` 定期从 worker 拉取实际负载信息（通过 `/get_loads` 或 engine metrics）。需要定期更新 worker 负载数据（`update_loads()`）。

---

### 3.5 Bucket 策略
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/policies/bucket.rs`

**详细说明**: 桶分区策略。将请求按哈希值分配到不同的桶（bucket），每个桶映射到一个 worker。定期调整桶边界以平衡负载。参数包括：
- `balance_abs_threshold`: 负载差异绝对阈值
- `balance_rel_threshold`: 负载差异相对阈值
- `bucket_adjust_interval_secs`: 桶边界调整周期

---

### 3.6 Manual 策略 (粘性会话)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/policies/manual.rs`

**详细说明**: 手动路由/粘性会话策略。通过 HTTP header `X-SMG-Routing-Key` 实现会话亲和性。使用 DashMap 缓存路由键到 worker 的映射。当遇到新的路由键时，支持三种分配模式（`assignment_mode`）：
- `random`: 随机分配
- `min_load`: 分配给负载最小的 worker
- `min_group`: 分配给活跃路由键最少的 worker

支持 TTL 淘汰（`max_idle_secs`），默认 4 小时无活动后释放。添加 worker 时不会重新分配已有键（零键重分配）。

---

### 3.7 ConsistentHashing 策略
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/policies/consistent_hashing.rs`

**详细说明**: 一致性哈希策略。使用哈希环实现会话亲和性。支持两种路由方式：
- `X-SMG-Target-Worker`: 直接指定目标 worker URL
- `X-SMG-Routing-Key`: 基于一致性哈希选择 worker

O(log n) 查找复杂度。拓扑变化时只有约 1/N 的键需要重新分配。哈希环由 `WorkerRegistry` 构建和缓存，通过 `SelectWorkerInfo::hash_ring` 传递到策略层，避免每次请求重建。

---

### 3.8 PrefixHash 策略
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/policies/prefix_hash.rs`

**详细说明**: 前缀哈希策略，CacheAware 策略的轻量替代方案。基于请求前缀 token 的哈希值进行路由，实现 KV cache 局部性。使用一致性哈希环结合有界负载均衡：如果选中的 worker 过载（load > avg * load_factor），则沿哈希环顺序查找下一个可用 worker。参数：
- `prefix_token_count`: 用于哈希的前缀 token 数量（默认 256）
- `load_factor`: 负载因子阈值（默认 1.25）

O(log n) 查找代替 CacheAware 的 O(prefix_len) 前缀树遍历。

---

### 3.9 Policy Registry (策略注册中心)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/policies/registry.rs`，`src/policies/factory.rs`

**详细说明**: 策略注册与管理中心。`PolicyRegistry` 管理 model_id → policy 的映射，支持多模型场景下为不同模型配置不同的路由策略。`PolicyFactory` 根据 `PolicyConfig` 创建对应的策略实例。在 PD 模式下，为 prefill 和 decode 分别维护独立的策略注册。策略支持 Mesh 同步（通过 `set_mesh_sync`）。

---

## 4. API 端点 (API Endpoints)

### 4.1 Chat Completion API (`/v1/chat/completions`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:184-193`

**详细说明**: OpenAI 兼容的聊天补全 API。接收 `ChatCompletionRequest`，包含 messages 数组、model、temperature 等参数。支持流式（`stream: true`）和非流式响应。使用 `ValidatedJson` 进行请求体验证。支持通过 header 进行策略路由。

---

### 4.2 Completion API (`/v1/completions`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:195-204`

**详细说明**: OpenAI 兼容的文本补全 API。接收 `CompletionRequest`，支持 prompt 文本补全。与 chat completion 类似但不使用消息格式。

---

### 4.3 Generate API (`/generate`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:172-182`

**详细说明**: SGLang 原生的生成 API。接收 `GenerateRequest`，直接支持 SGLang 的底层生成参数（如 input_ids、sampling_params 等）。这是 SGLang 的原始接口，不遵循 OpenAI 格式。

---

### 4.4 Responses API (`/v1/responses`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:229-305`，`src/routers/openai/responses/`

**详细说明**: OpenAI Responses API 的完整实现，包括：
- `POST /v1/responses`: 创建 response（支持 streaming 和 non-streaming）
- `GET /v1/responses/{response_id}`: 获取已存储的 response
- `POST /v1/responses/{response_id}/cancel`: 取消进行中的 response
- `DELETE /v1/responses/{response_id}`: 删除 response
- `GET /v1/responses/{response_id}/input_items`: 列出 response 的输入项

支持 MCP 工具调用的多轮执行循环（tool_handler）。通过 `ResponseStorage` 接口持久化 response 数据。实现了 `ResponseAccumulator` 用于聚合流式 response 片段。

---

### 4.5 Embedding API (`/v1/embeddings`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:240-249`

**详细说明**: OpenAI 兼容的向量嵌入 API。接收 `EmbeddingRequest`，将文本转换为向量表示。通过 gRPC 或 HTTP 转发到后端 embedding 模型。

---

### 4.6 Rerank API (`/rerank`, `/v1/rerank`)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:206-227`

**详细说明**: 重排序 API，提供两个端点兼容不同格式。接收 `RerankRequest`，对候选文档按相关性重新排序。`/v1/rerank` 接收 `V1RerankReqInput` 格式并自动转换。

---

### 4.7 Classify API (`/v1/classify`)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/server.rs:251-260`

**详细说明**: 分类 API。接收 `ClassifyRequest`，将文本分类到预定义的类别。转发到支持分类任务的后端模型。

---

### 4.8 Conversations API (`/v1/conversations`)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/server.rs:307-410`，`src/routers/conversations/handlers.rs`

**详细说明**: 对话管理 API 的完整 CRUD 实现：
- `POST /v1/conversations`: 创建对话
- `GET /v1/conversations/{id}`: 获取对话
- `POST /v1/conversations/{id}`: 更新对话
- `DELETE /v1/conversations/{id}`: 删除对话
- `GET /v1/conversations/{id}/items`: 列出对话项（支持分页：limit, order, after）
- `POST /v1/conversations/{id}/items`: 创建对话项（每次最多 20 项）
- `GET /v1/conversations/{id}/items/{item_id}`: 获取对话项
- `DELETE /v1/conversations/{id}/items/{item_id}`: 删除对话项

支持的对话项类型包括：message、reasoning、mcp_list_tools、mcp_call、item_reference、function_call、function_call_output 等。每个对话最多 16 个 metadata 属性。数据通过 `ConversationStorage` 和 `ConversationItemStorage` 接口持久化。

---

### 4.9 Tokenize / Detokenize API
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:483-527`，`src/routers/tokenize/handlers.rs`

**详细说明**: Tokenizer 管理和使用 API：
- `POST /v1/tokenize`: 将文本编码为 token IDs
- `POST /v1/detokenize`: 将 token IDs 解码为文本
- `POST /v1/tokenizers`: 添加新的 tokenizer
- `GET /v1/tokenizers`: 列出所有已注册的 tokenizer
- `GET /v1/tokenizers/{id}`: 获取 tokenizer 信息
- `GET /v1/tokenizers/{id}/status`: 获取 tokenizer 加载状态
- `DELETE /v1/tokenizers/{id}`: 删除 tokenizer

Tokenizer 通过 `TokenizerRegistry` 管理，支持从 HuggingFace model ID 或本地路径加载。支持两级缓存：L0（精确匹配缓存）和 L1（前缀匹配缓存）。启动时可通过 `--tokenizer-path` 或 `--model-path` 自动加载。

---

### 4.10 Parse API（函数调用 / 推理解析）
- [x] 保留 - [ ] 删除 - [] 待讨论

**代码位置**: `src/server.rs:80-92`，`src/routers/parse/handlers.rs`

**详细说明**: 输出解析 API，用于后处理模型输出：
- `POST /parse/function_call`: 从模型输出文本中解析函数调用（tool calls），使用 `tool_parser` crate
- `POST /parse/reasoning`: 从模型输出中分离推理内容（thinking/reasoning），使用 `reasoning_parser` crate

支持多种解析器（通过 `--tool-call-parser` 和 `--reasoning-parser` 配置）。

---

### 4.11 Worker 管理 API
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:424-477`，`src/core/worker_service.rs`

**详细说明**: Worker 动态管理 REST API：
- `POST /workers`: 创建新的 worker（动态添加）
- `GET /workers`: 列出所有 worker
- `GET /workers/{id}`: 获取 worker 详细信息
- `PUT /workers/{id}`: 更新 worker 属性（如健康状态、优先级等）
- `DELETE /workers/{id}`: 删除 worker

通过 `WorkerService` 业务逻辑层协调 `WorkerRegistry` 和 `JobQueue`。支持运行时动态添加/删除 worker，无需重启服务。Worker 通过 UUID 唯一标识。

---

### 4.12 健康检查 / 就绪检查 API
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:94-170`

**详细说明**: 提供多种健康检查端点：
- `GET /liveness`: 存活检查，始终返回 200 OK
- `GET /readiness`: 就绪检查，验证是否有足够的健康 worker（PD 模式要求同时有 prefill 和 decode worker）
- `GET /health`: 基本健康检查
- `GET /health_generate`: 通过生成请求验证后端 worker 健康
- `GET /engine_metrics`: 从所有 worker 抓取引擎指标

---

### 4.13 模型信息 API
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:160-170`

**详细说明**: 
- `GET /v1/models`: OpenAI 兼容的模型列表 API
- `GET /get_model_info`: 获取模型详细信息
- `GET /get_server_info`: 获取服务器信息

在 IGW（多模型）模式下返回所有已注册模型的信息。

---

### 4.14 缓存管理 API
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:412-420`

**详细说明**: 
- `POST /flush_cache`: 清除所有 worker 的 KV cache
- `GET /get_loads`: 获取所有 worker 的负载信息

通过 `WorkerManager` 向所有注册的 worker 发送缓存清除请求或负载查询请求。

---

## 5. Mesh / HA 高可用功能

### 5.1 Mesh Server (Gossip 协议集群)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/server.rs:742-796`（启动代码），外部 crate `smg-mesh`

**详细说明**: 基于 Gossip 协议的分布式集群功能。多个 router 实例通过 mesh 网络组成高可用集群。使用 CRDT（Conflict-free Replicated Data Types）实现无冲突的状态同步。核心组件：
- `MeshServerHandler`: 处理 mesh 协议消息
- `MeshSyncManager`: 管理跨节点的状态同步
- `PartitionDetector`: 检测网络分区
- `StateStores`: 存储集群状态（成员关系、worker 状态、策略状态）

通过 `--enable-mesh`、`--mesh-server-name`、`--mesh-host`、`--mesh-port`、`--mesh-peer-urls` 配置。

---

### 5.2 Mesh 管理 API (`/ha/*`)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/server.rs:664-681`，`src/routers/mesh/handlers.rs`

**详细说明**: HA 集群管理的 REST API：
- `GET /ha/status`: 获取集群状态（节点列表、存储统计）
- `GET /ha/health`: 集群健康检查
- `GET /ha/workers`: 获取所有 worker 的全局状态
- `GET /ha/workers/{id}`: 获取单个 worker 的全局状态
- `GET /ha/policies`: 获取所有策略的全局状态
- `GET /ha/policies/{model_id}`: 获取某个模型的策略状态
- `GET /ha/config/{key}`: 获取集群配置
- `POST /ha/config`: 更新集群配置
- `POST /ha/rate-limit`: 设置全局速率限制
- `GET /ha/rate-limit`: 获取全局速率限制配置
- `GET /ha/rate-limit/stats`: 获取全局速率限制统计
- `POST /ha/shutdown`: 触发优雅关闭

---

### 5.3 全局速率限制 (Global Rate Limiting)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/server.rs:762-766`，`smg-mesh` crate 的 `RateLimitWindow`

**详细说明**: 跨 mesh 节点的全局速率限制。使用哈希环将速率限制计数分布到不同节点。`RateLimitWindow` 定期重置计数器（默认每秒一次）。通过 `/ha/rate-limit` API 动态配置。

---

## 6. Worker 管理

### 6.1 Worker Registry (Worker 注册中心)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/worker_registry.rs`

**详细说明**: 集中管理所有 worker 的注册和查询。维护 worker 列表及其健康状态。提供按类型（prefill/decode/regular）筛选 worker 的能力。构建和缓存一致性哈希环（`HashRing`）。支持 mesh 同步（通过 `set_mesh_sync`）。提供 `start_health_checker()` 启动周期性健康检查。通过 `WorkerId`（UUID）唯一标识 worker。

---

### 6.2 Worker Builder (Worker 构建器)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/worker_builder.rs`

**详细说明**: 使用 Builder 模式创建 worker 实例。`BasicWorkerBuilder` 支持设置 worker URL、类型（Prefill/Decode/Regular）、API key、优先级、成本等属性。`DPAwareWorkerBuilder` 扩展支持 DP（数据并行）感知的 worker 分组。

---

### 6.3 Worker Manager
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/worker_manager.rs`

**详细说明**: Worker 管理器，提供高级 worker 操作：
- `get_worker_urls()`: 获取所有 worker URL
- `get_engine_metrics()`: 从所有 worker 抓取引擎 metrics
- `flush_cache_all()`: 清除所有 worker 的缓存
- `get_all_worker_loads()`: 获取所有 worker 的负载信息
- `LoadMonitor`: 后台监控任务，定期拉取 worker 负载并更新策略

---

### 6.4 Health Check (健康检查)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/config/types.rs` (`HealthCheckConfig`)，`src/core/worker_registry.rs`

**详细说明**: Worker 健康检查系统。可配置参数：
- `failure_threshold`: 连续失败多少次标记为不健康（默认 3）
- `success_threshold`: 连续成功多少次标记为健康（默认 2）
- `timeout_secs`: 单次健康检查超时（默认 5 秒）
- `check_interval_secs`: 检查间隔（默认 60 秒）
- `endpoint`: 健康检查端点路径（默认 `/health`）
- `disable_health_check`: 全局禁用健康检查

---

### 6.5 Circuit Breaker (熔断器)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/circuit_breaker.rs`，`src/config/types.rs` (`CircuitBreakerConfig`)

**详细说明**: 基于滑动窗口的熔断器实现，保护后端 worker 免受级联故障影响。三种状态：
- **Closed**: 正常工作，记录失败次数
- **Open**: 熔断，拒绝所有请求
- **HalfOpen**: 尝试恢复，允许少量请求通过

可配置参数：
- `failure_threshold`: 失败次数阈值（默认 10）
- `success_threshold`: HalfOpen 态恢复所需成功次数（默认 3）
- `timeout_duration_secs`: 从 Open 态自动进入 HalfOpen 态的等待时间（默认 60 秒）
- `window_duration_secs`: 滑动窗口时长（默认 120 秒）
- 可通过 `--disable-circuit-breaker` 禁用

---

### 6.6 Retry (重试机制)
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/retry.rs`，`src/config/types.rs` (`RetryConfig`)

**详细说明**: 指数退避重试机制。当请求到某个 worker 失败时自动重试到其他 worker。`RetryExecutor` 实现了通用的重试逻辑。可配置参数：
- `max_retries`: 最大重试次数（默认 5）
- `initial_backoff_ms`: 初始退避延迟（默认 50ms）
- `max_backoff_ms`: 最大退避延迟（默认 30000ms）
- `backoff_multiplier`: 退避倍数（默认 1.5）
- `jitter_factor`: 抖动因子（默认 0.2），用于 D' = D * (1 + U[-j, +j])
- 可通过 `--disable-retries` 禁用

---

### 6.7 Job Queue (作业队列)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/core/job_queue.rs`

**详细说明**: 异步作业队列，用于管理 worker 的初始化和配置变更。支持的作业类型包括：
- `InitializeWorkersFromConfig`: 从配置初始化所有 worker
- `AddTokenizer`: 添加 tokenizer
- `InitializeMcpServers`: 初始化 MCP 服务器
- 动态 worker 创建/删除

使用 `tokio::sync::mpsc` 实现异步任务处理。Workflow engine（`wfaas` crate）提供可观测的多步骤工作流执行。

---

### 6.8 DP Aware 调度
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/worker_builder.rs` (`DPAwareWorkerBuilder`)，CLI `--dp-aware`

**详细说明**: 数据并行感知调度。当 `--dp-aware` 启用时，通过 worker 发现阶段检测 DP 分组（data parallel groups），将属于同一 DP 组的 worker 关联在一起。在路由时考虑 DP 组关系，将请求发送到同一 DP 组的 worker 以优化通信。

---

## 7. Kubernetes 服务发现

### 7.1 Service Discovery
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/service_discovery.rs`

**详细说明**: Kubernetes 原生的服务发现，自动发现并注册后端 worker Pod。功能包括：
- 通过 Label Selector 筛选 worker Pod（`--selector key=value`）
- PD 模式下分别发现 prefill 和 decode Pod（`--prefill-selector`、`--decode-selector`）
- 使用 Kubernetes Watch API 实时监听 Pod 变化
- 自动检测 bootstrap port（通过 Pod annotation `sglang.ai/bootstrap-port`）
- 支持 namespace 过滤
- 支持发现其他 router 节点用于 mesh 组网（`router_selector`、`router_mesh_port_annotation`）
- 当 Pod Ready 时自动注册为 worker，当 Pod 删除时自动移除

依赖 `kube` 和 `k8s-openapi` crate。

---

## 8. 可观测性 (Observability)

### 8.1 Prometheus Metrics
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/observability/metrics.rs`

**详细说明**: Prometheus 指标采集和暴露。启动独立的 Prometheus exporter 服务（`--prometheus-port`，默认 29000）。收集的指标包括：
- 请求计数（按方法、路径、状态码分组）
- 请求延迟直方图（可自定义 buckets）
- 并发请求数
- Worker 健康状态
- 路由策略选择统计

支持从后端 worker 聚合 Prometheus 指标（`MetricsAggregator`，`src/core/metrics_aggregator.rs`）。

---

### 8.2 OpenTelemetry Tracing
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/observability/otel_trace.rs`

**详细说明**: OpenTelemetry 分布式追踪集成。支持 W3C TraceContext 传播（自动注入/提取 trace headers）。通过 OTLP gRPC 协议导出 trace 到 collector。使用 `BatchSpanProcessor` 批量导出 span。可配置参数：
- `--enable-trace`: 启用追踪
- `--otlp-traces-endpoint`: OTLP collector 地址（默认 `localhost:4317`）

支持 HTTP 和 gRPC 请求的 trace context 注入。

---

### 8.3 Structured Logging
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/observability/logging.rs`

**详细说明**: 结构化日志系统。使用 `tracing` + `tracing-subscriber`。支持功能：
- JSON 格式日志输出（`--json-log`）
- 日志文件输出（`--log-dir`，使用 `tracing-appender`）
- 日志级别控制（`--log-level`：debug/info/warn/error）
- 彩色终端输出
- 与 OpenTelemetry 集成的日志上报

---

### 8.4 InFlight Request Tracker
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/observability/inflight_tracker.rs`

**详细说明**: 实时追踪正在处理的请求数量。通过 `HttpMetricsLayer` 中间件自动记录。当 Prometheus 启用时，启动采样器（每 20ms 采样一次）发布到 gauge_histogram 指标。支持 Gauge Histogram（`src/observability/gauge_histogram.rs`）用于记录并发请求分布。

---

### 8.5 Event System
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/observability/events.rs`

**详细说明**: 结构化事件系统，定义了路由生命周期中的关键事件（如 worker_selected、request_dispatched、response_received 等）。事件通过 tracing span 和 event 机制发布，可被 OpenTelemetry 和日志系统消费。

---

## 9. 中间件 (Middleware)

### 9.1 Authentication (认证中间件)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/middleware.rs`（`auth_middleware`），外部 crate `smg-auth`

**详细说明**: API Key 认证中间件。通过 `Authorization: Bearer <key>` header 验证请求。使用 `subtle::ConstantTimeEq` 进行常量时间比较防止时序攻击。可通过 `--api-key` 设置 API key。不设置时不启用认证。

---

### 9.2 Control Plane Authentication (控制面认证)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/main.rs:525-577`，外部 crate `smg-auth`

**详细说明**: 控制面（管理 API）的高级认证系统。支持两种认证方式：
- **JWT/OIDC**: 通过 `--jwt-issuer`、`--jwt-audience` 配置，支持 JWKS 自动发现，支持角色映射（admin/user）
- **API Key**: 通过 `--control-plane-api-keys` 配置，格式 `id:name:role:key`

支持角色权限控制（Admin/User）和审计日志（`--disable-audit-logging` 可禁用）。

---

### 9.3 Concurrency Limiter (并发限制)
- [ ] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/middleware.rs`（`ConcurrencyLimiter`、`concurrency_limit_middleware`）

**详细说明**: 请求并发数限制和排队机制。当并发请求数达到 `--max-concurrent-requests` 时，新请求进入等待队列。使用 Token Bucket 算法（`src/core/token_bucket.rs`）进行令牌控制。`TokenGuardBody` 确保流式响应在完整发送后才释放令牌。配置参数：
- `--max-concurrent-requests`: 最大并发数（-1 禁用）
- `--queue-size`: 等待队列大小（默认 100）
- `--queue-timeout-secs`: 队列等待超时（默认 60 秒）
- `--rate-limit-tokens-per-second`: Token bucket 填充速率

---

### 9.4 WASM Middleware
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/middleware.rs`（`wasm_middleware`），`src/wasm/`

**详细说明**: WebAssembly 中间件，允许通过 WASM 模块扩展 gateway 的请求/响应处理逻辑。基于 Wasmtime 运行时（v38）和 Component Model。支持在请求处理的不同阶段注入自定义逻辑：
- 请求预处理（修改 headers、body）
- 响应后处理
- 自定义认证/鉴权
- 请求限流

管理 API：
- `POST /wasm`: 上传 WASM 模块
- `DELETE /wasm/{module_uuid}`: 删除 WASM 模块
- `GET /wasm`: 列出所有 WASM 模块

通过 `--enable-wasm` 启用。提供示例模块（`examples/wasm/`）：认证、日志记录、速率限制。

---

### 9.5 Request ID 中间件
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/middleware.rs`（`RequestIdLayer`）

**详细说明**: 请求 ID 追踪中间件。从请求 header 中提取或自动生成请求 ID。支持多种 header 名称（通过 `--request-id-headers` 配置）：默认检查 `x-request-id`、`x-correlation-id`、`x-trace-id`、`request-id`。如果请求中没有 ID 则自动生成 UUID。将请求 ID 传播到响应 header 和日志上下文。

---

### 9.6 CORS (跨域资源共享)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:1133-1156`

**详细说明**: CORS 配置。不指定 `--cors-allowed-origins` 时默认允许所有来源。指定后仅允许列出的来源，限制方法为 GET/POST/OPTIONS，限制 headers 为 Content-Type 和 Authorization。预检请求缓存 3600 秒。

---

### 9.7 HTTP Logging / Tracing Layer
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/middleware.rs`（`create_logging_layer`、`HttpMetricsLayer`）

**详细说明**: HTTP 请求/响应日志和指标中间件。基于 `tower-http` 的 `TraceLayer`。记录请求方法、路径、延迟、状态码等信息到 tracing span。`HttpMetricsLayer` 收集 HTTP 指标到 Prometheus。

---

## 10. MCP (Model Context Protocol) 支持

### 10.1 MCP Server 集成
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:893-912`，外部 crate `smg-mcp`，`src/routers/mcp_utils.rs`

**详细说明**: 支持 MCP（Model Context Protocol）服务器集成，允许模型调用外部工具。通过 `--mcp-config-path` 加载 MCP 配置文件。使用 `rmcp` crate 支持多种 MCP 传输方式：
- SSE（Server-Sent Events）
- Streamable HTTP
- Child process（本地 MCP server）

`McpManager` 管理 MCP 服务器连接，包括 LRU 缓存和后台定期刷新（每 10 分钟）。支持动态注册 MCP 服务器。在 OpenAI Responses API 中实现了完整的多轮 MCP 工具调用循环。

---

## 11. TLS/mTLS 安全

### 11.1 TLS Server 支持
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:1073-1091`

**详细说明**: 服务端 TLS 加密。通过 `--tls-cert-path` 和 `--tls-key-path` 配置证书和私钥。使用 `rustls` 实现，不依赖 OpenSSL。通过 `axum-server` 的 `tls-rustls` feature 提供 HTTPS 服务。

---

### 11.2 mTLS Client 支持
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/config/types.rs:77-82`（`client_identity`、`ca_certificates`）

**详细说明**: 客户端 mTLS（双向 TLS）支持。用于 router 与后端 worker 之间的安全通信。支持配置客户端证书（`client_identity`）和 CA 证书（`ca_certificates`）。用于验证 worker 服务器证书的可信性。

---

## 12. 数据持久化

### 12.1 History Backend (历史存储后端)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `src/config/types.rs:54-64`，外部 crate `data-connector`

**详细说明**: 对话历史和 response 的持久化存储，支持多种后端：
- **memory**: 内存存储（默认，重启丢失）
- **none**: 不存储
- **oracle**: Oracle 数据库（需配置 wallet/DSN、用户名、密码、连接池）
- **postgres**: PostgreSQL 数据库（需配置连接 URL、连接池大小）
- **redis**: Redis 数据库（需配置连接 URL、连接池大小、数据保留天数）

通过 `data-connector` crate 抽象存储接口（`ResponseStorage`、`ConversationStorage`、`ConversationItemStorage`）。

---

## 13. IGW (Inference Gateway) 多模型支持

### 13.1 IGW 模式
- [x] 保留 - [ ] 删除 - [] 待讨论

**代码位置**: `src/routers/router_manager.rs`，CLI `--enable-igw`

**详细说明**: Inference Gateway 模式，支持多模型部署。启用后 `RouterManager` 运行在多路由器模式：
- 自动创建 HTTP Regular、gRPC Regular、gRPC PD、HTTP PD、HTTP OpenAI 等多种路由器
- 通过 `ModelCard` 自动发现 worker 支持的模型
- 根据请求中的 `model` 字段路由到对应的后端
- 支持不同模型使用不同的路由策略

单模型模式下（`enable_igw=false`），`RouterManager` 仅创建一个与配置匹配的路由器。

---

### 13.2 Model Card / Model Type
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/model_card.rs`，`src/core/model_type.rs`

**详细说明**: 模型元数据管理。`ModelCard` 描述一个可用模型的信息，包括模型 ID、支持的端点（chat、generate、embedding、rerank、classify）、provider 类型（SGLang、vLLM、TrtLLM、OpenAI、Anthropic）。`ModelType` 定义了不同的推理端点类型。用于 IGW 模式下的模型发现和请求路由。

---

## 14. 多后端支持

### 14.1 Backend Runtime 支持
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/main.rs:54-79` (`Backend` enum)，`src/core/worker.rs` (`RuntimeType`)

**详细说明**: 支持多种推理引擎后端：
- **SGLang**: 默认后端，支持 HTTP 和 gRPC
- **vLLM**: vLLM 推理引擎
- **TrtLLM**: NVIDIA TensorRT-LLM
- **OpenAI**: OpenAI 兼容 API
- **Anthropic**: Anthropic API

通过 `--backend` 参数选择。不同后端可能支持不同的 API 端点和协议。

---

## 15. Graceful Shutdown (优雅关闭)

### 15.1 优雅关闭机制
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/server.rs:1062-1131`

**详细说明**: 支持优雅关闭。监听 SIGTERM 和 Ctrl+C 信号。收到信号后等待已有请求完成（`--shutdown-grace-period-secs`，默认 180 秒），然后关闭服务。通过 `axum_server::Handle` 实现请求排空。mesh 模式下通过 `/ha/shutdown` API 触发远程优雅关闭。

---

## 16. 语言绑定 (Language Bindings)

### 16.1 Python Binding
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `bindings/python/`

**详细说明**: Python SDK 绑定。提供 `sglang_router` Python 包，可通过 `pip install` 安装。包含：
- `Router`: Rust 实现的高性能路由器的 Python 封装
- `MiniLoadBalancer`: 纯 Python 实现的简易负载均衡器（用于调试）
- `RouterArgs`: 路由器参数配置类
- `launch_router()`: 启动路由器的入口函数
- `launch_server.py`: 启动完整服务的脚本
- CLI 入口（`cli.py`、`__main__.py`）

支持从 Python 端配置和启动路由器，与 SGLang 的 Python 生态集成。

---

### 16.2 Golang Binding (gRPC SDK)
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `bindings/golang/`

**详细说明**: Go 语言 gRPC SDK。提供 OpenAI 风格的 Go API：
- FFI（Foreign Function Interface）层：通过 C ABI 调用 Rust 实现的 gRPC 客户端
- 预处理器（preprocessor）：将 Go 请求转换为 gRPC 请求
- 后处理器（postprocessor）：将 gRPC 响应转换为 Go 结构体
- 批量后处理器（batch_postprocessor）
- gRPC 转换器（grpc_converter）
- 流式支持（stream）
- Tokenizer 集成

提供示例：简单推理、流式推理、OpenAI 兼容服务器。Rust 端实现在 `bindings/golang/src/` 中，导出 C ABI 函数供 Go 调用。

---

## 17. 构建和部署

### 17.1 Docker 构建
- [ ] 保留 - [x] 删除 - [ ] 待讨论

**代码位置**: `docker/Dockerfile_mesh`，`docker/build_mesh.sh`

**详细说明**: Docker 容器化构建脚本和 Dockerfile。提供了标准化的构建流程，构建日志保存在 `docker/logs/` 目录。

---

### 17.2 Makefile 构建系统
-[x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `Makefile`

**详细说明**: Make 构建系统，提供编译、测试、格式化、lint 等目标。

---

### 17.3 E2E 测试框架
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `e2e_test/`

**详细说明**: 端到端测试框架（Python pytest）。包含：
- Chat completion 测试（函数调用、思维链、推理内容、验证）
- Embedding 测试（基本功能、正确性）
- Responses API 测试（CRUD、状态管理、流式事件、结构化输出、工具调用）
- Router 测试（MMLU 准确性、PD 模式 MMLU、Worker API）
- 性能基准测试（PD 性能、常规性能）
- GPU 分配器和监控
- 模型池管理

---

### 17.4 Benchmark 基准测试
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `benches/`

**详细说明**: Criterion 性能基准测试：
- `consistent_hash_bench.rs`: 一致性哈希性能测试
- `wasm_middleware_latency.rs`: WASM 中间件延迟测试
- `request_processing.rs`: 请求处理性能测试
- `router_registry_bench.rs`: 路由器注册表性能测试
- `manual_policy_benchmark.rs`: Manual 策略性能测试
- `tree_benchmark.rs`: 前缀树性能测试

---

## 18. Workflow Engine (工作流引擎)

### 18.1 Worker 初始化工作流
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/core/steps/`（整个目录），外部 crate `wfaas`

**详细说明**: 多步骤工作流引擎，用于管理 worker 的生命周期操作。基于 `wfaas`（Workflow as a Service）crate。工作流步骤包括：

**Worker 发现和注册（local 模式）:**
- `detect_connection.rs`: 检测 worker 连接模式（HTTP/gRPC）
- `discover_metadata.rs`: 从 worker 获取模型元数据
- `discover_dp.rs`: 发现 DP 并行配置
- `create_worker.rs`: 创建 worker 实例
- `submit_tokenizer_job.rs`: 提交 tokenizer 加载任务
- `update_worker_properties.rs`: 更新 worker 属性
- `update_policies_for_worker.rs`: 为 worker 更新路由策略
- `update_remaining_policies.rs`: 更新其余策略

**Worker 外部发现（external 模式）:**
- `discover_models.rs`: 发现外部模型
- `create_workers.rs`: 批量创建 worker

**共享步骤:**
- `activate.rs`: 激活 worker
- `register.rs`: 注册 worker 到 registry
- `update_policies.rs`: 更新策略

**其他工作流:**
- `tokenizer_registration.rs`: Tokenizer 注册工作流
- `mcp_registration.rs`: MCP 服务器注册工作流
- `wasm_module_registration.rs`: WASM 模块注册
- `wasm_module_removal.rs`: WASM 模块移除
- `find_workers_to_remove.rs`、`remove_from_policy_registry.rs`、`remove_from_worker_registry.rs`: Worker 移除工作流

支持 `LoggingSubscriber` 日志订阅。

---

## 19. 版本管理

### 19.1 Version 模块
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/version.rs`，`build.rs`

**详细说明**: 版本信息管理。`build.rs` 在编译时从 `Cargo.toml` 读取版本号和构建时间，通过环境变量注入到二进制文件中。提供运行时版本查询能力。

---

## 20. Header 路由辅助

### 20.1 Header-based Routing
- [] 保留 - [ ] 删除 - [x] 待讨论

**代码位置**: `src/routers/header_utils.rs`

**详细说明**: 基于 HTTP Header 的路由辅助功能。支持的特殊 header：
- `X-SMG-Target-Worker`: 直接指定目标 worker URL
- `X-SMG-Routing-Key`: 用于粘性会话/一致性哈希的路由键
- 认证 header 提取和传递
- Provider 特定 header 的应用

---

## 21. 配置系统

### 21.1 Configuration Builder & Validation
- [x] 保留 - [ ] 删除 - [ ] 待讨论

**代码位置**: `src/config/builder.rs`，`src/config/validation.rs`，`src/config/mod.rs`

**详细说明**: 配置构建和验证系统。`ConfigBuilder` 提供 fluent API 构建 `RouterConfig`。`validation.rs` 验证配置的一致性（如 PD 模式必须同时有 prefill 和 decode URL）。支持从 CLI 参数和环境变量构建配置。

---

## 功能总结

| 类别 | 功能数量 |
|------|---------|
| 路由模式 | 3 |
| 通信协议 | 3 |
| 负载均衡策略 | 9 |
| API 端点 | 14 |
| Mesh/HA | 3 |
| Worker 管理 | 8 |
| K8s 服务发现 | 1 |
| 可观测性 | 5 |
| 中间件 | 7 |
| MCP | 1 |
| TLS/mTLS | 2 |
| 数据持久化 | 1 |
| IGW 多模型 | 2 |
| 多后端 | 1 |
| 优雅关闭 | 1 |
| 语言绑定 | 2 |
| 构建部署 | 4 |
| 工作流引擎 | 1 |
| 版本管理 | 1 |
| Header 路由 | 1 |
| 配置系统 | 1 |
| **总计** | **~71** |
