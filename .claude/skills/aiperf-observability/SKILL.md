---
name: aiperf-observability
description: Convert AIPerf profile_export JSONL or artifact directories into observable request timelines. Use when the user provides AIPerf JSON/JSONL, asks to inspect concurrency, sessions, TTFT, cache tokens, wait/TTFB, prefill overlap, or wants Chrome Trace/Perfetto visualization.
---

# AIPerf Observability

将 AIPerf 的逐请求记录整理成可观测的 session-tree 和 concurrency-slot 时间线。

## Quick start

输入可以是：

- `profile_export.jsonl`
- 包含 `profile_export.jsonl` 和 `profile_export_aiperf.json` 的 artifact 目录

优先执行：

```bash
python .claude/skills/aiperf-observability/scripts/aiperf_to_chrome_trace.py \
  <artifact_dir>/profile_export.jsonl \
  --phase all \
  --concurrency <configured_concurrency> \
  --inspect-all-slots \
  -o <artifact_dir>/profile_export.c<concurrency>.slots.trace.json
```

如果 artifact 目录不可写，将输出放到其父目录或用户指定的可写目录。不要修改原始 JSONL。

## 配置和分组

1. 优先从 `profile_export_aiperf.json` 的 profiling phase 读取 `concurrency`。
2. 不要把 `session_num` 当作 session ID；它是请求/会话顺序编号。
3. Agentic replay 使用 `root_correlation_id` 作为完整 session tree 的 key：
   - root 请求和所有 descendant sub-agent 请求共享一个 tree。
   - 一个 tree 占用一个 slot，直到整个 tree drain。
   - tree 完成后，slot 可被后续 tree 复用。
4. 一个 tree 内使用 `x_correlation_id` 区分 root 和 sub-agent 分支。
5. `parent_correlation_id` 用于解释父子 Agent 关系；`conversation_id` 是数据集/trajectory 标识，不保证等同于运行时 session。

`--concurrency N` 模式必须生成固定的 `slot 00` 到 `slot N-1` overview 轨道。tree 按生命周期 greedy-pack 到可复用 slot；不要直接把每个 root ID 当作永久轨道。

## Trace 内容

固定 slot 模式使用 `--inspect-all-slots`，生成：

- `slot XX overview`：该 slot 中每个 root tree 的完整生命周期。
- `slot XX row 0`：root session 请求。
- 更高 row：子 Agent 或同一分支的并行请求，row 是显示轨道，不是 ID。
- `all slots • TTFT`：全部请求的 TTFT/prefill 总览。
- `all slots • decode`：全部请求的 decode 总览。
- `all slots • prefill+decode`：同一轨道展示全部 prefill 和 decode；
  正常 prefill 用黄色、decode 用蓝色，cache 异常 prefill 用红色。
- 请求 row 内的 `HTTP wait/TTFB`：该请求自己的 HTTP waiting 区间。
- `slot XX wait∩prefill`：wait/TTFB 与 prefill 的实际重叠区间。

每个请求事件的标题和 args 至少包含：

- request number、`x_request_id`
- `root_correlation_id`、`x_correlation_id`
- `slot_index`、`slot_row`
- `time_to_first_token_ms`
- `input_tokens`、`output_tokens`
- `usage_prompt_tokens`
- `prompt_cache_read_tokens`
- `usage_completion_tokens`
- `cache_read_ratio_pct`
- `cache_anomaly`

当 `prompt_cache_read_tokens / input_tokens < 80%` 时，将 TTFT 总览和对应
prefill 事件标记为红色。TTFT 和 decode 总览通过 Chrome Trace flow events
连接到具体 slot row 的对应事件。若 viewer 不显示连接线，使用事件 args 中的
`slot_index`、`slot_row` 和 `x_request_id` 定位。

Chrome Trace 颜色使用事件的 `cname` 字段；Perfetto UI 可能忽略该 legacy
颜色字段，因此颜色验证应优先在 `chrome://tracing` 中进行。

## 时间语义

- 请求区间：`request_start_ns` 到 `request_end_ns`
- TTFT/prefill 结束：`request_start_ns + time_to_first_token * 1e6`，并限制在请求区间内
- HTTP wait 起点：`request_start_ns + http_req_sending * 1e6`
- wait/TTFB 结束：wait 起点加 `http_req_waiting`
- overlap：`wait/TTFB` 与 `[request_start, TTFT]` 的交集

`wait∩prefill` 证明两个客户端观测区间在时间上重叠，不单独证明 GPU 已饱和。判断 GPU saturation 还需要 server metrics 或 GPU trace。

## Token 字段选择

- `usage_prompt_tokens`：API/server 返回的 prompt token 数，优先用于“真实输入 token”。
- `input_sequence_length`：AIPerf ISL；正常情况下可能由客户端 tokenizer 计算。
- 如果命令包含 `--use-server-token-count`，AIPerf 会使用 server usage 计算 ISL。
- `usage_prompt_cache_read_tokens` 是 prompt token 的 cache-read 子集，不要重复加到总输入 token。
- 无 `usage` 或 tokenization 失败时保留 null，并在结果中说明。

## 阶段选择

- `--phase all`：复现完整 slot 生命周期，适合 Agentic replay 的 slot 分配。
- `--phase profiling`：只看 profiling 请求，适合纯性能分析，但可能截断 warmup 中已启动的 tree。
- 结果中必须说明是否包含 warmup。

## 打开和验证

将生成的 JSON 下载到本地后打开：

```text
chrome://tracing
```

大文件优先使用：

```text
https://ui.perfetto.dev
```

验证输出：

```bash
python -m json.tool <trace.json> >/dev/null
python -m pytest .claude/skills/aiperf-observability/tests/test_aiperf_to_chrome_trace.py -q
ruff check .claude/skills/aiperf-observability/scripts/aiperf_to_chrome_trace.py \
  .claude/skills/aiperf-observability/tests/test_aiperf_to_chrome_trace.py
```

报告时给出：输入记录数、phase 过滤、配置并发、生成的 slot 数、tree 数、输出路径，以及权限或缺失字段问题。
