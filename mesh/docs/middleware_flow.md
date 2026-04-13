# Middleware Flow: Streaming Request Lifecycle

A streaming `POST /v1/chat/completions` request through all middleware layers.

```
Client
  │
  │  POST /v1/chat/completions  (stream: true)
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     RequestIdLayer  (L128-210)                      │
│                                                                     │
│  [Pre]  Extract x-request-id from headers                          │
│         OR generate: "chatcmpl-Ax7kQ9mN3pR2wY5t..."               │
│         → req.extensions_mut().insert(RequestId)                   │
│                         │                                           │
│                         ▼                                           │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                  HttpMetricsLayer  (L510-590)                │   │
│  │                                                              │   │
│  │  [Pre]  ACTIVE_HTTP_CONNECTIONS += 1                         │   │
│  │         in_flight_request_tracker.track()                    │   │
│  │         start = Instant::now()                               │   │
│  │                       │                                      │   │
│  │                       ▼                                      │   │
│  │  ┌────────────────────────────────────────────────────────┐  │   │
│  │  │               TraceLayer  (L212-312)                   │  │   │
│  │  │                                                        │  │   │
│  │  │  [MakeSpan]  info_span!("http_request", method, uri,   │  │   │
│  │  │              request_id=Empty, status_code=Empty, ...)  │  │   │
│  │  │                                                        │  │   │
│  │  │  [OnRequest] span.record("request_id", "chatcmpl-...") │  │   │
│  │  │              Metrics::record_http_request("POST", path) │  │   │
│  │  │              log: "started processing request"          │  │   │
│  │  │                       │                                 │  │   │
│  │  │                       ▼                                 │  │   │
│  │  │  ┌──────────────────────────────────────────────────┐   │  │   │
│  │  │  │       concurrency_limit_middleware (L426-504)     │   │  │   │
│  │  │  │       (only on protected routes)                 │   │  │   │
│  │  │  │                                                  │   │  │   │
│  │  │  │   token_bucket.try_acquire(1.0)                  │   │  │   │
│  │  │  │          │                                       │   │  │   │
│  │  │  │    ┌─────┴──────┐                                │   │  │   │
│  │  │  │    │            │                                │   │  │   │
│  │  │  │  [OK]        [Fail]                              │   │  │   │
│  │  │  │    │            │                                │   │  │   │
│  │  │  │    │      ┌─────┴──────┐                         │   │  │   │
│  │  │  │    │      │            │                         │   │  │   │
│  │  │  │    │  [has queue]  [no queue]                     │   │  │   │
│  │  │  │    │      │            │                         │   │  │   │
│  │  │  │    │   enqueue &   return 429                    │   │  │   │
│  │  │  │    │   wait for                                  │   │  │   │
│  │  │  │    │   permit_rx                                 │   │  │   │
│  │  │  │    │      │                                      │   │  │   │
│  │  │  │    │  ┌───┴────┐                                 │   │  │   │
│  │  │  │    │ [OK]   [Timeout]                            │   │  │   │
│  │  │  │    │  │     return 408                            │   │  │   │
│  │  │  │    │  │                                          │   │  │   │
│  │  │  │    ▼  ▼                                          │   │  │   │
│  │  │  │  ┌──────────────────────┐                        │   │  │   │
│  │  │  │  │    Route Handler     │                        │   │  │   │
│  │  │  │  │  v1_chat_completions │                        │   │  │   │
│  │  │  │  │     → router.route() │                        │   │  │   │
│  │  │  │  │     → SSE stream     │                        │   │  │   │
│  │  │  │  └──────────┬───────────┘                        │   │  │   │
│  │  │  │             │                                    │   │  │   │
│  │  │  │             ▼                                    │   │  │   │
│  │  │  │  ┌──────────────────────────────────┐            │   │  │   │
│  │  │  │  │  Wrap body: TokenGuardBody       │            │   │  │   │
│  │  │  │  │  (holds token until stream ends) │            │   │  │   │
│  │  │  │  └──────────┬───────────────────────┘            │   │  │   │
│  │  │  │             │                                    │   │  │   │
│  │  │  └─────────────┼────────────────────────────────────┘   │  │   │
│  │  │                │                                        │  │   │
│  │  │  [OnResponse]  span.record("status_code", 200)          │  │   │
│  │  │                span.record("latency", ...)              │  │   │
│  │  │                log: "finished processing request"       │  │   │
│  │  │                │                                        │  │   │
│  │  └────────────────┼────────────────────────────────────────┘  │   │
│  │                   │                                           │   │
│  │  [Post] drop(guard) → in_flight tracking ends                │   │
│  │         ACTIVE_HTTP_CONNECTIONS -= 1                          │   │
│  │         Metrics::record_http_duration(method, path, elapsed)  │   │
│  │                   │                                           │   │
│  └───────────────────┼───────────────────────────────────────────┘   │
│                      │                                               │
│  [Post]  response.headers.insert("x-request-id", "chatcmpl-...")    │
│                      │                                               │
└──────────────────────┼───────────────────────────────────────────────┘
                       │
                       ▼
                    Client
              (receives SSE stream)
                       │
              ... tokens streaming ...
                       │
                  stream ends
                       │
                       ▼
              ┌─────────────────────┐
              │ TokenGuardBody Drop │
              │ → return_tokens(1.0)│
              │   back to bucket    │
              └─────────────────────┘
```

## Key Insight: Token Lifecycle in Streaming

For non-streaming requests, the token is acquired and returned within the middleware.
For streaming requests, the token is **held by `TokenGuardBody`** throughout the entire
stream duration. This is critical because:

1. `concurrency_limit_middleware` acquires 1 token
2. The response body is wrapped in `TokenGuardBody`
3. The middleware stack returns, but the **token is NOT returned yet**
4. Client reads SSE chunks over seconds/minutes
5. When the stream ends (or client disconnects), `TokenGuardBody` is dropped
6. `Drop::drop()` calls `bucket.return_tokens_sync(1.0)` — token finally returned

This ensures that concurrent streaming connections are accurately counted.
