# Three-Layer Architecture Design

Entrypoints / Mesh / Worker three-layer decoupled design.

---

## 1. Overall Architecture

```mermaid
flowchart TB
    CLIENT([Client]) --> EP

    subgraph EP ["Entrypoints Layer"]
        direction TB
        SERVER["HTTP Server (Axum)"]
        MW["Middleware<br/>rate limit | metrics | tracing | request-id"]
        HANDLERS["Endpoint Handlers<br/>/v1/chat/completions<br/>/v1/completions<br/>/v1/responses<br/>/v1/tokenize | /health"]
        BACKEND_TRAIT{{"Backend trait"}}
        SERVER --> MW --> HANDLERS --> BACKEND_TRAIT
    end

    subgraph MESH ["Mesh Layer"]
        direction TB
        MESH_BACKEND["MeshBackend"]
        ROUTER["Router Engine<br/>HTTP / gRPC x Regular / PD"]
        POLICY["Policy Engine<br/>random | round_robin | cache_aware<br/>power_of_two | prefix_hash"]
        CORE["Worker Management<br/>registry | lifecycle | health check"]
        RELIABLE["Reliability<br/>circuit breaker | retry | backoff"]
        MESH_BACKEND --> ROUTER
        ROUTER --> POLICY
        ROUTER --> CORE
        ROUTER --> RELIABLE
    end

    subgraph WORKER ["Worker Layer"]
        direction LR
        W1["Worker 1<br/>SGLang"]
        W2["Worker 2<br/>ATOM"]
        W3["Worker N<br/>vLLM"]
    end

    BACKEND_TRAIT -->|PD / multi-worker| MESH_BACKEND
    BACKEND_TRAIT -.->|"standalone<br/>(direct call)"| ATOM_ENGINE["ATOM Engine<br/>(in-process)"]
    MESH --> |"HTTP / gRPC"| WORKER

    style EP fill:#e3f2fd,stroke:#1565c0
    style MESH fill:#fff8e1,stroke:#f9a825
    style WORKER fill:#e8f5e9,stroke:#2e7d32
    style BACKEND_TRAIT fill:#f3e5f5,stroke:#7b1fa2
```

---

## 2. Standalone vs PD Mode Dataflow

```mermaid
flowchart LR
    subgraph Standalone ["Standalone Mode"]
        direction LR
        C1([Client]) --> EP1["Entrypoints<br/>HTTP + Middleware"]
        EP1 --> SB["StandaloneBackend"]
        SB --> ENGINE["ATOM Engine<br/>(in-process)"]
    end

    subgraph PD ["PD / Multi-Worker Mode"]
        direction LR
        C2([Client]) --> EP2["Entrypoints<br/>HTTP + Middleware"]
        EP2 --> MB["MeshBackend"]
        MB --> MESH2["Mesh<br/>Router + Policy"]
        MESH2 --> WP["Prefill Worker"]
        MESH2 --> WD["Decode Worker"]
    end

    style Standalone fill:#e8f5e9,stroke:#2e7d32
    style PD fill:#fff8e1,stroke:#f9a825
```

---

## 3. Entrypoints Layer

```mermaid
flowchart TB
    REQ([Client Request]) --> RID["RequestIdLayer<br/>assign / propagate x-request-id"]
    RID --> MET["HttpMetricsLayer<br/>active_connections++<br/>start timer"]
    MET --> TRC["TraceLayer<br/>create span"]
    TRC --> BODY["BodyLimits<br/>max payload check"]
    BODY --> RL{"Rate Limiter<br/>try_acquire token?"}

    RL -->|OK| ROUTE["Route Dispatch"]
    RL -->|Fail| Q{"Queue<br/>enabled?"}
    Q -->|No| R429([429 Too Many Requests])
    Q -->|Yes| WAIT{"Wait<br/>timeout?"}
    WAIT -->|Got token| ROUTE
    WAIT -->|Timeout| R408([408 Timeout])

    ROUTE --> H_CHAT["/v1/chat/completions"]
    ROUTE --> H_COMP["/v1/completions"]
    ROUTE --> H_RESP["/v1/responses"]
    ROUTE --> H_TOK["/v1/tokenize"]
    ROUTE --> H_HEALTH["/health"]
    ROUTE --> H_MODELS["/v1/models"]
    ROUTE --> H_ADMIN["admin endpoints"]

    H_CHAT & H_COMP & H_RESP & H_TOK --> BACKEND{{"backend.call()"}}

    BACKEND -->|standalone| DIRECT["ATOM Engine"]
    BACKEND -->|mesh| MESH_CALL["MeshBackend"]

    style RL fill:#fff9c4,stroke:#f9a825
    style Q fill:#fff9c4,stroke:#f9a825
    style WAIT fill:#fff9c4,stroke:#f9a825
    style BACKEND fill:#f3e5f5,stroke:#7b1fa2
    style R429 fill:#ffcdd2,stroke:#c62828
    style R408 fill:#ffcdd2,stroke:#c62828
```

