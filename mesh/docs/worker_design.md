# Worker 子系统设计

Worker 子系统是 ATOM Mesh 的核心模块，负责后端推理服务的抽象、注册、管理和运维。本文档通过 Mermaid 图详细介绍整个 Worker 子系统的设计原理。

---

## 1. 整体架构总览

```mermaid
graph TB
    subgraph "Worker 子系统"
        WT["Worker Trait<br/>核心抽象接口"]
        WB["WorkerBuilder<br/>构建器模式"]
        WR["WorkerRegistry<br/>注册中心 (6个索引)"]
        WS["WorkerService<br/>CRUD 业务层"]
        WM["WorkerManager<br/>批量运维操作"]
        HC["HealthChecker<br/>后台健康检查"]
        LM["LoadMonitor<br/>后台负载采集"]
    end

    WB -->|"构建"| WT
    WT -->|"注册到"| WR
    WS -->|"操作"| WR
    WM -->|"读取"| WR
    HC -->|"遍历检查"| WR
    LM -->|"采集负载"| WR
    LM -->|"更新"| PR["PolicyRegistry<br/>路由策略"]

    Router["Router 路由层"] -->|"查询 Worker"| WR
    Router -->|"选择策略"| PR
    API["HTTP/gRPC API"] -->|"增删改查"| WS
```

---

## 2. Worker Trait 与两种实现

Worker Trait 定义了 ~30 个方法，是所有后端服务的统一抽象。有两种实现：

```mermaid
classDiagram
    class Worker {
        <<trait>>
        +url() str
        +worker_type() WorkerType
        +connection_mode() ConnectionMode
        +is_healthy() bool
        +set_healthy(bool)
        +check_health_async()
        +load() usize
        +increment_load()
        +decrement_load()
        +circuit_breaker() CircuitBreaker
        +is_available() bool
        +model_id() str
        +dp_rank() Option~usize~
        +get_grpc_client()
    }

    class BasicWorker {
        -metadata: WorkerMetadata
        -load_counter: AtomicUsize
        -processed_counter: AtomicUsize
        -healthy: AtomicBool
        -circuit_breaker: CircuitBreaker
        -grpc_client: OnceCell
        +normalised_url() str
    }

    class DPAwareWorker {
        -base_worker: BasicWorker
        -dp_rank: usize
        -dp_size: usize
        -base_url: String
        +is_dp_aware() true
        +dp_rank() usize
        +dp_size() usize
    }

    Worker <|.. BasicWorker : 实现
    Worker <|.. DPAwareWorker : 实现
    DPAwareWorker o-- BasicWorker : 包装委托

    class WorkerType {
        <<enum>>
        Regular
        Prefill(bootstrap_port)
        Decode
    }

    class ConnectionMode {
        <<enum>>
        Http
        Grpc(port)
    }

    BasicWorker --> WorkerType
    BasicWorker --> ConnectionMode
```

**关键设计**：
- `BasicWorker` 使用 `AtomicUsize` / `AtomicBool` 实现无锁计数器，高并发下零竞争
- `DPAwareWorker` 包装 `BasicWorker`，URL 格式为 `base_url@dp_rank`，用于数据并行路由
- `CircuitBreaker` 实现熔断保护，防止故障 Worker 拖垮整个系统

---

## 3. WorkerBuilder 构建流程

使用 Builder 模式创建 Worker，避免构造函数参数过多：

```mermaid
flowchart LR
    A["BasicWorkerBuilder::new(url)"] --> B[".worker_type(Prefill)"]
    B --> C[".connection_mode(Grpc)"]
    C --> D[".model_id('qwen3')"]
    D --> E[".health_config(...)"]
    E --> F[".circuit_breaker_config(...)"]
    F --> G[".build()"]
    G --> H["BasicWorker"]

    I["DPAwareWorkerBuilder::new(url)"] --> J[".dp_rank(0)"]
    J --> K[".dp_size(4)"]
    K --> L[".worker_type(Regular)"]
    L --> M[".build()"]
    M --> N["DPAwareWorker"]
    N -.->|"内部包含"| H2["BasicWorker"]
```

---

## 4. WorkerRegistry 六索引设计

WorkerRegistry 是 Worker 子系统的核心数据结构，维护 6 个 `DashMap` 索引实现多维度 O(1) 查询：

```mermaid
graph TB
    subgraph "WorkerRegistry — 6 个并发索引"
        W["workers<br/>DashMap&lt;WorkerId, Worker&gt;<br/>主存储"]
        U["url_to_id<br/>DashMap&lt;String, WorkerId&gt;<br/>URL → ID 映射"]
        M["model_index<br/>DashMap&lt;String, Arc&lt;[Worker]&gt;&gt;<br/>模型 → Worker 列表<br/>⚡ Copy-on-Write 快照"]
        H["hash_rings<br/>DashMap&lt;String, HashRing&gt;<br/>模型 → 一致性哈希环"]
        T["type_workers<br/>DashMap&lt;WorkerType, Vec&lt;Id&gt;&gt;<br/>类型索引"]
        C["connection_workers<br/>DashMap&lt;ConnMode, Vec&lt;Id&gt;&gt;<br/>连接模式索引"]
    end

    REG["register(worker)"] --> W
    REG --> U
    REG --> M
    REG --> H
    REG --> T
    REG --> C

    Q1["get_by_model(id)"] --> M
    Q2["get_by_url(url)"] --> U --> W
    Q3["get_by_type(type)"] --> T --> W
    Q4["get_hash_ring(model)"] --> H
    Q5["get_by_connection(mode)"] --> C --> W

    style M fill:#e8f5e9,stroke:#2e7d32
    style H fill:#e3f2fd,stroke:#1565c0
```

