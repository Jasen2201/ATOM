# WorkflowEngines Design

Typed workflow engine collection for executing multi-step control plane operations with retry, timeout, and DAG-based step ordering.

## Architecture Overview

```mermaid
graph TB
    subgraph WorkflowEngines
        LW[local_worker<br/>LocalWorkerEngine]
        WR[worker_removal<br/>WorkerRemovalEngine]
        WU[worker_update<br/>WorkerUpdateEngine]
        TK[tokenizer<br/>TokenizerEngine]
    end

    subgraph "wfaas Library"
        WE["WorkflowEngine&lt;D, Store&gt;"]
        WD[WorkflowDefinition<br/>DAG of StepDefinitions]
        SE["StepExecutor&lt;D&gt; trait"]
        EB[EventBus<br/>EventSubscriber]
        IMS[InMemoryStore]
    end

    LW --> WE
    WR --> WE
    WU --> WE
    TK --> WE

    WE --> WD
    WD --> SE
    WE --> EB
    WE --> IMS

    EB --> LOG[LoggingSubscriber<br/>All workflow events logged]
```

## Engine Types

```mermaid
graph LR
    subgraph "Each engine is fully typed"
        LW["LocalWorkerEngine<br/>WorkflowEngine&lt;LocalWorkerWorkflowData, InMemoryStore&gt;"]
        WR["WorkerRemovalEngine<br/>WorkflowEngine&lt;WorkerRemovalWorkflowData, InMemoryStore&gt;"]
        WU["WorkerUpdateEngine<br/>WorkflowEngine&lt;WorkerUpdateWorkflowData, InMemoryStore&gt;"]
        TK["TokenizerEngine<br/>WorkflowEngine&lt;TokenizerWorkflowData, InMemoryStore&gt;"]
    end

    style LW fill:#dfd
    style WR fill:#fdd
    style WU fill:#ffd
    style TK fill:#ddf
```

## Step Configuration

Each step in a workflow can be configured with:

```mermaid
graph TD
    STEP[StepDefinition]
    STEP --> NAME["name + display_name"]
    STEP --> EXEC["StepExecutor&lt;D&gt;<br/>async execute()"]
    STEP --> RETRY["RetryPolicy<br/>max_attempts + backoff"]
    STEP --> TIMEOUT["Timeout<br/>Duration"]
    STEP --> FAILURE["FailureAction<br/>FailWorkflow | ContinueNextStep"]
    STEP --> DEPS["depends_on<br/>DAG ordering"]

    subgraph "Backoff Strategies"
        FIXED["Fixed(duration)"]
        LINEAR["Linear(increment, max)"]
        EXPO["Exponential(base, max)"]
    end

    RETRY --> FIXED
    RETRY --> LINEAR
    RETRY --> EXPO
```

## Local Worker Registration Workflow

The most complex workflow — registers a locally deployed inference engine.

```mermaid
graph TD
    START((Start)) --> DETECT

    DETECT["detect_connection_mode<br/>HTTP vs gRPC probe<br/>━━━━━━━━━━━━━━━━━━━<br/>Retry: dynamic (based on timeout)<br/>Backoff: Linear 1s→5s<br/>Timeout: worker_startup_timeout<br/>Failure: FailWorkflow"]

    DETECT --> META

    META["discover_metadata<br/>GET /server_info or gRPC<br/>━━━━━━━━━━━━━━━━━━━<br/>Retry: 3x, Fixed 1s<br/>Timeout: 10s<br/>Failure: ContinueNextStep"]

    META --> DP

    DP["discover_dp_info<br/>Detect Data Parallel ranks<br/>━━━━━━━━━━━━━━━━━━━<br/>Retry: 3x, Fixed 1s<br/>Timeout: 10s<br/>Failure: FailWorkflow"]

    DP --> CREATE

    CREATE["create_worker<br/>Build Worker objects<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 5s<br/>Failure: FailWorkflow"]

    CREATE --> REGISTER

    REGISTER["register_workers<br/>Add to WorkerRegistry<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 5s<br/>Failure: FailWorkflow"]

    REGISTER --> TOKENIZER & POLICIES & ACTIVATE

    TOKENIZER["submit_tokenizer_job<br/>Queue tokenizer loading<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 5s<br/>Failure: ContinueNextStep"]

    POLICIES["update_policies<br/>Add to PolicyRegistry<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 5s<br/>Failure: ContinueNextStep"]

    ACTIVATE["activate_workers<br/>Mark healthy, start serving<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 5s<br/>Failure: FailWorkflow"]

    TOKENIZER --> DONE((Done))
    POLICIES --> DONE
    ACTIVATE --> DONE

    style DETECT fill:#ffd
    style META fill:#ffe
    style DP fill:#ffd
    style CREATE fill:#dfd
    style REGISTER fill:#dfd
    style TOKENIZER fill:#ddf
    style POLICIES fill:#ddf
    style ACTIVATE fill:#ddf
```

## Worker Removal Workflow

```mermaid
graph TD
    START((Start)) --> FIND

    FIND["find_workers_to_remove<br/>Lookup by URL in registry<br/>━━━━━━━━━━━━━━━━━━━<br/>Retry: 1x<br/>Timeout: 10s"]

    FIND --> REMOVE_POLICY

    REMOVE_POLICY["remove_from_policy_registry<br/>Unregister from all policies<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 10s"]

    REMOVE_POLICY --> REMOVE_WORKER

    REMOVE_WORKER["remove_from_worker_registry<br/>Delete from WorkerRegistry<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 10s"]

    REMOVE_WORKER --> UPDATE_REMAINING

    UPDATE_REMAINING["update_remaining_policies<br/>Rebalance remaining workers<br/>━━━━━━━━━━━━━━━━━━━<br/>Timeout: 10s<br/>Failure: ContinueNextStep"]

    UPDATE_REMAINING --> DONE((Done))

    style FIND fill:#fdd
    style REMOVE_POLICY fill:#fdd
    style REMOVE_WORKER fill:#fdd
    style UPDATE_REMAINING fill:#ffe
```

