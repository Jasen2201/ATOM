# JobQueue Design

Async job queue for control plane operations — worker and tokenizer lifecycle management.

## Architecture Overview

```mermaid
graph TB
    subgraph Callers
        A1[startup - InitializeWorkersFromConfig]
        A2[startup - AddTokenizer]
        A3[REST API - POST /workers]
        A4[REST API - DELETE /workers/:id]
        A5[REST API - PUT /workers/:id]
        A6[REST API - POST /v1/tokenizers]
    end

    subgraph JobQueue
        TX[mpsc::Sender tx]
        CH[(Bounded Channel<br/>capacity: 1000)]
        RX[mpsc::Receiver rx]
        SEM[Semaphore<br/>max concurrent: 200]
        SM[DashMap status_map<br/>key: worker_url / tokenizer_id]
    end

    subgraph Background Tasks
        DISP[Dispatcher Task]
        CLEAN[Cleanup Task<br/>every 60s, TTL 5min]
    end

    subgraph Job Executors
        E1[tokio::spawn - Job 1]
        E2[tokio::spawn - Job 2]
        EN[tokio::spawn - Job N]
    end

    A1 & A2 & A3 & A4 & A5 & A6 -->|submit| TX
    TX --> CH
    CH --> RX
    RX --> DISP
    DISP -->|acquire permit| SEM
    DISP -->|spawn| E1 & E2 & EN
    E1 & E2 & EN -->|update| SM
    E1 & E2 & EN -->|drop permit| SEM
    CLEAN -->|retain fresh entries| SM
```

## Initialization Sequence

```mermaid
sequenceDiagram
    participant S as startup()
    participant AC as AppContext
    participant JQ as JobQueue
    participant D as Dispatcher Task
    participant C as Cleanup Task

    S->>AC: AppContext::from_config()<br/>worker_job_queue = OnceLock::new() (empty)
    Note over AC: AppContext created first,<br/>JobQueue field is empty OnceLock

    S->>S: Arc::downgrade(&app_context)
    Note over S: Weak reference avoids<br/>circular Arc dependency

    S->>JQ: JobQueue::new(config, Weak AppContext)
    activate JQ
    JQ->>JQ: mpsc::channel(1000)
    JQ->>JQ: Semaphore::new(200)
    JQ->>JQ: DashMap::new()
    JQ->>D: tokio::spawn(dispatcher loop)
    JQ->>C: tokio::spawn(cleanup loop)
    JQ-->>S: Arc JobQueue
    deactivate JQ

    S->>AC: worker_job_queue.set(Arc JobQueue)
    Note over AC: OnceLock filled — immutable from now on
```

## Circular Reference Problem & Solution

```mermaid
graph LR
    subgraph "Problem: Memory Leak"
        AC1[AppContext<br/>refcount: 2] -->|Arc strong ref| JQ1[JobQueue<br/>refcount: 1]
        JQ1 -->|Arc strong ref| AC1
    end
```

```mermaid
graph LR
    subgraph "Solution: Weak Reference"
        AC2[AppContext<br/>refcount: 1] -->|"Arc (OnceLock)"| JQ2[JobQueue]
        JQ2 -->|"Weak (no refcount)"| AC2
    end
```

```mermaid
graph TD
    subgraph "Weak::upgrade() at Runtime"
        JQ3[JobQueue process_job] -->|context.upgrade| CHECK{AppContext<br/>still alive?}
        CHECK -->|Some ctx| EXEC[Execute job normally]
        CHECK -->|None| FAIL[Log error, mark job failed<br/>Server is shutting down]
    end
```

## Job Lifecycle

```mermaid
stateDiagram-v2
    [*] --> Pending: submit()
    Pending --> Processing: dispatcher dequeues,<br/>semaphore permit acquired
    Processing --> Completed: execute_job() returns Ok
    Processing --> Failed: execute_job() returns Err
    Completed --> [*]: status removed from map
    Failed --> Cleaned: after 5 min TTL
    Cleaned --> [*]
```

## Job Types & Execution

