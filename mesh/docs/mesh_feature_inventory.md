# Mesh Feature Inventory

Complete inventory of all features in `mesh/src/`.

---

## 1. Server & Entry

| Feature | File | Description |
|---------|------|-------------|
| CLI argument parsing | `main.rs` | ~50 clap parameters across 12 groups, manual `--prefill` parsing |
| CLI to config conversion | `main.rs` | `to_router_config()` + `to_server_config()` |
| HTTP server | `server.rs` | Axum-based HTTP server with graceful shutdown |
| Route assembly | `server.rs` `build_app()` | 4 route groups: protected, public, admin, worker |
| Middleware stack assembly | `server.rs` `build_app()` | RequestId → HttpMetrics → Trace → BodyLimits → ConcurrencyLimit |
| 15-step startup sequence | `server.rs` `startup()` | Config → client → registries → router → workers → bind |
| ~30 endpoint handlers | `server.rs` | Thin wrappers calling `state.router.route_*()` |
| AppState | `server.rs` | Holds router, context, concurrency_queue_tx, router_manager |
| AppContext | `app_context.rs` | 17-field dependency container (Data Plane + Control Plane) |
| AppContextBuilder | `app_context.rs` | Builder pattern with strict initialization ordering |
| `from_config()` factory | `app_context.rs` | One-call initialization chain |

## 2. Middleware

| Feature | File | Description |
|---------|------|-------------|
| `RequestIdLayer` | `middleware.rs` | Assigns/propagates `x-request-id` header (pre + post processing) |
| `HttpMetricsLayer` | `middleware.rs` | Tracks `active_connections`, records latency histogram, error code extraction |
| `concurrency_limit_middleware` | `middleware.rs` | TokenBucket rate limiting with optional FIFO queue + timeout |
| `TokenGuardBody` | `middleware.rs` | RAII wrapper: holds rate-limit token until SSE stream ends via `Drop` |
| `extract_error_code_from_response` | `middleware.rs` | Extracts error code from `X-Mesh-Error-Code` header or response body |

## 3. Core: Worker Abstraction

| Feature | File | Description |
|---------|------|-------------|
| `Worker` trait | `core/worker.rs` | Async trait: `url()`, `is_healthy()`, `load()`, `worker_type()`, `circuit_breaker()`, `connection_mode()` |
| `BasicWorker` | `core/worker.rs` | Standard worker with atomic health/load/processed counters |
| `DPAwareWorker` | `core/worker.rs` | Data-parallel aware worker (wraps BasicWorker + dp_rank/dp_size) |
| `WorkerType` enum | `core/worker.rs` | `Regular`, `Prefill { bootstrap_port }`, `Decode` |
| `ConnectionMode` enum | `core/worker.rs` | `Http`, `Grpc { port }` |
| `RuntimeType` enum | `core/worker.rs` | `Sglang`, `Vllm` |
| `WorkerMetadata` | `core/worker.rs` | URL, API key, type, labels, health config, model ID, tokenizer path, parsers |
| `WorkerLoadGuard` | `core/worker.rs` | RAII guard for tracking in-flight requests per worker |
| `WorkerRoutingKeyLoad` | `core/worker.rs` | DashMap-based tracking of active routing keys per worker |
| `HealthChecker` | `core/worker.rs` | Async health check via `/health` endpoint with timeout |
| `HealthConfig` | `core/worker.rs` | Endpoint, timeout, interval, failure/success thresholds |
| `BasicWorkerBuilder` | `core/worker_builder.rs` | Fluent builder for BasicWorker |
| `DPAwareWorkerBuilder` | `core/worker_builder.rs` | Fluent builder for DPAwareWorker |

## 4. Core: Worker Registry

| Feature | File | Description |
|---------|------|-------------|
| `WorkerRegistry` | `core/worker_registry.rs` | Central worker store with DashMap, model-based index (Arc snapshots for lock-free reads) |
| `HashRing` | `core/worker_registry.rs` | Consistent hash ring: blake3 hashing, 150 virtual nodes per worker, O(log n) lookup |
| Model index | `core/worker_registry.rs` | Pre-computed per-model worker lists with immutable Arc snapshots |
| Worker ID management | `core/worker_registry.rs` | UUID-based `WorkerId`, `reserve_id_for_url()` pre-allocation |
| Worker stats | `core/worker_registry.rs` | Total/prefill/decode/regular worker counts |
| URL-to-worker lookup | `core/worker_registry.rs` | DashMap-based URL → Worker mapping |