---

## 5. Copy-on-Write 模型索引（热路径优化）

`model_index` 是请求路由的热路径，使用 `Arc<[Arc<dyn Worker>]>` 不可变快照实现无锁读取：

```mermaid
sequenceDiagram
    participant Req as 请求线程 (读)
    participant MI as model_index
    participant Reg as 注册操作 (写)

    Note over MI: 当前快照: Arc → [W1, W2, W3]

    Req->>MI: get_by_model("qwen3")
    MI-->>Req: Arc::clone (原子引用计数 +1)
    Note over Req: 无锁读取，零竞争

    Reg->>MI: 注册新 Worker W4
    Note over Reg: 1. 读取旧快照 [W1,W2,W3]
    Note over Reg: 2. 创建新 Vec [W1,W2,W3,W4]
    Note over Reg: 3. 转为 Arc<[...]> 新快照
    Reg->>MI: 替换快照指针

    Note over MI: 新快照: Arc → [W1, W2, W3, W4]
    Note over Req: 旧快照仍然有效<br/>直到引用计数归零
```

---

## 6. 一致性哈希环 (HashRing)

每个模型维护一个 HashRing，用于 ConsistentHash 策略的 O(log n) Worker 选择：

```mermaid
graph LR
    subgraph "HashRing 结构"
        direction TB
        R["环形空间 0 ~ 2^64"]
        V1["Worker A<br/>150 个虚拟节点"]
        V2["Worker B<br/>150 个虚拟节点"]
        V3["Worker C<br/>150 个虚拟节点"]
    end

    Key["routing_key"] -->|"blake3 hash"| Pos["环上位置"]
    Pos -->|"二分查找<br/>O(log n)"| Hit["顺时针最近节点"]
    Hit -->|"健康检查"| Result{"healthy?"}
    Result -->|"是"| OK["选中该 Worker"]
    Result -->|"否"| Next["继续顺时针<br/>跳过已检查 URL"]
```

**设计要点**：
- 每个 Worker 150 个虚拟节点，保证均匀分布
- blake3 哈希，跨 Rust 版本稳定
- 增删 Worker 时只有 ~1/N 的 key 需要重新分配

---

## 7. WorkerService CRUD 操作

WorkerService 是 HTTP API 和 WorkerRegistry 之间的业务逻辑层，所有写操作通过 JobQueue 异步执行：

```mermaid
sequenceDiagram
    participant Client as HTTP Client
    participant API as API Handler
    participant WS as WorkerService
    participant JQ as JobQueue
    participant WR as WorkerRegistry

    Note over Client,WR: 创建 Worker
    Client->>API: POST /workers
    API->>WS: create_worker(config)
    WS->>JQ: submit(RegisterJob)
    JQ-->>WS: job_id
    WS-->>API: 202 Accepted
    API-->>Client: {"status":"accepted"}
    JQ->>WR: register(worker)

    Note over Client,WR: 查询 Worker
    Client->>API: GET /workers/{id}
    API->>WS: get_worker(id)
    WS->>WR: get(worker_id)
    WR-->>WS: Worker
    WS-->>API: WorkerInfo
    API-->>Client: 200 OK

    Note over Client,WR: 删除 Worker
    Client->>API: DELETE /workers/{id}
    API->>WS: delete_worker(id)
    WS->>JQ: submit(RemoveJob)
    JQ-->>WS: job_id
    WS-->>API: 202 Accepted
    JQ->>WR: remove(worker_id)
```

---

## 8. WorkerManager 批量运维

WorkerManager 提供静态方法，对所有 Worker 进行并行批量操作（fan-out 模式）：

```mermaid
flowchart TB
    subgraph "fan_out 并行请求模式"
        WR["WorkerRegistry<br/>获取所有 Worker"] --> Split["拆分为独立请求"]
        Split --> F1["Worker 1<br/>GET /endpoint"]
        Split --> F2["Worker 2<br/>GET /endpoint"]
        Split --> F3["Worker 3<br/>GET /endpoint"]
        Split --> FN["Worker N<br/>GET /endpoint"]
        F1 --> Merge["buffer_unordered(32)<br/>最多 32 并发"]
        F2 --> Merge
        F3 --> Merge
        FN --> Merge
        Merge --> Result["汇总结果"]
    end

    subgraph "三种批量操作"
        A["flush_cache_all<br/>清空所有 Worker 缓存"]
        B["get_all_worker_loads<br/>采集所有 Worker 负载"]
        C["get_engine_metrics<br/>采集并聚合指标"]
    end

    Result --> A
    Result --> B
    Result --> C
```

