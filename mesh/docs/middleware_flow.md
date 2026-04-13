# Middleware Flow: Streaming Request Lifecycle

A streaming `POST /v1/chat/completions` request through all middleware layers.

## Request Flow (Onion Model)

```mermaid
sequenceDiagram
    participant C as Client
    participant RID as RequestIdLayer
    participant MET as HttpMetricsLayer
    participant TRC as TraceLayer
    participant CL as ConcurrencyLimiter
    participant QP as QueueProcessor
    participant H as Route Handler
    participant TGB as TokenGuardBody

    C->>RID: POST /v1/chat/completions (stream: true)

    Note over RID: [Pre] Extract x-request-id from headers<br/>or generate "chatcmpl-Ax7k..."<br/>Insert into req.extensions()

    RID->>MET: forward request

    Note over MET: [Pre] ACTIVE_HTTP_CONNECTIONS += 1<br/>in_flight_tracker.track()<br/>start = Instant::now()

    MET->>TRC: forward request

    Note over TRC: [MakeSpan] info_span!("http_request")<br/>[OnRequest] span.record("request_id")<br/>log: "started processing request"

    TRC->>CL: forward request

    alt token available
        Note over CL: try_acquire(1.0) = OK
        CL->>H: forward request
    else token NOT available & queue enabled
        CL->>QP: enqueue request (oneshot channel)
        alt token acquired within timeout
            QP-->>CL: permit OK
            CL->>H: forward request
        else timeout exceeded
            QP-->>CL: permit Err(408)
            CL-->>TRC: 408 Request Timeout
        end
    else token NOT available & no queue
        CL-->>TRC: 429 Too Many Requests
    end

    H-->>CL: Response (SSE stream body)

    Note over CL: Wrap body in TokenGuardBody<br/>token held until stream ends

    CL-->>TRC: Response with guarded body

    Note over TRC: [OnResponse] span.record("status_code", 200)<br/>span.record("latency", ...)<br/>log: "finished processing request"

    TRC-->>MET: Response

    Note over MET: [Post] drop(guard)<br/>ACTIVE_HTTP_CONNECTIONS -= 1<br/>record_http_duration()

    MET-->>RID: Response

    Note over RID: [Post] Insert x-request-id header

    RID-->>C: SSE stream begins

    Note over C,TGB: Client reads SSE chunks...<br/>tokens streaming over seconds/minutes

    C-xTGB: stream ends or client disconnects
    Note over TGB: Drop::drop() called<br/>bucket.return_tokens_sync(1.0)<br/>token returned to bucket
```

## Token Lifecycle in Streaming

```mermaid
graph LR
    A[acquire token] --> B[handler executes]
    B --> C[wrap body in TokenGuardBody]
    C --> D[middleware stack returns]
    D --> E["SSE streaming<br/>(token still held)"]
    E --> F["stream ends<br/>TokenGuardBody::drop()"]
    F --> G[return_tokens_sync to bucket]

    style A fill:#ffd
    style E fill:#fdd
    style G fill:#dfd
```

## Middleware Layer Order

Axum `.layer()` uses an onion model: **last added = outermost = executes first**.

```mermaid
graph TB
    subgraph "build_app() layer order (server.rs)"
        L1[".layer(RequestIdLayer)  — added last"]
        L2[".layer(HttpMetricsLayer)"]
        L3[".layer(create_logging_layer)"]
        L4[".layer(RequestBodyLimitLayer)"]
        L5[".route_layer(concurrency_limit_middleware)<br/>only on protected routes"]
        L6["Route Handler"]
    end

    L1 -->|"request ↓"| L2
    L2 -->|"request ↓"| L3
    L3 -->|"request ↓"| L4
    L4 -->|"request ↓"| L5
    L5 -->|"request ↓"| L6
    L6 -->|"response ↑"| L5
    L5 -->|"response ↑"| L4
    L4 -->|"response ↑"| L3
    L3 -->|"response ↑"| L2
    L2 -->|"response ↑"| L1

    style L1 fill:#ddf
    style L5 fill:#ffd
    style L6 fill:#dfd
```

> **Note**: `concurrency_limit_middleware` is a `.route_layer()` applied only to inference
> endpoints (`/v1/chat/completions`, `/v1/completions`, `/generate`, `/v1/responses`).
> Health checks, model listing, and admin routes bypass concurrency limiting.