## 5. Core: Worker Service & Manager

| Feature | File | Description |
|---------|------|-------------|
| `WorkerService` | `core/worker_service.rs` | Business logic: create/list/get/delete/update workers via JobQueue |
| `CreateWorkerResult` | `core/worker_service.rs` | Returns 202 Accepted with worker_id + location |
| `ListWorkersResult` | `core/worker_service.rs` | Returns workers list with prefill/decode/regular stats |
| `WorkerServiceError` | `core/worker_service.rs` | Typed errors: NotFound, InvalidId, QueueNotInitialized, QueueSubmitFailed |
| `WorkerManager` | `core/worker_manager.rs` | Fan-out operations to all workers |
| `flush_cache_all()` | `core/worker_manager.rs` | Fan-out POST /flush_cache to all HTTP workers |
| `get_all_worker_loads()` | `core/worker_manager.rs` | Fan-out GET /get_load to all workers |
| `get_engine_metrics()` | `core/worker_manager.rs` | Fan-out GET /metrics + aggregate |
| `LoadMonitor` | `core/worker_manager.rs` | Periodic background service: fetch worker loads, update PowerOfTwo policies |

## 6. Core: Reliability

| Feature | File | Description |
|---------|------|-------------|
| `CircuitBreaker` | `core/circuit_breaker.rs` | State machine: Closed → Open → HalfOpen → Closed |
| `CircuitBreakerConfig` | `core/circuit_breaker.rs` | failure_threshold, success_threshold, timeout_duration, window_duration |
| `CircuitState` | `core/circuit_breaker.rs` | Atomic state (lock-free reads), per-worker instance |
| `RetryExecutor` | `core/retry.rs` | Generic async retry with exponential backoff + jitter |
| `BackoffCalculator` | `core/retry.rs` | Configurable: initial_backoff, max_backoff, multiplier, jitter_factor |
| `is_retryable_status()` | `core/retry.rs` | 408, 429, 500, 502, 503, 504 |
| `TokenBucket` | `core/token_bucket.rs` | Rate limiting: smooth refill, burst capacity, async acquire, sync return for Drop |

## 7. Core: Control Plane

| Feature | File | Description |
|---------|------|-------------|
| `JobQueue` | `core/job_queue.rs` | Async mpsc queue with Semaphore concurrency control |
| `Job` enum | `core/job_queue.rs` | `AddWorker`, `UpdateWorker`, `RemoveWorker`, `InitializeWorkersFromConfig`, `AddTokenizer`, `RemoveTokenizer` |
| `JobQueueConfig` | `core/job_queue.rs` | Buffer size, max concurrent jobs |
| Job status tracking | `core/job_queue.rs` | Per-URL `JobStatus` (DashMap) |
| `MetricsAggregator` | `core/metrics_aggregator.rs` | Aggregates Prometheus metrics from multiple workers into unified exposition |

## 8. Workflow Steps

| Feature | File | Description |
|---------|------|-------------|
| `WorkflowEngines` | `core/steps/workflow_engines.rs` | 4 typed engines: local_worker, worker_removal, worker_update, tokenizer |
| `CreateLocalWorkerStep` | `core/steps/worker/local/create_worker.rs` | Creates BasicWorker/DPAwareWorker from config |
| `DetectConnectionModeStep` | `core/steps/worker/local/detect_connection.rs` | Probes worker for gRPC support |
| `DiscoverDPInfoStep` | `core/steps/worker/local/discover_dp.rs` | Discovers data parallelism rank/size |
| `DiscoverMetadataStep` | `core/steps/worker/local/discover_metadata.rs` | Fetches model ID, tokenizer path from worker |
| `RegisterWorkersStep` | `core/steps/worker/shared/register.rs` | Registers workers in WorkerRegistry |
| `ActivateWorkersStep` | `core/steps/worker/shared/activate.rs` | Marks workers healthy, starts health checks |
| `UpdatePoliciesForWorkerStep` | `core/steps/worker/local/update_policies_for_worker.rs` | Assigns policy for new worker's model |
| `UpdatePoliciesStep` | `core/steps/worker/shared/update_policies.rs` | Shared policy update logic |
| `UpdateRemainingPoliciesStep` | `core/steps/worker/local/update_remaining_policies.rs` | Updates policies after property changes |
| `SubmitTokenizerJobStep` | `core/steps/worker/local/submit_tokenizer_job.rs` | Submits tokenizer loading job |
| `FindWorkersToRemoveStep` | `core/steps/worker/local/find_workers_to_remove.rs` | Finds workers by URL for removal |
| `RemoveFromPolicyRegistryStep` | `core/steps/worker/local/remove_from_policy_registry.rs` | Cleans up policy mappings |
| `RemoveFromWorkerRegistryStep` | `core/steps/worker/local/remove_from_worker_registry.rs` | Removes from registry |
| `FindWorkerToUpdateStep` | `core/steps/worker/local/find_worker_to_update.rs` | Finds worker for property update |
| `UpdateWorkerPropertiesStep` | `core/steps/worker/local/update_worker_properties.rs` | Updates worker properties |
| `LoadTokenizerStep` | `core/steps/tokenizer_registration.rs` | Loads tokenizer from HuggingFace/local path |

