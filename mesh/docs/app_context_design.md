# AppContext Design

Global dependency container for the MESH router — centralizes all shared resources behind `Arc<AppContext>`.

## Architecture Overview

```mermaid
graph TB
    subgraph AppContext ["Arc&lt;AppContext&gt;"]
        direction TB

        subgraph Infra ["Infrastructure"]
            CLIENT[client: reqwest::Client<br/>Connection pool, timeouts, keepalive]
            CONFIG[router_config: RouterConfig<br/>Immutable config snapshot]
            TRACKER[inflight_tracker: InFlightRequestTracker<br/>Prometheus request counting]
        end

        subgraph Routing ["Routing & Policies"]
            WR[worker_registry: WorkerRegistry<br/>All registered workers]
            PR[policy_registry: PolicyRegistry<br/>Routing strategies]
            RM[router_manager: RouterManager<br/>Route decisions]
            LM[load_monitor: LoadMonitor<br/>Worker load polling]
            RL[rate_limiter: TokenBucket<br/>Concurrency control]
        end

        subgraph Processing ["Request Processing"]
            TR[tokenizer_registry: TokenizerRegistry<br/>Multi-model tokenizers]
            RPF[reasoning_parser_factory<br/>Thinking/answer separation]
            TPF[tool_parser_factory<br/>Tool call extraction]
        end

        subgraph Storage ["Persistence"]
            RS[response_storage<br/>dyn ResponseStorage]
            CS[conversation_storage<br/>dyn ConversationStorage]
            CIS[conversation_item_storage<br/>dyn ConversationItemStorage]
        end

        subgraph ControlPlane ["Control Plane (Lazy Init)"]
            JQ["worker_job_queue<br/>OnceLock&lt;Arc&lt;JobQueue&gt;&gt;"]
            WE["workflow_engines<br/>OnceLock&lt;WorkflowEngines&gt;"]
            WS[worker_service: WorkerService<br/>CRUD orchestration]
        end
    end

    H[HTTP Handlers] -->|State extract| AppContext
    ROUTER[RouterManager] -->|route requests| AppContext
    JQ2[JobQueue Dispatcher] -->|Weak reference| AppContext
    LM2[LoadMonitor Task] -->|poll workers| AppContext
```

## Builder Initialization Sequence

```mermaid
sequenceDiagram
    participant S as startup()
    participant B as AppContextBuilder
    participant AC as AppContext

    S->>B: AppContextBuilder::from_config(router_config, timeout)
    activate B

    B->>B: with_client()<br/>reqwest::Client with pool/timeout/keepalive
    B->>B: maybe_rate_limiter()<br/>TokenBucket if max_concurrent > 0
    B->>B: with_tokenizer_registry()<br/>Empty TokenizerRegistry
    B->>B: with_reasoning_parser_factory()
    B->>B: with_tool_parser_factory()
    B->>B: with_worker_registry()<br/>Empty WorkerRegistry
    B->>B: with_policy_registry()<br/>From config.policy
    B->>B: with_storage()<br/>Memory implementations
    B->>B: with_load_monitor()<br/>Requires client + worker_registry
    B->>B: with_worker_job_queue()<br/>OnceLock::new() (empty)
    B->>B: with_workflow_engines()<br/>OnceLock::new() (empty)

    B->>AC: build()
    Note over AC: Validates all required fields<br/>Creates WorkerService from components

    deactivate B

    Note over S,AC: Post-build lazy initialization

    S->>S: Arc::downgrade(&app_context)
    S->>S: JobQueue::new(config, Weak)
    S->>AC: worker_job_queue.set(job_queue)
    Note over AC: OnceLock filled

    S->>S: WorkflowEngines::new(router_config)
    S->>AC: workflow_engines.set(engines)
    Note over AC: OnceLock filled
```

## Builder Dependency Order

```mermaid
graph LR
    CLIENT[with_client] --> LM[with_load_monitor]
    WR[with_worker_registry] --> LM
    PR[with_policy_registry] --> LM

    CLIENT --> BUILD[build]
    WR --> BUILD
    PR --> BUILD
    LM --> BUILD
    TR[with_tokenizer_registry] --> BUILD
    STORAGE[with_storage] --> BUILD
    JQ[with_worker_job_queue] --> BUILD
    WE[with_workflow_engines] --> BUILD
    CONFIG[router_config] --> BUILD

    BUILD --> WS["WorkerService::new()<br/>(created inside build)"]

    style LM fill:#ffd,stroke:#aa0
    style BUILD fill:#ddf,stroke:#00a
```

## OnceLock Lazy Initialization

