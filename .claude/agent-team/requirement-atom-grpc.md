# Requirement: ATOM gRPC interface for atomesh, tokens in and tokens out

Tracking: wuhuikx/LLM-Booster#651 (sub-issues wuhuikx/LLM-Booster#660 to #670).
This line covers the CPU part, items 1 to 8 below. GPU end-to-end validation
and the split into ROCm/ATOM PRs are done by the owner afterwards.

## Why

At high concurrency the CPU frontend saturates before the GPUs do:

- ATOM's Python API process: with DeepSeek-V4.1-Flash agentic at c512, one
  process stops reading sockets in time after 10 to 20 minutes, and throughput
  falls from about 12K to 5K tok/s/GPU (ROCm/ATOM#2913).
- The same prompt is tokenized twice, once by atomesh for routing and once by
  ATOM's prefill. The two do not always agree (ROCm/ATOM#3473).

## What to build

atomesh's gRPC mode becomes the only entrypoint. It already does template
rendering, tokenization, detokenization and tool/reasoning parsing in Rust
(`routers/prepare`, `routers/render`, the gRPC pipeline). ATOM gets a gRPC
service that receives token ids and returns token ids, and does none of that
work on this path.

- **ATOM's own protocol**, `atom.grpc.v1.AtomEngine`, unrelated to the SGLang
  and vLLM protos. Sampling parameters reuse engine_v1's `SamplingParams`
  (`atom/model_engine/zmq_protocol/engine_v1.proto`). `kv_transfer_params`
  travels as an opaque JSON string, in the request and in prefill's completion,
  with the same meaning it has in the HTTP API today.
- **P/D is a transport swap only.** atomesh keeps ATOM's existing P/D flow
  exactly: relay or chunked transfer, `AtomAdapter` field injection and
  decode-side enrichment (`core/placement/backend/atom.rs`,
  `routers/http_pd_router.rs`), and the rank mapping. Only the calls to P and D
  move from HTTP JSON to gRPC. Do not use SGLang's bootstrap dual dispatch.
- **DP:** atomesh discovers each instance's DP size, routes to url@rank with
  `--dp-aware`, and passes `data_parallel_rank` to P and D. Decode's
  `remote_dp_rank` names the P rank actually used.

Protocol sketch, to be finalized by item 1:

```proto
syntax = "proto3";
package atom.grpc.v1;
import "atom/model_engine/zmq_protocol/engine_v1.proto";

service AtomEngine {
  rpc Generate(GenerateRequest) returns (stream GenerateResponse);
  rpc Abort(AbortRequest) returns (AbortResponse);
  rpc GetModelInfo(GetModelInfoRequest) returns (GetModelInfoResponse);     // tokenizer registration
  rpc GetServerInfo(GetServerInfoRequest) returns (GetServerInfoResponse);  // what /kv_transfer_info returns, plus DP size
  rpc HealthCheck(HealthCheckRequest) returns (HealthCheckResponse);
}
message GenerateRequest {
  string request_id = 1;
  repeated uint32 prompt_token_ids = 2;
  atom.engine.v1.SamplingParams sampling = 3;
  repeated atom.engine.v1.TokenIds stop_token_sequences = 4;
  optional string kv_transfer_params_json = 5;
  optional int32 data_parallel_rank = 6;
  bool stream = 7;
}
message GenerateResponse {
  oneof response {
    GenerateStreamChunk chunk = 1;   // this step's new tokens, prompt / completion / cached tokens, logprobs
    GenerateComplete complete = 2;   // finish_reason, matched stop token, usage, kv_transfer_params_json
    GenerateError error = 3;         // message and HTTP status code
  }
}
```

## Work items, in order

Every item is developed and accepted on CPU. The gate is
`.claude/agent-team/gate.sh`.

1. **ATOM gRPC server** (wuhuikx/LLM-Booster#660).
   - `--grpc-port` starts it beside the HTTP server, in the process that owns
     CoreManager.
   - Generate: streaming and non-streaming; MTP tokens of one step go out as
     one chunk.
   - Abort on cancel or `Abort`, releasing KV.
   - `GetModelInfo` and `GetServerInfo`.
   - Validation and errors as in the HTTP routes.
   - The generated pb2 is committed. The engine_v1 Python codec is in
     ROCm/ATOM#3489, not merged yet: bring in what is needed, or decode
     `SamplingParams` locally.
   - Accept: fake engine plus real gRPC tests; with the same prompt ids and
     greedy sampling, the output tokens equal `/v1/completions`.
2. **No-GPU fake engine and harness** (wuhuikx/LLM-Booster#667).
   - Replace only the ModelRunner and the KV connector. Keep the scheduler,
     BlockManager and prefix cache real.
   - Step times follow a law on a real-time clock: Compass's fitted
     DeepSeek-V4.1-Flash step law (wuhuikx/LLM-Booster#592) when available,
     otherwise a linear model.
   - Fake KV transfer honours the `kv_transfer_params` protocol, for relay and
     chunked, with failure injection. DP fake ranks.
   - One command starts atomesh (gRPC) plus N fake instances from a topology
     file: single instance, 1P1D, and 3P×DP2 + 4D×DP4.
   - Accept: TTFT and ITL within 5% of the configured law; functional cases
     pass on CPU.
3. **atomesh ATOM gRPC client, single instance** (wuhuikx/LLM-Booster#661).
   - Compile the proto with protox.
   - Add an `atom` runtime to worker detection.
   - Encode `GenerationPayload` and decode the stream into `TokenChunk`;
     `Abort` when the handle drops.
   - Use `GetModelInfo` for tokenizer registration.
   - Align with wuhuikx/LLM-Booster#650 (MengqingCao's mesh gRPC refactor).
   - Accept: chat, completions, streaming, abort and rejection cases pass
     against the fake engine; prompt ids arrive unchanged; no token is lost or
     duplicated.
4. **Existing ATOM P/D flow over gRPC** (wuhuikx/LLM-Booster#662).
   - Relay and chunked through `AtomAdapter`.
   - Prefill info comes from `GetServerInfo` instead of `/kv_transfer_info`.
   - Decode gets atomesh's token ids directly.
   - P failure: no D in relay, abort D in chunked. Keep the failed-receive hold
     of ROCm/ATOM#2431.
   - Accept: relay, chunked, P failure, D cancel and transfer timeout pass. For
     the same request, the `kv_transfer_params` sent to P and D equal the HTTP
     path's.
5. **DP P/D** (wuhuikx/LLM-Booster#668).
   - P has DP2 and D has DP4, as in the 6P16D recipe on the ROCm/ATOM branch
     `lirzhang/recipe-6p16d-c1228-share-cache-20261117`.
   - Covers rank discovery, `data_parallel_rank` on both sides,
     `remote_dp_rank/size`, and `--atom-pd-rank-mapping-policy none` and
     idx2idx, each with relay and chunked.
   - Per-rank load counters return to zero.
   - Run once at 3P×DP2 + 4D×DP4 (22 fake ranks).
   - Accept: each request lands on the ranks atomesh chose, and decode's
     `remote_dp_rank` equals the P rank used.
6. **prefix-token-ids** (wuhuikx/LLM-Booster#664). ROCm/ATOM#2716 reuses
   prompt block-hash chains across turns, but only for text input. Extend it to
   token-id input on the ATOM side: match the previous turn by token prefix and
   reuse full blocks only, under `ATOM_TOKENIZE_CACHE_MB`.
   - Accept: reused chains equal a full recompute; the hash time per turn
     covers only the new blocks.
7. **Batched gRPC session** (wuhuikx/LLM-Booster#665).
   - Add `rpc Session(stream Frames) returns (stream Frames)`. Each `Frames`
     is an engine_v1 `Envelope` plus data frames: one batched message per
     engine step, not one per request.
   - Each session gets its own seq_id window.
   - atomesh reads the batched output and splits it per request.
   - Why: per-request streams cost one Python process 65% of a core at c512
     and saturate at c1024 (43K msgs/s); batched costs 1-2% (measured on
     pit2-p03-g48).
   - Accept: with the fake engine at c512, gRPC CPU is at most 5%; at c1024
     every message is delivered with p99 step lag at most 2 ms.
8. **C1228 frontend simulation** (wuhuikx/LLM-Booster#669).
   - Fake engines in the recipe's 3P×DP2 + 4D×DP4 topology. Replay the AgentX
     trace (revision `23f152f6`) at C1228, with aiperf flags as in the recipe.
   - Compare three paths on identical fake engines: atomesh HTTP + ATOM HTTP,
     atomesh gRPC + ATOM gRPC, and the batched session.
   - Report atomesh cores, ATOM frontend CPU, extra TTFT/ITL over the fake
     ideal, and stability over 30 minutes.

## Constraints

- Nothing is pushed to ROCm/ATOM. Each PR stays within 1000 changed lines and
  15 files.
- Code comments and docstrings are at most two lines.
- Template-rendering parity between atomesh and ATOM's Python frontend is
  wuhuikx/LLM-Booster#649 (MengqingCao), not this line. Use GLM-5.2's Jinja
  template or completions in tests.
- Multimodal, `/v1/messages` and `/v1/responses` stay on atomesh's existing
  HTTP path.