### Entrypoints Layer Features

```mermaid
flowchart LR
    subgraph Middleware ["Middleware Stack"]
        direction TB
        A1["RequestIdLayer"]
        A2["HttpMetricsLayer"]
        A3["TraceLayer"]
        A4["BodyLimits"]
        A5["ConcurrencyLimit + TokenGuardBody"]
    end

    subgraph Endpoints ["Endpoint Handlers"]
        direction TB
        B1["/v1/chat/completions"]
        B2["/v1/completions"]
        B3["/v1/responses"]
        B4["/v1/tokenize"]
        B5["/v1/detokenize"]
        B6["/v1/models"]
        B7["/health + /health/ready"]
        B8["/metrics"]
    end

    subgraph Infra ["Infrastructure"]
        direction TB
        C1["TokenBucket<br/>rate limit + refill"]
        C2["InFlightTracker<br/>request age histogram"]
        C3["Logging<br/>tracing + rolling file"]
        C4["Prometheus<br/>HTTP-level metrics"]
        C5["Error Helpers<br/>structured error responses"]
        C6["Header Utils<br/>forwarding + hop-by-hop filter"]
    end

    subgraph BackendDef ["Backend Trait"]
        direction TB
        D1["chat_completion()"]
        D2["completion()"]
        D3["generate()"]
        D4["tokenize()"]
        D5["health()"]
        D6["models()"]
    end

    Middleware --> Endpoints
    Endpoints --> BackendDef
    Infra -.-> Middleware

    style Middleware fill:#e3f2fd,stroke:#1565c0
    style Endpoints fill:#e3f2fd,stroke:#1565c0
    style Infra fill:#e3f2fd,stroke:#1565c0
    style BackendDef fill:#f3e5f5,stroke:#7b1fa2
```

---

## 4. Mesh Layer

### 4.1 Request Routing Flow

```mermaid
flowchart TB
    IN([MeshBackend.chat_completion]) --> MODEL["Extract model_id"]
    MODEL --> GET_WORKERS["WorkerRegistry<br/>get workers for model"]
    GET_WORKERS --> SELECT["PolicyRegistry<br/>get policy for model"]

    SELECT --> POLICY{"Policy<br/>select_worker()"}
    POLICY --> W_HEALTHY["Filter healthy workers<br/>+ circuit_breaker.can_execute()"]
    W_HEALTHY --> CHOSEN["Selected Worker"]

    CHOSEN --> CB_CHECK{"Circuit Breaker<br/>state?"}
    CB_CHECK -->|Closed / HalfOpen| FORWARD["Forward Request<br/>HTTP reqwest / gRPC client"]
    CB_CHECK -->|Open| RETRY_OR_FAIL["Try next worker"]

    FORWARD --> RESULT{"Response<br/>status?"}
    RESULT -->|Success| CB_SUCCESS["CB: record_success()"]
    RESULT -->|Retryable error| RETRY{"RetryExecutor<br/>attempts left?"}
    RESULT -->|Fatal error| RETURN_ERR([Error Response])

    RETRY -->|Yes| BACKOFF["Backoff<br/>exponential + jitter"] --> POLICY
    RETRY -->|No| RETURN_ERR

    CB_SUCCESS --> STREAM["Stream Response<br/>SSE / non-streaming"]
    STREAM --> DONE([Response to Client])

    style POLICY fill:#fff9c4,stroke:#f9a825
    style CB_CHECK fill:#fff9c4,stroke:#f9a825
    style RETRY fill:#fff9c4,stroke:#f9a825
    style RETURN_ERR fill:#ffcdd2,stroke:#c62828
```

### 4.2 PD Routing Flow