## Worker Update Workflow

```mermaid
graph TD
    START((Start)) --> FIND

    FIND["find_worker_to_update<br/>Lookup worker by URL"]

    FIND --> UPDATE

    UPDATE["update_worker_properties<br/>Apply new labels/config"]

    UPDATE --> POLICY

    POLICY["update_policies_for_worker<br/>Reflect changes in routing<br/>━━━━━━━━━━━━━━━━━━━<br/>Failure: ContinueNextStep"]

    POLICY --> DONE((Done))

    style FIND fill:#ffd
    style UPDATE fill:#ffd
    style POLICY fill:#ffe
```

## Tokenizer Registration Workflow

```mermaid
graph TD
    START((Start)) --> LOAD

    LOAD["load_tokenizer<br/>Load from local path or HuggingFace<br/>Register in TokenizerRegistry<br/>━━━━━━━━━━━━━━━━━━━<br/>Retry: 3x, Fixed 2s<br/>Timeout: 300s (5 min for HF downloads)<br/>Failure: FailWorkflow"]

    LOAD --> DONE((Done))

    style LOAD fill:#ddf
```

## Integration with JobQueue

```mermaid
sequenceDiagram
    participant JQ as JobQueue
    participant EJ as execute_job()
    participant WE as WorkflowEngines
    participant ENGINE as WorkflowEngine
    participant STEPS as Step Executors

    JQ->>EJ: process_job(Job::AddWorker)
    EJ->>WE: engines.local_worker
    EJ->>ENGINE: start_workflow(id, data)
    activate ENGINE

    loop DAG execution
        ENGINE->>STEPS: execute step (respecting depends_on)

        alt Step succeeds
            STEPS-->>ENGINE: StepResult::Success
        else Step fails (retryable)
            STEPS-->>ENGINE: WorkflowError
            ENGINE->>ENGINE: Apply RetryPolicy (backoff + wait)
            ENGINE->>STEPS: Retry step
        else Step fails (max retries)
            STEPS-->>ENGINE: WorkflowError
            alt FailureAction::FailWorkflow
                ENGINE-->>EJ: Err(workflow failed)
            else FailureAction::ContinueNextStep
                ENGINE->>ENGINE: Skip to next step
            end
        end
    end

    ENGINE-->>EJ: Ok(completed)
    deactivate ENGINE
    EJ->>JQ: record_job_completion()
```

## WorkerRegistrationData Trait

Shared steps (register, activate, update_policies) work with any workflow data via this trait:

```mermaid
classDiagram
    class WorkerRegistrationData {
        <<trait>>
        +get_app_context() Option~Arc~AppContext~~
        +get_actual_workers() Option~Vec~Arc~dyn Worker~~~
        +get_labels() Option~HashMap~String, String~~
    }

    class LocalWorkerWorkflowData {
        +config: WorkerConfigRequest
        +connection_mode: Option~ConnectionMode~
        +discovered_labels: HashMap
        +dp_info: Option~DpInfo~
        +workers: Option~WorkerList~
        +app_context: Option~Arc~AppContext~~
        +actual_workers: Option~Vec~Arc~dyn Worker~~~
    }

    WorkerRegistrationData <|.. LocalWorkerWorkflowData

    class RegisterWorkersStep {
        +execute(ctx)
    }
    class ActivateWorkersStep {
        +execute(ctx)
    }
    class UpdatePoliciesStep {
        +execute(ctx)
    }

    RegisterWorkersStep ..> WorkerRegistrationData : "impl StepExecutor&lt;D: WorkerRegistrationData&gt;"
    ActivateWorkersStep ..> WorkerRegistrationData
    UpdatePoliciesStep ..> WorkerRegistrationData
```

## Data Flow: Worker Registration End-to-End

```mermaid
graph LR
    subgraph Input
        URL["Worker URL<br/>http://10.0.0.1:8000"]
        CONFIG["WorkerConfigRequest<br/>type, api_key, labels"]
    end

    subgraph "Step 1: Detect"
        PROBE["Probe HTTP + gRPC<br/>Determine protocol"]
        MODE["ConnectionMode::Http<br/>or ConnectionMode::Grpc"]
    end

    subgraph "Step 2: Discover"
        INFO["GET /server_info<br/>or gRPC ServerInfo"]
        LABELS["model_id, max_model_len<br/>runtime_type, etc."]
    end

    subgraph "Step 3: DP Discovery"
        DPCHECK["Check DP rank count<br/>behind single URL"]
        DPINFO["DpInfo: rank_count, urls"]
    end

    subgraph "Step 4: Create"
        BUILD["BasicWorkerBuilder<br/>.url() .model_id()<br/>.connection_mode()<br/>.worker_type()"]
        WORKERS["Vec&lt;Arc&lt;dyn Worker&gt;&gt;<br/>1 per DP rank"]
    end

    subgraph "Step 5: Register + Activate"
        REG["WorkerRegistry.register()"]
        POL["PolicyRegistry.add()"]
        ACT["worker.set_healthy(true)"]
    end

    URL --> PROBE --> MODE
    CONFIG --> PROBE
    MODE --> INFO --> LABELS
    LABELS --> DPCHECK --> DPINFO
    DPINFO --> BUILD --> WORKERS
    WORKERS --> REG
    WORKERS --> POL
    WORKERS --> ACT
```
