# Local Worker Registration

Detailed design of the local worker registration workflow — how MESH discovers, connects to, and registers locally deployed inference engines (ATOM, vLLM, SGLang).

## Overview

When a worker URL is submitted (via startup config or REST API), the system must:
1. Wait for the engine to be ready
2. Detect whether it speaks HTTP or gRPC
3. Pull metadata (model_id, capabilities)
4. Handle Data Parallel (DP) — one URL may have multiple DP ranks behind it
5. Create typed Worker objects
6. Register them in the routing infrastructure
7. Load the tokenizer for the model

## Full Registration Flow

```mermaid
sequenceDiagram
    participant USER as User / startup()
    participant SVC as WorkerService
    participant JQ as JobQueue
    participant WF as LocalWorkerEngine
    participant WORKER as Inference Engine<br/>(ATOM/vLLM/SGLang)
    participant REG as WorkerRegistry
    participant POL as PolicyRegistry

    USER->>SVC: POST /workers {url: "http://10.0.0.1:8000"}
    SVC->>REG: reserve_id_for_url() → worker_id
    SVC->>JQ: submit(Job::AddWorker)
    SVC-->>USER: 202 Accepted {worker_id, location}

    Note over JQ: Dispatcher dequeues job,<br/>acquires semaphore permit

    JQ->>WF: start_workflow("local_worker_registration", data)
    activate WF

    rect rgb(255, 245, 220)
        Note over WF,WORKER: Step 1: Detect Connection Mode
        loop Retry with linear backoff (1s→5s)
            WF->>WORKER: HTTP GET /health
            alt HTTP responds
                WORKER-->>WF: 200 OK
                Note over WF: ConnectionMode::Http
            else HTTP fails, try gRPC
                WF->>WORKER: gRPC health check
                alt gRPC responds
                    WORKER-->>WF: SERVING
                    Note over WF: ConnectionMode::Grpc
                else Both fail
                    Note over WF: Retry (engine still starting)
                end
            end
        end
    end

    rect rgb(220, 245, 255)
        Note over WF,WORKER: Step 2: Discover Metadata
        WF->>WORKER: GET /get_server_info (HTTP)<br/>or gRPC ServerInfo
        WORKER-->>WF: model_id, max_model_len,<br/>tp_size, runtime_type, etc.
        Note over WF: Store in discovered_labels
    end

    rect rgb(220, 245, 255)
        Note over WF,WORKER: Step 3: Discover DP Info
        WF->>WORKER: GET /get_server_info<br/>(check dp_size field)
        WORKER-->>WF: dp_size, rank_urls[]
        Note over WF: If dp_size > 1:<br/>one URL = multiple workers
    end

    rect rgb(220, 255, 220)
        Note over WF: Step 4: Create Worker Objects
        WF->>WF: BasicWorkerBuilder<br/>.url(rank_url)<br/>.model_id(discovered)<br/>.connection_mode(detected)<br/>.worker_type(from config)<br/>.build()
        Note over WF: Creates N workers<br/>(1 per DP rank)
    end

    rect rgb(220, 255, 220)
        Note over WF,REG: Step 5: Register Workers
        WF->>REG: register(worker) for each
        REG-->>WF: WorkerId assigned
    end

    par Parallel final steps
        rect rgb(220, 220, 255)
            Note over WF,JQ: Step 6a: Submit Tokenizer Job
            WF->>JQ: submit(Job::AddTokenizer)<br/>for discovered model_id
        end

        rect rgb(220, 220, 255)
            Note over WF,POL: Step 6b: Update Policies
            WF->>POL: add worker to routing tables
        end

        rect rgb(220, 220, 255)
            Note over WF: Step 6c: Activate Workers
            WF->>WF: worker.set_healthy(true)
            Note over WF: Now eligible for traffic
        end
    end

    deactivate WF
    JQ->>JQ: record_job_completion(Ok)
    Note over JQ: Drop semaphore permit
```

## Step Details

### Step 1: Detect Connection Mode

```mermaid
flowchart TD
    START[Worker URL submitted] --> HTTP_TRY["Try HTTP GET /health"]

    HTTP_TRY --> HTTP_OK{HTTP 200?}
    HTTP_OK -->|Yes| SET_HTTP["ConnectionMode::Http"]
    HTTP_OK -->|No| GRPC_TRY["Try gRPC health.check"]

    GRPC_TRY --> GRPC_OK{gRPC SERVING?}
    GRPC_OK -->|Yes| SET_GRPC["ConnectionMode::Grpc"]
    GRPC_OK -->|No| RETRY{Retries left?}

    RETRY -->|Yes| WAIT["Backoff: Linear 1s → 5s"] --> HTTP_TRY
    RETRY -->|No| FAIL["FailWorkflow<br/>Worker unreachable"]

    style SET_HTTP fill:#dfd
    style SET_GRPC fill:#dfd
    style FAIL fill:#fdd
```

**Why retry?** The inference engine may still be loading the model when MESH starts. The retry loop (with configurable `worker_startup_timeout_secs`) waits until the engine is ready.

### Step 2: Discover Metadata

```mermaid
flowchart TD
    MODE{ConnectionMode?}

    MODE -->|Http| HTTP_INFO["GET /get_server_info"]
    MODE -->|Grpc| GRPC_INFO["gRPC GetServerInfo()"]

    HTTP_INFO --> PARSE["Parse response JSON"]
    GRPC_INFO --> PARSE

    PARSE --> EXTRACT["Extract fields:<br/>- model_id<br/>- max_model_len<br/>- tp_size<br/>- runtime_type (sglang/vllm)<br/>- chat_template<br/>- tokenizer_path"]

    EXTRACT --> LABELS["Store in discovered_labels HashMap"]

    LABELS --> DONE["ContinueNextStep even on failure<br/>(metadata is best-effort)"]

    style DONE fill:#ffe
```