## 9. Routing Policies

| Feature | File | Description |
|---------|------|-------------|
| `LoadBalancingPolicy` trait | `policies/mod.rs` | `select_worker()`, `on_request_complete()`, `update_loads()`, `needs_request_text()` |
| `SelectWorkerInfo` | `policies/mod.rs` | Request text, tokens, headers, hash_ring |
| `RandomPolicy` | `policies/random.rs` | Random selection among healthy workers |
| `RoundRobinPolicy` | `policies/round_robin.rs` | Atomic counter-based round robin |
| `CacheAwarePolicy` | `policies/cache_aware.rs` | Radix-tree prefix matching for KV cache locality |
| `PowerOfTwoPolicy` | `policies/power_of_two.rs` | Pick 2 random, choose lower load |
| `PrefixHashPolicy` | `policies/prefix_hash.rs` | Consistent hash ring on prefix tokens, bounded load walk |
| `PolicyRegistry` | `policies/registry.rs` | Model-to-policy mapping, dynamic assignment, PD prefill/decode policies |
| `PolicyFactory` | `policies/factory.rs` | Creates policy by config or name string |
| Radix tree | `policies/tree.rs` | Prefix tree for CacheAwarePolicy |
| `PeriodicTask` | `policies/utils.rs` | Background periodic task helper |

## 10. Routers

| Feature | File | Description |
|---------|------|-------------|
| `RouterTrait` | `routers/mod.rs` | Unified trait: `route_chat`, `route_generate`, `route_completion`, `route_responses`, `route_tokenize`, `route_detokenize`, etc. |
| `RouterFactory` | `routers/factory.rs` | Creates router by connection_mode x routing_mode (4 combinations) |
| `RouterManager` | `routers/router_manager.rs` | Router coordination, hot-swap, default router tracking |
| HTTP Regular Router | `routers/http/router.rs` | select_worker → reqwest forward → stream, with RetryExecutor |
| HTTP PD Router | `routers/http/pd_router.rs` | Separate prefill/decode worker selection + forwarding |
| PD types | `routers/http/pd_types.rs` | PD-specific request/response types |
| gRPC Regular Router | `routers/grpc/router.rs` | gRPC pipeline: template → tokenize → generate → detokenize → parse |
| gRPC PD Router | `routers/grpc/pd_router.rs` | gRPC PD with prefill/decode worker selection |
| gRPC client | `routers/grpc/client.rs` | gRPC client wrapper |
| gRPC pipeline | `routers/grpc/pipeline.rs` | `RequestPipeline` for gRPC worker selection + execution |
| gRPC context | `routers/grpc/context.rs` | `SharedComponents` (tokenizer, parsers) |
| gRPC proto wrapper | `routers/grpc/proto_wrapper.rs` | Proto message wrappers |
| gRPC utilities | `routers/grpc/utils.rs` | gRPC helper functions |
| gRPC common stages | `routers/grpc/common/stages/` | client_acquisition, dispatch_metadata, worker_selection, request_execution |
| gRPC common responses | `routers/grpc/common/responses/` | Response collection, streaming, formatting |
| gRPC regular stages | `routers/grpc/regular/stages/` | Chat/generate preparation, request building, response processing |
| gRPC regular responses | `routers/grpc/regular/responses/` | Streaming, non-streaming, conversions |
| gRPC regular streaming | `routers/grpc/regular/streaming.rs` | SSE stream construction from gRPC |