```mermaid
graph TD
    subgraph "Chicken-and-Egg Problem"
        AC1["AppContext needs<br/>JobQueue field"] -->|but| JQ1["JobQueue needs<br/>Weak&lt;AppContext&gt;"]
        JQ1 -->|but| AC1
    end

    subgraph "Solution: OnceLock"
        STEP1["1. Create AppContext<br/>worker_job_queue = OnceLock::new()"]
        STEP2["2. Create JobQueue<br/>with Arc::downgrade(&app_context)"]
        STEP3["3. Fill OnceLock<br/>app_context.worker_job_queue.set(queue)"]
        STEP1 --> STEP2 --> STEP3
    end

    subgraph "Same pattern for WorkflowEngines"
        WE1["1. workflow_engines = OnceLock::new()"]
        WE2["2. WorkflowEngines::new(config)"]
        WE3["3. app_context.workflow_engines.set(engines)"]
        WE1 --> WE2 --> WE3
    end
```

## Consumer Map

Which components use which AppContext fields:

```mermaid
graph LR
    subgraph Consumers
        HANDLER[HTTP Handlers]
        ROUTER[RouterManager]
        JOBQ[JobQueue]
        HEALTH[Health Checker]
        LOAD[LoadMonitor]
        WKSVC[WorkerService]
    end

    subgraph Fields
        client
        worker_registry
        policy_registry
        tokenizer_registry
        rate_limiter
        response_storage
        conversation_storage
        conversation_item_storage
        worker_job_queue
        workflow_engines
        inflight_tracker
    end

    HANDLER --> worker_registry
    HANDLER --> conversation_storage
    HANDLER --> conversation_item_storage
    HANDLER --> tokenizer_registry

    ROUTER --> client
    ROUTER --> worker_registry
    ROUTER --> policy_registry
    ROUTER --> response_storage

    JOBQ --> workflow_engines
    JOBQ --> worker_registry
    JOBQ --> worker_job_queue

    HEALTH --> worker_registry
    HEALTH --> client

    LOAD --> worker_registry
    LOAD --> policy_registry
    LOAD --> client

    WKSVC --> worker_registry
    WKSVC --> worker_job_queue
```

## Storage Layer

```mermaid
graph TB
    subgraph "Responses API"
        R1["POST /v1/responses"] --> RS[response_storage]
        R2["GET /v1/responses/:id"] --> RS
        R3["DELETE /v1/responses/:id"] --> RS
    end

    subgraph "Conversations API"
        C1["POST /v1/conversations"] --> CS[conversation_storage]
        C2["GET /v1/conversations/:id"] --> CS
        C3["POST /v1/conversations/:id/items"] --> CIS[conversation_item_storage]
        C4["GET /v1/conversations/:id/items"] --> CIS
    end

    subgraph "Persistence Flow"
        REQ["POST /v1/responses<br/>with conversation_id"] --> PERSIST[persist_conversation_items]
        PERSIST --> RS2[Store response<br/>input + output + metadata]
        PERSIST --> LINK[Link items to conversation]
        LINK --> CIS2[Create + link items<br/>in conversation_item_storage]
    end

    RS -.->|"Arc&lt;dyn ResponseStorage&gt;"| MEM1[MemoryResponseStorage]
    CS -.->|"Arc&lt;dyn ConversationStorage&gt;"| MEM2[MemoryConversationStorage]
    CIS -.->|"Arc&lt;dyn ConversationItemStorage&gt;"| MEM3[MemoryConversationItemStorage]

    style MEM1 fill:#ffe,stroke:#aa0
    style MEM2 fill:#ffe,stroke:#aa0
    style MEM3 fill:#ffe,stroke:#aa0
```

## Rate Limiting

```mermaid
flowchart TD
    REQ[Incoming Request] --> MW[concurrency_limit_middleware]
    MW --> CHECK{rate_limiter<br/>exists?}

    CHECK -->|None| PASS[Pass through directly]

    CHECK -->|Some TokenBucket| TRY{try_acquire<br/>1 token?}
    TRY -->|OK| FORWARD[Forward to worker]
    FORWARD --> BODY[Wrap response in TokenGuardBody]
    BODY -->|"Body fully sent (drop)"| RETURN[return_tokens_sync 1]

    TRY -->|Err bucket empty| QUEUE{queue_size > 0?}
    QUEUE -->|Yes| WAIT[Enter wait queue<br/>QueueProcessor monitors]
    QUEUE -->|No| REJECT[429 Too Many Requests]

    WAIT -->|Token returned| FORWARD
    WAIT -->|Timeout| UNAVAIL[503 Service Unavailable]
```