```mermaid
flowchart TB
    IN([PD Chat Request]) --> PREFILL_SELECT["PolicyRegistry<br/>prefill_policy.select_worker()"]
    IN --> DECODE_SELECT["PolicyRegistry<br/>decode_policy.select_worker()"]

    PREFILL_SELECT --> PF["Prefill Worker"]
    DECODE_SELECT --> DF["Decode Worker"]

    PF --> SEND_PF["Forward to Prefill<br/>+ bootstrap info"]
    DF --> SEND_DF["Forward to Decode<br/>with prefill context"]

    SEND_PF --> PF_RESULT{"Prefill<br/>result?"}
    PF_RESULT -->|OK| WAIT_DECODE["Wait for Decode stream"]
    PF_RESULT -->|Fail| RETRY_PF["Retry with<br/>different prefill worker"]

    SEND_DF --> WAIT_DECODE
    WAIT_DECODE --> STREAM([Stream Response])

    style PREFILL_SELECT fill:#e8f5e9,stroke:#2e7d32
    style DECODE_SELECT fill:#e3f2fd,stroke:#1565c0
```

### 4.3 Policy Engine

```mermaid
flowchart TB
    subgraph PolicyEngine ["Policy Engine"]
        direction TB

        subgraph Policies ["5 Load Balancing Policies"]
            direction LR
            P1["Random<br/>simple random pick"]
            P2["RoundRobin<br/>atomic counter"]
            P3["CacheAware<br/>radix tree prefix match<br/>for KV cache locality"]
            P4["PowerOfTwo<br/>pick 2, choose lower load"]
            P5["PrefixHash<br/>consistent hash ring<br/>on prefix tokens"]
        end

        REG["PolicyRegistry<br/>model -> policy mapping"]
        FAC["PolicyFactory<br/>create by config or name"]

        FAC -->|creates| Policies
        REG -->|stores| Policies
    end

    subgraph PDPolicies ["PD Mode Policies"]
        direction LR
        PP["Prefill Policy<br/>(independent)"]
        DP["Decode Policy<br/>(independent)"]
    end

    REG -->|PD mode| PDPolicies

    style PolicyEngine fill:#fff8e1,stroke:#f9a825
    style PDPolicies fill:#fff8e1,stroke:#f9a825
```

### 4.4 Worker Management

```mermaid
flowchart TB
    subgraph WorkerMgmt ["Worker Management"]
        direction TB

        subgraph Registry ["Worker Registry"]
            direction LR
            WR["WorkerRegistry<br/>DashMap + Arc snapshots"]
            HR["HashRing<br/>blake3 x 150 vnodes"]
            MI["Model Index<br/>per-model worker lists"]
        end

        subgraph Lifecycle ["Worker Lifecycle"]
            direction TB
            JQ["JobQueue<br/>async mpsc + semaphore"]
            WF["WorkflowEngines<br/>4 typed engines"]

            JQ --> |AddWorker| ADD["Registration Workflow"]
            JQ --> |RemoveWorker| REM["Removal Workflow"]
            JQ --> |UpdateWorker| UPD["Update Workflow"]
            JQ --> |AddTokenizer| TOK["Tokenizer Workflow"]
        end

        subgraph Monitoring ["Health & Load"]
            direction LR
            HC["Health Checker<br/>periodic /health probe"]
            LM["LoadMonitor<br/>periodic /get_load fetch"]
            CB["CircuitBreaker<br/>per-worker state machine"]
        end
    end

    ADD --> WR
    REM --> WR
    UPD --> WR
    LM -->|update loads| P4_ref["PowerOfTwo policies"]
    HC -->|mark unhealthy| WR

    style WorkerMgmt fill:#fff8e1,stroke:#f9a825
```

### 4.5 Worker Registration Workflow

```mermaid
flowchart LR
    A["CreateLocalWorker<br/>build BasicWorker"] --> B["DetectConnection<br/>HTTP or gRPC?"]
    B --> C["DiscoverDP<br/>dp_rank / dp_size"]
    C --> D["DiscoverMetadata<br/>model_id / tokenizer"]
    D --> E["Register<br/>add to WorkerRegistry"]
    E --> F["UpdatePolicies<br/>assign policy for model"]
    F --> G["SubmitTokenizerJob<br/>load tokenizer async"]
    G --> H["Activate<br/>mark healthy, start health check"]

    style A fill:#e8f5e9,stroke:#2e7d32
    style H fill:#e8f5e9,stroke:#2e7d32
```

### 4.6 Router Implementations