```mermaid
flowchart TD
    JOB[Job submitted]
    JOB --> TYPE{Job Type}

    TYPE -->|AddWorker| AW_CHECK{runtime field?}
    AW_CHECK -->|external| EXT[ExternalWorker Workflow<br/>timeout: startup + 30s]
    AW_CHECK -->|local / default| LOC[LocalWorker Workflow<br/>timeout: startup + 30s]

    TYPE -->|UpdateWorker| UW[WorkerUpdate Workflow<br/>timeout: 30s]

    TYPE -->|RemoveWorker| RW[WorkerRemoval Workflow<br/>timeout: 30s]
    RW --> CLEAN_STATUS[Remove from status_map]

    TYPE -->|InitializeWorkersFromConfig| INIT
    INIT --> PARSE_MODE{Routing Mode?}
    PARSE_MODE -->|Regular| REG["Collect worker_urls<br/>(url, 'regular', None)"]
    PARSE_MODE -->|PrefillDecode| PD["Collect prefill_urls + decode_urls<br/>(url, type, bootstrap_port)"]
    REG & PD --> SPLIT["Split into N AddWorker jobs<br/>re-submit to same queue"]
    SPLIT -->|"Job 1"| AW_CHECK
    SPLIT -->|"Job 2"| AW_CHECK
    SPLIT -->|"Job N"| AW_CHECK

    TYPE -->|AddTokenizer| AT[Tokenizer Workflow<br/>timeout: 600s for HF downloads]

    TYPE -->|RemoveTokenizer| RT[Synchronous removal<br/>from TokenizerRegistry]
```

## Startup Job Flow

```mermaid
sequenceDiagram
    participant S as startup()
    participant JQ as JobQueue
    participant D as Dispatcher
    participant W1 as Worker Task 1
    participant W2 as Worker Task 2
    participant WN as Worker Task N

    Note over S: Phase 1: Tokenizer
    S->>JQ: submit(Job::AddTokenizer)
    JQ->>D: channel send
    D->>D: acquire semaphore permit
    D-->>D: spawn tokenizer workflow (background)

    Note over S: Phase 2: Workers
    S->>JQ: submit(Job::InitializeWorkersFromConfig)
    JQ->>D: channel send
    D->>D: acquire semaphore permit

    Note over D: Job splits into N AddWorker jobs
    D->>JQ: submit(AddWorker for worker 1)
    D->>JQ: submit(AddWorker for worker 2)
    D->>JQ: submit(AddWorker for worker N)

    par Parallel Worker Registration
        D->>W1: spawn(process_job)
        Note over W1: connect → health check → register
        D->>W2: spawn(process_job)
        Note over W2: connect → health check → register
        D->>WN: spawn(process_job)
        Note over WN: connect → health check → register
    end

    Note over S: startup() continues immediately<br/>without waiting for workers

    W1-->>JQ: status: completed
    W2-->>JQ: status: completed
    WN-->>JQ: status: completed
```

## Concurrency Control

```mermaid
graph TD
    subgraph "Semaphore Gate (max 200)"
        P1[Permit 1 - Job A]
        P2[Permit 2 - Job B]
        P3[...]
        P200[Permit 200 - Job X]
    end

    WAIT[Job Y waiting<br/>sem.acquire_owned.await]
    DONE[Job A completes<br/>permit dropped]

    WAIT -->|blocked| P1
    DONE -->|permit freed| WAIT
    WAIT -->|now acquired| RUN[Job Y starts executing]

    style WAIT fill:#ffd,stroke:#aa0
    style DONE fill:#dfd,stroke:#0a0
    style RUN fill:#ddf,stroke:#00a
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `Weak<AppContext>` instead of `Arc` | Breaks circular reference: AppContext → JobQueue → AppContext |
| `OnceLock` for storing JobQueue in AppContext | AppContext must be created before JobQueue (chicken-and-egg), OnceLock allows deferred one-time init |
| Bounded channel (1000) | Backpressure: `submit()` blocks if queue is full, prevents unbounded memory growth |
| Semaphore (200) | Limits concurrent workflow executions, prevents overwhelming the system during bulk operations |
| `InitializeWorkersFromConfig` splits into `AddWorker` jobs | Reuses the same registration workflow, enables parallel worker init with shared concurrency control |
| DashMap for status tracking | Lock-free concurrent HashMap, safe for reads/writes from dispatcher + cleanup + API handlers |
| 5-minute TTL cleanup | Prevents status_map from growing unbounded while keeping recent results queryable |
