# ATOM Mesh

> Forked from [sgl-model-gateway v0.3.2](https://github.com/sgl-project/sglang/tree/main/sgl-model-gateway).

High-performance model routing gateway for PD (Prefill-Decode) disaggregated LLM serving. Routes inference requests across heterogeneous worker fleets with cache-aware load balancing, gRPC pipeline with Rust-native tokenization, and built-in reliability primitives.

## Features

- **PD Disaggregation**: Separate prefill and decode workers with independent routing policies and bootstrap port handling
- **Regular Mode**: Non-disaggregated routing as performance baseline
- **Dual Protocol**: HTTP and gRPC routing with shared reliability layer
- **gRPC Pipeline**: Fully Rust tokenization, reasoning parsing, and tool-call execution for high-throughput serving
- **Load Balancing**: Random, round-robin, cache-aware (prefix tree), power-of-two, prefix-hash strategies with DP-aware scheduling
- **Reliability**: Retries with exponential backoff, per-worker circuit breakers, rate limiting, and request queuing
- **Observability**: 40+ Prometheus metrics, structured logging
- **Multi-Backend**: SGLang worker backend

### Supported Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /v1/chat/completions` | Chat completions with streaming and tool calls |
| `POST /generate` | SGLang generate API |
| `POST /v1/responses` | Background responses with status tracking |
| `POST /v1/embeddings` | Embedding requests (HTTP and gRPC) |
| `POST /v1/tokenize` / `/v1/detokenize` | Tokenization with batch support |
| `POST /parse/reasoning` / `/parse/function_call` | Reasoning and tool-call parsing |
| `GET /health` / `/readiness` / `/liveness` | Health probes |
| `GET /v1/models` | Model info |

## Installation

### Prerequisites

- **Rust and Cargo**
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
  source "$HOME/.cargo/env"
  ```
### Build from Source

```bash
# Debug build
cargo build

# Release build (optimized)
cargo build --release
```

Binaries: `target/release/atom-mesh`, `target/release/mesh`

### Verify

```bash
./target/release/mesh --version
```

## Usage

### Regular HTTP Routing

```bash
mesh launch --worker-urls http://worker1:8000 http://worker2:8000 --policy cache_aware
```

### Prefill/Decode Disaggregation

```bash
mesh launch --pd-disaggregation \
  --prefill http://prefill1:30001 9001 \
  --prefill http://prefill2:30002 \
  --decode http://decode1:30011 \
  --decode http://decode2:30012 \
  --prefill-policy cache_aware --decode-policy power_of_two
```

Prefill entries accept an optional bootstrap port (for Mooncake KV cache transfer).

### gRPC Routing

```bash
mesh launch \
  --worker-urls grpc://worker1:31001 grpc://worker2:31002 \
  --tokenizer-path /path/to/tokenizer.json \
  --reasoning-parser deepseek-r1 \
  --tool-call-parser json
```

Supported reasoning parsers: `deepseek-r1`, `qwen3`, `qwen3-thinking`, `kimi`, `glm45`, `glm47`, `step3`, `minimax`.
Supported tool parsers: `json`, `python`, `xml`.

## Architecture

### Control Plane

- **Worker Registry**: Centralized registration with model-based indexing and consistent hash ring
- **Worker Manager**: Validates workers, discovers capabilities, tracks load
- **Job Queue**: Async add/remove operations with status tracking via `/workers/{id}`
- **Health Checker**: Background probes feeding circuit breakers and policies

### Data Plane

- **HTTP Router**: Regular and PD routing with per-model policy overrides
- **gRPC Router**: Rust-native tokenizer, reasoning parser, and tool parser pipeline
- **Resilience Layer**: Rate limiting, queuing, retries, circuit breakers

### Worker APIs

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/workers` | Register worker (async, returns 202) |
| `GET` | `/workers` | List workers with health and load |
| `GET/PUT/DELETE` | `/workers/{id}` | Inspect, update, or remove worker |
| `POST` | `/flush_cache` | Flush cache across HTTP workers |
| `GET` | `/get_loads` | Sample current worker loads |

## Load Balancing

| Policy | Description |
|--------|-------------|
| `random` | Uniform random selection |
| `round_robin` | Sequential rotation with atomic counters |
| `cache_aware` | Prefix tree matching for cache reuse, with configurable balance thresholds |
| `power_of_two` | Picks lighter worker among two random candidates |
| `prefix_hash` | Consistent prefix hashing |

Per-mode overrides via `--prefill-policy` and `--decode-policy` in PD mode.

## Reliability

- **Retries**: Max 5 with exponential backoff and jitter (`--retry-max-retries`, `--retry-initial-backoff-ms`)
- **Circuit Breakers**: Per-worker with configurable failure/success thresholds (`--cb-failure-threshold`, `--cb-timeout-duration-secs`)
- **Rate Limiting**: Token bucket via `--max-concurrent-requests` with optional request queue (`--queue-size`, `--queue-timeout-secs`)
- **Health Checks**: Configurable interval, timeout, and failure thresholds (`--health-check-interval-secs`)

## Observability

### Prometheus Metrics

Default endpoint: `0.0.0.0:29000` (`--prometheus-host` / `--prometheus-port`)

| Layer | Prefix | Description |
|-------|--------|-------------|
| HTTP | `mesh_http_*` | Request counts, duration, connections, rate limiting |
| Router | `mesh_router_*` | Requests by model/endpoint, latency, errors |
| Inference | `mesh_router_ttft/tpot/tokens_*` | TTFT, TPOT, token counts (gRPC) |
| Worker | `mesh_worker_*` | Pool size, connections, health, selection |
| Circuit Breaker | `mesh_worker_cb_*` | State, transitions, outcomes |
| Retry | `mesh_worker_retries_*` | Attempts, exhausted, backoff |

### Logging

Structured logging via `tracing` with optional file sink (`--log-dir`) and configurable level (`--log-level`).

## Security

Simple API key protection for router endpoints:

```bash
mesh launch --api-key "your-secret-key" \
  --worker-urls http://worker1:8000 http://worker2:8000
```

Clients must provide `Authorization: Bearer <key>`. Workers declared via CLI inherit the router key.

## Source Tree

```
mesh/src/
├── main.rs              # CLI 入口：~50 个 clap 参数，转换为 RouterConfig + ServerConfig
├── lib.rs               # Crate 根：导出所有模块 + 4 个外部 crate 重导出 (protocols/reasoning_parser/tokenizer/tool_parser)
├── server.rs            # Axum HTTP 服务器：路由组装、中间件堆叠、~30 个 handler、启动序列
├── app_context.rs       # 依赖容器 AppContext：HTTP 客户端/配置/注册中心/存储/限流器等，Builder + OnceLock
├── middleware.rs        # 中间件：RequestId / HttpMetrics / TokenBucket 限流 / TokenGuardBody 流式释放
├── version.rs           # 编译时版本常量（git commit/branch/rustc）+ 格式化函数
│
├── config/              # 配置层
│   ├── mod.rs           #   模块入口 + ConfigError / ConfigResult 定义
│   ├── types.rs         #   RouterConfig, RoutingMode, PolicyConfig, RetryConfig 等所有配置结构体
│   ├── builder.rs       #   RouterConfigBuilder：fluent API 构建配置（带默认值）
│   └── validation.rs    #   ConfigValidator：启动前校验所有配置约束
│
├── core/                # 核心抽象层（Worker 生命周期 + 可靠性）
│   ├── mod.rs               #   模块入口 + 常用类型重导出
│   ├── worker.rs            #   Worker trait + BasicWorker + DPAwareWorker：健康状态、负载计数、路由 key
│   ├── worker_builder.rs    #   BasicWorkerBuilder / DPAwareWorkerBuilder：fluent 构造器
│   ├── worker_registry.rs   #   WorkerRegistry：DashMap 存储 + 模型索引 + HashRing 一致性哈希（150 虚拟节点, blake3）
│   ├── worker_service.rs    #   WorkerService：Worker CRUD 业务逻辑层，解耦 HTTP handler
│   ├── worker_manager.rs    #   WorkerManager：fan-out 操作（flush_cache/get_loads/get_metrics）+ LoadMonitor
│   ├── circuit_breaker.rs   #   熔断器：Closed → Open → HalfOpen 状态机，可配置失败/成功阈值
│   ├── retry.rs             #   重试执行器：指数退避 + 抖动计算 + HTTP 状态码可重试判定
│   ├── job_queue.rs         #   异步任务队列：mpsc + Semaphore，分发 Worker/Tokenizer 增删改任务到工作流引擎
│   ├── token_bucket.rs      #   令牌桶限流器：平滑补充 + 突发容量 + Notify 异步排队
│   ├── metrics_aggregator.rs#   多 Worker Prometheus 指标聚合：抓取 + 去重合并
│   ├── error.rs             #   WorkerError 枚举（健康检查失败/网络错误/容量上限/配置问题）
│   └── steps/               #   工作流步骤（wfaas DAG 引擎驱动）
│       ├── mod.rs               #   步骤模块入口
│       ├── workflow_data.rs     #   强类型工作流数据：LocalWorkerData / RemovalData / UpdateData / TokenizerData
│       ├── workflow_engines.rs  #   4 个类型化引擎别名：注册/删除/更新/Tokenizer
│       ├── tokenizer_registration.rs  # Tokenizer 加载步骤：本地路径或 HuggingFace 下载 + 校验 + 缓存
│       └── worker/              #   Worker 生命周期步骤
│           ├── mod.rs               #   重导出所有 local + shared 步骤
│           ├── local/               #   本地 Worker 专用步骤
│           │   ├── mod.rs                   #   聚合导出 + URL 协议剥离工具
│           │   ├── create_worker.rs         #   合并配置和元数据，构建 Worker 对象（basic 或 DP-aware）
│           │   ├── detect_connection.rs     #   探测 Worker 端点，判定连接模式（HTTP/gRPC）
│           │   ├── discover_dp.rs           #   查询 server-info 获取 dp_size 和 model_id
│           │   ├── discover_metadata.rs     #   获取 Worker 元数据（labels、model info）
│           │   ├── find_worker_to_update.rs #   按 URL 查找已注册 Worker，用于更新
│           │   ├── find_workers_to_remove.rs#   按 URL 匹配待移除 Worker
│           │   ├── update_worker_properties.rs  #   用新配置重建 Worker 并替换注册表中旧条目
│           │   ├── update_policies_for_worker.rs#   Worker 属性变更后重建受影响模型的路由策略
│           │   ├── update_remaining_policies.rs #   Worker 移除后重建剩余 Worker 的路由策略
│           │   ├── submit_tokenizer_job.rs      #   提交 AddTokenizer 异步任务到 JobQueue
│           │   ├── remove_from_policy_registry.rs   #   从策略注册中心移除 Worker
│           │   └── remove_from_worker_registry.rs   #   从 Worker 注册中心删除 + 发射移除指标
│           └── shared/              #   共享步骤（local + external 复用）
│               ├── mod.rs               #   导出 + WorkerList 类型别名
│               ├── register.rs          #   RegisterWorkersStep：写入注册中心 + 更新 pool_size 指标
│               ├── update_policies.rs   #   UpdatePoliciesStep：通知策略注册中心 + 初始化 cache-aware + PD 冲突检测
│               └── activate.rs          #   ActivateWorkersStep：标记 Worker 健康，开始接收流量
│
├── policies/            # 负载均衡策略层
│   ├── mod.rs           #   LoadBalancingPolicy trait 定义 + 模块重导出
│   ├── random.rs        #   RandomPolicy：均匀随机选择健康 Worker
│   ├── round_robin.rs   #   RoundRobinPolicy：原子计数器顺序轮询
│   ├── cache_aware.rs   #   CacheAwarePolicy：基数树前缀匹配 + 负载不均衡时回退 shortest-queue
│   ├── power_of_two.rs  #   PowerOfTwoPolicy：随机选两个，路由到负载更低者
│   ├── prefix_hash.rs   #   PrefixHashPolicy：前缀 token 一致性哈希环 + 负载约束回退
│   ├── registry.rs      #   PolicyRegistry：model → policy 动态映射 + PD 模式独立策略
│   ├── factory.rs       #   PolicyFactory：按 PolicyConfig 创建具体策略实例
│   ├── tree.rs          #   并发近似基数树（DashMap 实现），服务 CacheAwarePolicy
│   └── utils.rs         #   PeriodicTask：后台线程 + shutdown flag，用于定期维护（如树淘汰）
│
├── routers/             # 路由层（请求转发到 Worker）
│   ├── mod.rs           #   RouterTrait 定义：route_chat / route_generate / route_responses 等
│   ├── factory.rs       #   RouterFactory：connection_mode × routing_mode → 具体路由实现
│   ├── router_manager.rs#   RouterManager：路由协调 + 请求分发
│   ├── error.rs         #   结构化错误响应（400/404/500 + X-Mesh-Error-Code header）
│   ├── header_utils.rs  #   请求/响应 header 转发 + hop-by-hop 过滤
│   ├── persistence_utils.rs # Response/Conversation 序列化持久化工具
│   ├── http/            #   HTTP 路由实现
│   │   ├── mod.rs           #   模块导出
│   │   ├── router.rs        #   HttpRouter：select_worker → reqwest 转发 → SSE stream + retry
│   │   ├── pd_router.rs     #   HttpPdRouter：分别选 prefill/decode Worker → 双阶段转发
│   │   └── pd_types.rs      #   PD 专用错误类型 + URL 构造工具
│   ├── grpc/            #   gRPC 路由实现
│   │   ├── mod.rs           #   模块导出 + ProcessedMessages 类型
│   │   ├── router.rs        #   GrpcRouter：pipeline 编排 + retry
│   │   ├── pd_router.rs     #   GrpcPdRouter：prefill/decode 双分发管道
│   │   ├── client.rs        #   多态 gRPC 客户端：封装 SGLang/vLLM 后端
│   │   ├── pipeline.rs      #   RequestPipeline：按阶段串联 prep → build → execute → process
│   │   ├── context.rs       #   RequestContext / SharedComponents：请求状态贯穿管道
│   │   ├── proto_wrapper.rs #   统一 protobuf 请求/响应/流枚举包装
│   │   ├── utils.rs         #   流收集、logprob 格式化、tool-call 解析、错误映射
│   │   ├── common/          #   共享 gRPC 组件
│   │   │   ├── mod.rs               #   模块导出
│   │   │   ├── response_collection.rs   #   收集合并 gRPC 流响应（单路/PD 双路）
│   │   │   ├── response_formatting.rs   #   聚合 Usage token 计数
│   │   │   ├── responses/               #   /v1/responses 端点共享逻辑
│   │   │   │   ├── mod.rs               #   导出 ResponsesContext / SSE / persist
│   │   │   │   ├── context.rs           #   ResponsesContext：pipeline + storage + shared 组件
│   │   │   │   ├── handlers.rs          #   GET/Cancel /v1/responses/{id} handler
│   │   │   │   ├── streaming.rs         #   SSE 流：chat completion 事件 → responses 事件格式转换
│   │   │   │   └── utils.rs             #   tool 提取 + response 持久化辅助
│   │   │   └── stages/                  #   共享管道阶段
│   │   │       ├── mod.rs               #   PipelineStage trait + 导出 4 个共享阶段
│   │   │       ├── worker_selection.rs  #   选择 Worker：regular 选 1 个 / PD 选 prefill+decode
│   │   │       ├── client_acquisition.rs#   从已选 Worker 获取 gRPC client handle
│   │   │       ├── dispatch_metadata.rs #   构建路由元数据（model_id、时间戳）
│   │   │       ├── request_execution.rs #   执行 gRPC generate 请求（单路 / PD 双路分发）
│   │   │       └── helpers.rs           #   注入 PD bootstrap 元数据（host/port/room_id）
│   │   └── regular/         #   Regular 模式专用
│   │       ├── mod.rs           #   模块导出
│   │       ├── processor.rs     #   响应处理器：收集 gRPC 流 → chat/generate response 类型
│   │       ├── streaming.rs     #   流式处理器：gRPC token 流 → SSE chat/generate completion 事件
│   │       ├── responses/       #   Regular /v1/responses 实现
│   │       │   ├── mod.rs           #   导出 route_responses 入口
│   │       │   ├── common.rs        #   加载会话历史 + 前序 response 链
│   │       │   ├── conversions.rs   #   ResponsesRequest ↔ ChatCompletionRequest 互转
│   │       │   ├── handlers.rs      #   POST /v1/responses 入口：分发 streaming/non-streaming
│   │       │   ├── non_streaming.rs #   非流式执行：加载历史 → chat pipeline → 转换 → 持久化
│   │       │   └── streaming.rs     #   流式执行：chat SSE → responses 事件 → 持久化
│   │       └── stages/          #   Regular 管道阶段
│   │           ├── mod.rs           #   导出 3 个顶层阶段分发器
│   │           ├── preparation.rs   #   分发器：按请求类型委派 chat/generate preparation
│   │           ├── request_building.rs  #   分发器：委派 chat/generate 的 proto 请求构建
│   │           ├── response_processing.rs #  分发器：委派 streaming/non-streaming 响应处理
│   │           ├── chat/            #   Chat 专用阶段
│   │           │   ├── mod.rs           #   导出 3 个 chat 阶段
│   │           │   ├── preparation.rs   #   过滤 tools、处理 messages、tokenize、构建生成约束
│   │           │   ├── request_building.rs  #   构建 chat proto GenerateRequest（+ PD 元数据注入）
│   │           │   └── response_processing.rs # 流式 → SSE / 非流式 → ChatCompletionResponse
│   │           └── generate/        #   Generate 专用阶段
│   │               ├── mod.rs           #   导出 3 个 generate 阶段
│   │               ├── preparation.rs   #   解析 input_ids、tokenize text、创建 stop-sequence decoder
│   │               ├── request_building.rs  #   构建 generate proto GenerateRequest（+ PD 元数据注入）
│   │               └── response_processing.rs # 流式/非流式收集 → GenerateResponse
│   ├── conversations/   #   /v1/conversations API
│   │   ├── mod.rs           #   模块导出
│   │   └── handlers.rs     #   CRUD handler：list/create/manage conversation items
│   ├── parse/           #   模型输出解析
│   │   ├── mod.rs           #   导出 parse_function_call / parse_reasoning
│   │   └── handlers.rs     #   解析 function-call JSON + 分离 reasoning text
│   └── tokenize/        #   Tokenize 服务
│       ├── mod.rs           #   导出 tokenize/detokenize/管理 handler
│       └── handlers.rs     #   tokenize/detokenize + tokenizer 生命周期管理（add/list/get/remove）
│
└── observability/       # 可观测性层
    ├── mod.rs           #   模块导出
    ├── metrics.rs       #   Prometheus 指标：字符串 interning + counters/gauges/histograms 定义与导出
    ├── events.rs        #   请求事件：RequestSentEvent / RequestPDSentEvent 结构化 tracing debug 日志
    ├── inflight_tracker.rs  #   在飞请求追踪器：按 ID 跟踪 + 按年龄分桶 gauge histogram
    ├── gauge_histogram.rs   #   非累积 gauge histogram：预注册 handle 零分配热路径，Grafana heatmap 适配
    └── logging.rs       #   日志配置：tracing-subscriber + 非阻塞滚动文件 + JSON 格式 + UTC 时间戳
```

## Development

```bash
cargo build                              # build
cargo build --release                    # release build
```