---

## 9. 两个后台任务：HealthChecker & LoadMonitor

```mermaid
flowchart TB
    subgraph HC["HealthChecker (后台 tokio task)"]
        direction TB
        HC1["定时触发<br/>每 N 秒一次"] --> HC2["遍历 WorkerRegistry<br/>获取所有 Worker"]
        HC2 --> HC3["并行 health check<br/>join_all(futures)"]
        HC3 --> HC4{"通过?"}
        HC4 -->|"连续成功 >= 阈值"| HC5["set_healthy(true)"]
        HC4 -->|"连续失败 >= 阈值"| HC6["set_healthy(false)"]
        HC3 -.->|"跳过"| HC7["disable_health_check<br/>的 Worker"]
    end

    subgraph LM["LoadMonitor (后台 tokio task)"]
        direction TB
        LM1["定时触发<br/>每 N 秒一次"] --> LM2{"存在 PowerOfTwo<br/>策略?"}
        LM2 -->|"否"| LM3["跳过本轮"]
        LM2 -->|"是"| LM4["get_all_worker_loads<br/>采集负载"]
        LM4 --> LM5["更新所有<br/>PowerOfTwo 策略"]
        LM5 --> LM6["watch::send(loads)<br/>广播负载数据"]
    end

    WR["WorkerRegistry"] --> HC
    WR --> LM
    LM --> PR["PolicyRegistry"]

    style HC fill:#fff3e0,stroke:#e65100
    style LM fill:#e8eaf6,stroke:#283593
```

---

## 10. 请求处理完整流程

一个请求从进入到选中 Worker 的完整路径：

```mermaid
sequenceDiagram
    participant Client as 客户端
    participant Router as Router
    participant WR as WorkerRegistry
    participant Policy as Policy
    participant Worker as Worker

    Client->>Router: POST /v1/chat/completions
    Router->>WR: get_by_model("qwen3")
    WR-->>Router: Arc<[W1, W2, W3]> (无锁快照)

    Router->>Policy: select(workers, request)
    Note over Policy: RoundRobin / LeastLoad /<br/>PowerOfTwo / ConsistentHash

    Policy-->>Router: 选中 Worker W2

    Router->>Worker: is_available()?
    Note over Worker: is_healthy() &&<br/>circuit_breaker.can_execute()

    alt Worker 可用
        Router->>Worker: increment_load()
        Router->>Worker: 转发请求
        Worker-->>Router: 响应
        Router->>Worker: decrement_load()
        Router->>Worker: record_outcome(true)
        Router-->>Client: 200 OK
    else Worker 不可用
        Router->>Policy: 重新选择
        Note over Policy: 跳过不可用 Worker
    end
```

---

## 11. 组件间关系总图

```mermaid
graph TB
    subgraph "外部接口"
        API["HTTP/gRPC API"]
        CLI["CLI 启动参数"]
    end

    subgraph "构建层"
        CLI --> RCB["RouterConfigBuilder"]
        RCB --> RC["RouterConfig"]
        RC --> WB["WorkerBuilder"]
    end

    subgraph "核心层"
        WB -->|"build()"| W["Worker 实例"]
        W -->|"register()"| WR["WorkerRegistry<br/>(6 索引 + HashRing)"]
        API -->|"CRUD"| WS["WorkerService"]
        WS -->|"通过 JobQueue"| WR
    end

    subgraph "运维层"
        WR --> HC["HealthChecker<br/>后台健康检查"]
        WR --> LM["LoadMonitor<br/>后台负载采集"]
        WR --> WM["WorkerManager<br/>fan-out 批量操作"]
    end

    subgraph "路由层"
        WR -->|"查询 Worker"| Router
        LM -->|"更新负载"| PR["PolicyRegistry"]
        PR -->|"选择策略"| Router
    end

    Router -->|"转发请求"| W

    style WR fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px
    style W fill:#e3f2fd,stroke:#1565c0,stroke-width:2px
```

---

## 关键设计总结

| 组件 | 职责 | 核心技术 |
|------|------|----------|
| **Worker Trait** | 后端服务统一抽象 | async_trait, ~30 方法 |
| **BasicWorker** | 标准实现 | AtomicUsize/AtomicBool 无锁计数 |
| **DPAwareWorker** | 数据并行路由 | 包装委托 + `url@rank` 格式 |
| **WorkerBuilder** | 流式构建 Worker | Builder 模式，14+ 可配置字段 |
| **WorkerRegistry** | 多维索引注册中心 | 6 个 DashMap + Copy-on-Write 快照 |
| **HashRing** | 一致性哈希 | blake3 + 150 虚拟节点 + O(log n) 查找 |
| **WorkerService** | CRUD 业务逻辑 | JobQueue 异步写 + 同步读 |
| **WorkerManager** | 批量运维操作 | fan_out + buffer_unordered(32) |
| **HealthChecker** | 后台健康巡检 | tokio::spawn + join_all 并行检查 |
| **LoadMonitor** | 后台负载采集 | watch channel 广播 + 仅更新 PowerOfTwo |
