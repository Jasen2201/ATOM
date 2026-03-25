# DeepSeek-R1 FP8 PD Disaggregation Benchmark Report

**Date:** 2026-03-24
**Model:** DeepSeek-R1 (671B MoE), FP8
**Workload:** ISL=8192 / OSL=1024 / Concurrency=32 / 320 prompts

---

## 1. Benchmark Results

### 1.1 Throughput

| Metric | Value |
|--------|-------|
| **Output Throughput** | **250.52 tok/s** |
| **Total Token Throughput** | **2254.68 tok/s** |
| Request Throughput | 0.24 req/s |
| Output per Decode GPU (/ 8) | 31.3 tok/s/gpu |
| Total per All GPU (/ 16) | 141.0 tok/s/gpu |

### 1.2 Latency

| Metric | Mean | Median | P99 |
|--------|------|--------|-----|
| TTFT (ms) | 104,578 | 107,129 | 127,393 |
| TPOT (ms) | 20.03 | 20.64 | 22.14 |
| ITL (ms) | 20.03 | 21.67 | 25.62 |
| E2EL (ms) | 125,067 | 128,127 | 146,865 |

### 1.3 Request Statistics

| Metric | Value |
|--------|-------|
| Total Requests | 320 |
| Successful | 320 (100%) |
| Benchmark Duration | 1308.0 s (~21.8 min) |
| Total Input Tokens | 2,621,440 |
| Total Output Tokens | 327,680 |

## 2. vs InferenceX MI355X (Conc=32)

**Reference:** InferenceX (SemiAnalysis), `dsr1-fp8-mi355x-sglang-disagg`, ISL=8192/OSL=1024, Conc=32

### 2.1 配置差异

| Item | InferenceX | Our Setup |
|------|-----------|-----------|
| Prefill | TP=4, **4 GPUs** | TP=8, **8 GPUs** |
| Total GPUs | **12** (4P + 8D) | **16** (8P + 8D) |
| KV Transfer | **MoRI** (AMD proprietary) | **Mooncake** (open-source) |
| RDMA Devices | **8** (rdma0-7, 同子网) | **1** (ionic_0, 跨子网失败) |
| Chunked Prefill | **262,144** | **16,384** |
| CUDA Graph BS | **1-128** | **1-32** |

### 2.2 吞吐差距

| Metric | Our Result | InferenceX | Gap |
|--------|-----------|-----------|-----|
| Output per Decode GPU | 31.3 tok/s/gpu | 177.6 tok/s/gpu | **5.7x** |
| Output Throughput | 250.52 tok/s | 1,420.7 tok/s | **5.7x** |
| Total per All GPU | 141.0 tok/s/gpu | 1,064.5 tok/s/gpu | **7.5x** |
| Total Token Throughput | 2,254.68 tok/s | 12,774.2 tok/s | **5.7x** |

## 3. 差距原因与优化方向

| # | 差异项 | InferenceX | 我们 | 影响 | 优化方向 |
|---|--------|-----------|------|------|---------|
| 1 | RDMA 设备数 | 8 (同子网) | 1 (跨子网失败) | KV 传输带宽 ~8x 差距 | 解决跨子网路由, 启用多设备 |
| 2 | KV Transfer | MoRI (FP8 dispatch, shmem isolation) | Mooncake | 传输效率 | 评估 MoRI 适配 |
| 3 | Chunked Prefill | 262,144 | 16,384 (AITER topK 限制) | Prefill 吞吐 16x | 突破 ATOM OOT AITER 16K 限制 |
| 4 | CUDA Graph BS | 1-128 | 1-32 (hipIpc 错误) | 高并发 decode 无 graph | 排查 hipIpcGetMemHandle 错误 |
| 5 | Prefill TP | 4 (省 GPU) | 8 | 多用 4 GPU | 测试 TP=4 稳定性 |

## 4. Raw Benchmark JSON

```
File: logs/dsr1_fp8_disagg_isl8192_osl1024_conc32.json
Date: 2026-03-24 10:57:11 UTC
```