**FailureAction: ContinueNextStep** — If the engine doesn't support `/get_server_info`, registration continues with whatever the user provided in the config.

### Step 3: Discover DP Info

```mermaid
flowchart TD
    CHECK["Read server_info.dp_size"]
    CHECK --> HAS_DP{dp_size > 1?}

    HAS_DP -->|No| SINGLE["DpInfo: 1 rank<br/>Single worker for this URL"]
    HAS_DP -->|Yes| MULTI["DpInfo: N ranks<br/>Each rank gets its own worker"]

    MULTI --> URLS["Compute rank URLs:<br/>base_url:port → base_url:port+0<br/>base_url:port → base_url:port+1<br/>..."]

    SINGLE --> CONTINUE[Continue to create_worker]
    URLS --> CONTINUE

    style MULTI fill:#ffd
```

**What is DP-aware routing?** Data Parallel means one inference server runs N copies of the model (N ranks) behind a single URL. MESH discovers this and creates N separate Worker objects so the routing policy can distribute load across ranks individually.

### Step 4: Create Worker Objects

```mermaid
flowchart TD
    DP_INFO["DpInfo: rank_count, rank_urls"] --> LOOP["For each DP rank"]

    LOOP --> BUILD["BasicWorkerBuilder::new(rank_url)<br/>.model_id(from metadata or config)<br/>.connection_mode(from step 1)<br/>.worker_type(Regular/Prefill/Decode)<br/>.runtime_type(Sglang/Vllm)<br/>.circuit_breaker_config(...)<br/>.health_config(...)<br/>.labels(merged config + discovered)<br/>.build()"]

    BUILD --> WORKER["Arc&lt;dyn Worker&gt;"]
    WORKER --> VEC["Vec&lt;Arc&lt;dyn Worker&gt;&gt;<br/>stored in workflow data"]

    style BUILD fill:#dfd
```

### Step 5: Register Workers

```mermaid
flowchart TD
    WORKERS["Vec workers from step 4"] --> LOOP["For each worker"]
    LOOP --> REG["WorkerRegistry.register(worker)"]
    REG --> ID["Returns WorkerId (UUID)"]
    ID --> METRICS["Record metrics:<br/>mesh_workers_registered_total"]
    METRICS --> LOOP
```

### Step 6: Parallel Final Steps

```mermaid
graph LR
    REGISTER[register_workers<br/>completed] --> TOK & POL & ACT

    TOK["submit_tokenizer_job<br/>━━━━━━━━━━━━━━━<br/>Submit Job::AddTokenizer<br/>for model's tokenizer<br/>━━━━━━━━━━━━━━━<br/>Failure: skip<br/>(tokenizer may already exist)"]

    POL["update_policies<br/>━━━━━━━━━━━━━━━<br/>Add worker to<br/>PolicyRegistry tables<br/>━━━━━━━━━━━━━━━<br/>Failure: skip<br/>(worker registered but<br/>may not route yet)"]

    ACT["activate_workers<br/>━━━━━━━━━━━━━━━<br/>Set healthy = true<br/>Worker now receives traffic<br/>━━━━━━━━━━━━━━━<br/>Failure: FailWorkflow<br/>(worker must be active)"]

    style TOK fill:#ddf
    style POL fill:#ddf
    style ACT fill:#dfd
```

## Startup: Bulk Worker Initialization

```mermaid
sequenceDiagram
    participant S as startup()
    participant JQ as JobQueue
    participant D as Dispatcher
    participant W1 as Workflow (worker 1)
    participant W2 as Workflow (worker 2)
    participant WN as Workflow (worker N)

    S->>JQ: submit(Job::InitializeWorkersFromConfig)
    JQ->>D: dequeue

    Note over D: Job splits into N AddWorker jobs

    D->>JQ: submit(AddWorker{url: worker_1})
    D->>JQ: submit(AddWorker{url: worker_2})
    D->>JQ: submit(AddWorker{url: worker_N})

    par Concurrent registration (max 200)
        D->>W1: spawn workflow
        Note over W1: detect → discover →<br/>create → register → activate
        D->>W2: spawn workflow
        Note over W2: detect → discover →<br/>create → register → activate
        D->>WN: spawn workflow
        Note over WN: detect → discover →<br/>create → register → activate
    end

    W1-->>JQ: completed
    W2-->>JQ: completed
    WN-->>JQ: completed

    Note over S: Server already accepting<br/>requests during this process
```

## Error Handling Summary

| Step | Failure Action | Rationale |
|------|---------------|-----------|
| detect_connection_mode | **FailWorkflow** | Cannot proceed without knowing the protocol |
| discover_metadata | **ContinueNextStep** | Metadata is optional — user config may suffice |
| discover_dp_info | **FailWorkflow** | Must know rank topology to create correct workers |
| create_worker | **FailWorkflow** | No worker objects = nothing to register |
| register_workers | **FailWorkflow** | Must be in registry to be routable |
| submit_tokenizer_job | **ContinueNextStep** | Tokenizer may already exist from startup or another worker |
| update_policies | **ContinueNextStep** | Worker is registered; policies can be updated later |
| activate_workers | **FailWorkflow** | Worker must be marked healthy to serve traffic |