```mermaid
flowchart TB
    subgraph RouterMatrix ["Router Factory: connection_mode x routing_mode"]
        direction TB

        subgraph HTTP_Routers ["HTTP Routers"]
            direction LR
            HR_REG["HTTP Regular<br/>select worker -> reqwest forward<br/>-> SSE stream"]
            HR_PD["HTTP PD<br/>select prefill + decode<br/>-> concurrent forward"]
        end

        subgraph GRPC_Routers ["gRPC Routers"]
            direction LR
            GR_REG["gRPC Regular<br/>chat template -> tokenize<br/>-> generate -> detokenize<br/>-> parse reasoning/tools"]
            GR_PD["gRPC PD<br/>prefill + decode<br/>gRPC pipeline"]
        end
    end

    TRAIT["RouterTrait<br/>route_chat() | route_generate()<br/>route_completion() | route_responses()"]
    TRAIT --> RouterMatrix

    style RouterMatrix fill:#fff8e1,stroke:#f9a825
    style TRAIT fill:#f3e5f5,stroke:#7b1fa2
```

### 4.7 Observability (Mesh-Level)

```mermaid
flowchart LR
    subgraph MeshObs ["Mesh Observability"]
        direction TB
        M1["Worker Health Metrics<br/>healthy / unhealthy per worker"]
        M2["Circuit Breaker Metrics<br/>state per worker"]
        M3["Routing Metrics<br/>policy used, worker selected"]
        M4["Request Events<br/>PD sent, request sent"]
        M5["Metrics Aggregator<br/>collect from all workers<br/>-> unified Prometheus"]
        M6["LoadMonitor<br/>periodic load sampling"]
    end

    style MeshObs fill:#fff8e1,stroke:#f9a825
```

---

## 5. Worker Layer

```mermaid
flowchart TB
    subgraph WorkerLayer ["Worker Layer (separate processes)"]
        direction TB

        subgraph SGLang ["SGLang Runtime"]
            S1["HTTP API<br/>/v1/chat/completions<br/>/generate<br/>/health<br/>/get_load"]
            S2["gRPC API<br/>GenerateService"]
            S3["Inference Engine"]
            S1 & S2 --> S3
        end

        subgraph ATOM ["ATOM Runtime"]
            A1["FastAPI Server<br/>OpenAI-compatible"]
            A2["LLMEngine<br/>CoreManager -> EngineCore"]
            A1 --> A2
        end

        subgraph VLLM ["vLLM Runtime"]
            V1["HTTP API"]
            V2["vLLM Engine"]
            V1 --> V2
        end
    end

    MESH_IN(["Mesh Layer"]) -->|HTTP / gRPC| SGLang
    MESH_IN -->|HTTP| ATOM
    MESH_IN -->|HTTP| VLLM

    style WorkerLayer fill:#e8f5e9,stroke:#2e7d32
```

---

## 6. Feature-to-Layer Mapping

```mermaid
flowchart TB
    subgraph EP_Features ["Entrypoints Layer"]
        direction TB
        E1["HTTP Server (Axum)"]
        E2["RequestIdLayer"]
        E3["HttpMetricsLayer"]
        E4["ConcurrencyLimit + TokenGuardBody"]
        E5["TokenBucket"]
        E6["InFlightTracker"]
        E7["Logging"]
        E8["Endpoint Handlers"]
        E9["Error / Header Utils"]
        E10["Backend Trait"]
        E11["Prometheus HTTP Metrics"]
    end

    subgraph MESH_Features ["Mesh Layer"]
        direction TB
        M1["RouterTrait + 4 implementations"]
        M2["RouterFactory + RouterManager"]
        M3["5 Routing Policies + Registry"]
        M4["Worker Trait + Registry + HashRing"]
        M5["WorkerService + WorkerManager"]
        M6["CircuitBreaker + RetryExecutor"]
        M7["JobQueue + WorkflowEngines"]
        M8["8 Workflow Steps"]
        M9["LoadMonitor"]
        M10["MetricsAggregator"]
        M11["gRPC Client + Pipeline"]
        M12["AppContext"]
        M13["RouterConfig + PolicyConfig"]
    end

    subgraph SHARED_Features ["Shared / Cross-Layer"]
        direction TB
        S1["Tokenizer Registry + Handlers"]
        S2["Conversations API + Storage"]
        S3["Parse Handlers (reasoning / tools)"]
        S4["Response Storage"]
        S5["Protocol Types (openai_protocol)"]
    end

    subgraph WORKER_Features ["Worker Layer"]
        direction TB
        W1["SGLang Server"]
        W2["ATOM Engine"]
        W3["vLLM Server"]
    end

    style EP_Features fill:#e3f2fd,stroke:#1565c0
    style MESH_Features fill:#fff8e1,stroke:#f9a825
    style SHARED_Features fill:#f3e5f5,stroke:#7b1fa2
    style WORKER_Features fill:#e8f5e9,stroke:#2e7d32
```