## 11. Feature Handlers

| Feature | File | Description |
|---------|------|-------------|
| Conversations CRUD | `routers/conversations/handlers.rs` | Create/list/get/delete conversations + items (OpenAI Responses API compatible) |
| Parse function calls | `routers/parse/handlers.rs` | Extract tool calls from model output via tool_parser |
| Separate reasoning | `routers/parse/handlers.rs` | Extract reasoning content via reasoning_parser |
| Tokenize | `routers/tokenize/handlers.rs` | Encode text → tokens |
| Detokenize | `routers/tokenize/handlers.rs` | Decode tokens → text |
| Count tokens | `routers/tokenize/handlers.rs` | Count tokens without returning them |
| Add/remove tokenizer | `routers/tokenize/handlers.rs` | Dynamic tokenizer management via API |
| List tokenizers | `routers/tokenize/handlers.rs` | List registered tokenizers |
| Persistence utils | `routers/persistence_utils.rs` | Response/conversation item serialization, input item storage |
| Header utils | `routers/header_utils.rs` | Request header copy, response header forwarding, hop-by-hop filtering |
| Error helpers | `routers/error.rs` | Structured error responses: `internal_error`, `bad_request`, `not_found`, `service_unavailable` |

## 12. Observability

| Feature | File | Description |
|---------|------|-------------|
| Prometheus metrics | `observability/metrics.rs` | Request latency, active connections, worker health, queue depth, TTFT, token counts, circuit breaker state |
| String interning | `observability/metrics.rs` | DashMap-based interning for metric labels (avoid repeated heap allocs) |
| Metrics server | `observability/metrics.rs` | `PrometheusBuilder` with configurable bucket boundaries |
| Request events | `observability/events.rs` | `RequestSentEvent`, `RequestPDSentEvent`, `RequestReceivedEvent` |
| InFlight tracker | `observability/inflight_tracker.rs` | Tracks request ages, periodic sampling to gauge histogram |
| Gauge histogram | `observability/gauge_histogram.rs` | Custom gauge-histogram metric type with configurable buckets |
| Logging | `observability/logging.rs` | tracing-subscriber with rolling file appender, JSON format option, log level filtering |

## 13. Configuration

| Feature | File | Description |
|---------|------|-------------|
| `RouterConfig` | `config/types.rs` | Main config: mode, policy, host/port, timeouts, rate limits, retry, circuit breaker, health check, tokenizer |
| `RoutingMode` | `config/types.rs` | `Regular { worker_urls }`, `PrefillDecode { prefill_urls, decode_urls, policies }` |
| `PolicyConfig` | `config/types.rs` | Enum: Random, RoundRobin, CacheAware, PowerOfTwo, PrefixHash |
| `RetryConfig` | `config/types.rs` | max_retries, backoff, multiplier, jitter |
| `CircuitBreakerConfig` | `config/types.rs` | failure/success thresholds, timeout, window |
| `HealthCheckConfig` | `config/types.rs` | Endpoint, timeout, interval, thresholds |
| `MetricsConfig` | `config/types.rs` | Prometheus metrics server config |
| `TokenizerCacheConfig` | `config/types.rs` | L0 (exact match) + L1 (prefix match) cache settings |
| Config builder | `config/builder.rs` | Builder with defaults for RouterConfig |
| Config validation | `config/validation.rs` | Validation rules for config consistency |
| `ConfigError` | `config/mod.rs` | ValidationFailed, InvalidValue, IncompatibleConfig, MissingRequired |

## 14. External Crate Re-exports

| Re-export | Crate | Description |
|-----------|-------|-------------|
| `protocols` | `openai_protocol` | OpenAI-compatible request/response types (ChatCompletionRequest, etc.) |
| `tokenizer` | `llm_tokenizer` | Tokenizer trait + HuggingFace/local implementations |
| `reasoning_parser` | `reasoning_parser` | Reasoning content extraction (deepseek-r1, qwen3) |
| `tool_parser` | `tool_parser` | Tool/function call parsing |
