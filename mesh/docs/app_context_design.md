# AppContext Design

Global dependency container for the MESH router — centralizes all shared resources behind `Arc<AppContext>`.

```mermaid
flowchart TB
    subgraph AppContext ["Arc&lt;AppContext&gt;"]
        direction LR

        subgraph DataPlane ["Data Plane — request path"]
            direction TB
            CLIENT["client<br/>(reqwest)"]
            WR["worker_registry"]
            PR["policy_registry"]
            RL["rate_limiter<br/>(TokenBucket)"]
            TR["tokenizer_registry"]
            PARSERS["reasoning / tool<br/>parser_factory"]
            STORAGE["response / conversation<br/>storage"]
        end

        subgraph ControlPlane ["Control Plane — worker lifecycle"]
            direction TB
            WS["worker_service"]
            JQ["worker_job_queue<br/>(OnceLock)"]
            WE["workflow_engines<br/>(OnceLock)"]
            LM["load_monitor"]
        end
    end

    REQ(["POST /v1/chat/completions"]) -->|"route + forward"| DataPlane
    ADMIN(["POST /workers<br/>health checker"]) -->|"create / check"| ControlPlane
    ControlPlane -->|"register / remove<br/>workers into"| WR

    style JQ fill:#ffd,stroke:#aa0
    style WE fill:#ffd,stroke:#aa0
```

> **Data Plane**: serves every inference request — pick a worker (policy_registry),
> forward (client), parse response (parsers), store if needed (storage).
>
> **Control Plane**: manages worker lifecycle in the background — create, health check,
> update, remove workers via JobQueue + WorkflowEngines. Results flow back into
> worker_registry, which the Data Plane reads.
>
> **OnceLock fields** (yellow): lazily initialized after AppContext creation to break
> a circular dependency (JobQueue needs `Weak<AppContext>`, but AppContext contains JobQueue).