---

## 7. Backend Trait Design

```mermaid
classDiagram
    class Backend {
        <<trait>>
        +chat_completion(req, headers) Response
        +completion(req, headers) Response
        +generate(req, headers) Response
        +tokenize(req) Response
        +detokenize(req) Response
        +health() Response
        +models() Response
        +responses_create(req) Response
        +responses_get(id) Response
    }

    class StandaloneBackend {
        -engine: ATOMEngine
        +chat_completion(req, headers) Response
        +health() Response
    }

    class MeshBackend {
        -router: Arc~dyn RouterTrait~
        -app_context: Arc~AppContext~
        +chat_completion(req, headers) Response
        +health() Response
    }

    Backend <|.. StandaloneBackend : implements
    Backend <|.. MeshBackend : implements

    MeshBackend --> RouterTrait : delegates to
    StandaloneBackend --> ATOMEngine : calls directly
```

---

## 8. Migration Phases

```mermaid
flowchart LR
    subgraph Phase1 ["Phase 1: Extract Middleware"]
        direction TB
        P1A["Move TokenBucket to shared crate"]
        P1B["Move RequestId / HttpMetrics / TraceLayer"]
        P1C["Move InFlightTracker + Logging"]
        P1D["Move Error / Header Utils"]
    end

    subgraph Phase2 ["Phase 2: Define Backend Trait"]
        direction TB
        P2A["Define Backend trait in entrypoints"]
        P2B["Implement MeshBackend wrapping RouterTrait"]
        P2C["Implement StandaloneBackend for ATOM"]
    end

    subgraph Phase3 ["Phase 3: Split Server"]
        direction TB
        P3A["Move endpoint handlers to entrypoints"]
        P3B["Move build_app() to entrypoints"]
        P3C["AppState holds Arc dyn Backend"]
        P3D["Mesh becomes library, not binary"]
    end

    subgraph Phase4 ["Phase 4: Clean Boundary"]
        direction TB
        P4A["Mesh exposes MeshBackend::new()"]
        P4B["Remove HTTP server code from mesh"]
        P4C["Entrypoints chooses backend by mode"]
    end

    Phase1 --> Phase2 --> Phase3 --> Phase4

    style Phase1 fill:#e3f2fd,stroke:#1565c0
    style Phase2 fill:#f3e5f5,stroke:#7b1fa2
    style Phase3 fill:#fff8e1,stroke:#f9a825
    style Phase4 fill:#e8f5e9,stroke:#2e7d32
```

---

## 9. Target Crate Structure

```mermaid
flowchart TB
    subgraph Crates ["Crate Dependencies"]
        direction TB

        EP_CRATE["entrypoints crate<br/>HTTP server + middleware + handlers"]
        MESH_CRATE["mesh crate<br/>routing + policies + worker mgmt"]
        PROTO_CRATE["openai_protocol crate<br/>request / response types"]
        TOK_CRATE["llm_tokenizer crate<br/>tokenizer trait + impls"]
        PARSER_CRATE["reasoning_parser + tool_parser"]
        GRPC_CRATE["mesh_grpc crate<br/>proto definitions"]

        EP_CRATE -->|"optional dep<br/>(PD mode only)"| MESH_CRATE
        EP_CRATE --> PROTO_CRATE
        EP_CRATE --> TOK_CRATE
        MESH_CRATE --> PROTO_CRATE
        MESH_CRATE --> TOK_CRATE
        MESH_CRATE --> PARSER_CRATE
        MESH_CRATE --> GRPC_CRATE
    end

    style EP_CRATE fill:#e3f2fd,stroke:#1565c0
    style MESH_CRATE fill:#fff8e1,stroke:#f9a825
    style PROTO_CRATE fill:#f3e5f5,stroke:#7b1fa2
    style TOK_CRATE fill:#f3e5f5,stroke:#7b1fa2
    style PARSER_CRATE fill:#f3e5f5,stroke:#7b1fa2
    style GRPC_CRATE fill:#f3e5f5,stroke:#7b1fa2
```
