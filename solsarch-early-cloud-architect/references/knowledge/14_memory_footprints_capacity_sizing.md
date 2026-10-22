# SolsBrain — Chapter 14: Workload Memory Footprints & Capacity Sizing Guide

**Domain:** Runtime Memory Footprints, Language Heap Dynamics, Cache Sizing & OOM Prevention  
**Target Clouds:** AWS, Microsoft Azure, Google Cloud Platform (GCP), Kubernetes, Containers  
**Target Engine:** SolsArch Analyzer (Agent 6 Cloud Decider, Core Validator, Memory Sizer)  
**Version:** 2.0 (Exhaustive Technical Specification)

---

## 1. Runtime Memory Footprints Across Tech Stacks

Understanding the baseline memory requirements of programming language runtimes is essential for SolsArch's Cloud Decider when selecting container sizes (e.g., Fargate 0.25 vCPU / 0.5 GB vs. 1 vCPU / 2 GB):

```
Runtime Memory Baseline (RSS at Idle / Boot):
  Rust / Go:         [ 8 – 25 MB ]
  Python (FastAPI):  [ 45 – 90 MB ]
  Node.js (Express): [ 60 – 120 MB ]
  Node.js (Next.js): [ 120 – 250 MB ]
  Java (Spring):     [ 350 – 750 MB ]
```

### 1.1 Language Runtime Memory Dynamics
| Runtime | Base Memory (RSS) | Per-Request Overhead | Primary Memory Risk | SolsArch Recommended Container Sizing |
|---|:---:|:---:|---|---|
| **Go** | ~10–25 MB | ~2 KB per goroutine | Goroutine leaks (unbounded channels) | **0.25 vCPU / 0.5 GB RAM** (Micro) |
| **Rust** | ~8–20 MB | Minimal (Zero-cost abstractions) | Memory allocation in tight loops | **0.25 vCPU / 0.5 GB RAM** (Micro) |
| **Python (FastAPI / Uvicorn)** | ~50–85 MB | ~15–30 MB per worker process | Pandas/NumPy in-memory buffers, unclosed DB sessions | **0.50 vCPU / 1.0 GB RAM** (Small) |
| **Node.js (Express / Fastify)** | ~65–110 MB | V8 heap allocations | Event emitter leaks, unhandled promise rejections | **0.50 vCPU / 1.0 GB RAM** (Small) |
| **Node.js (Next.js SSR)** | ~140–280 MB | Dynamic page rendering & cache | SSR payload caching in memory | **1.00 vCPU / 2.0 GB RAM** (Medium) |
| **Java (Spring Boot / JVM)** | ~400–800 MB | 1 MB per OS thread (`-Xss1m`) | Off-heap Metaspace, GC pause overhead | **2.00 vCPU / 4.0 GB RAM** (Large) |

---

## 2. Database Memory Architecture & Tuning

### 2.1 PostgreSQL (Amazon RDS / Aurora) Memory Allocation
PostgreSQL memory is partitioned into shared buffers and private backend memory:

```
[ Total System RAM on Database Host ]
  │
  ├── 1. Shared Buffers (25% of Total RAM)
  │     └── Primary cache for table data and index pages loaded from disk
  │
  ├── 2. OS Page Cache (50% of Total RAM)
  │     └── Cached disk blocks managed directly by the Linux kernel
  │
  ├── 3. Maintenance Work Memory (maintenance_work_mem)
  │     └── Dedicated buffer for VACUUM, CREATE INDEX, and ALTER TABLE (~512 MB to 2 GB)
  │
  └── 4. Working Memory (work_mem) * MAX CONNECTIONS
        └── Memory allocated per SORT, HASH, or JOIN operation per query backend!
```

#### The Work Memory Mathematical Trap:
$$\text{Max Potential RAM Consumption} = \text{shared\_buffers} + (\text{work\_mem} \times \text{max\_connections} \times \text{operations\_per\_query})$$
- *Anti-Pattern*: Setting `work_mem = 64 MB` with `max_connections = 500`. If 200 concurrent queries execute a complex join with 2 sort operations, peak memory demand is:
  $$\text{RAM} = 64\text{ MB} \times 200 \times 2 = 25.6\text{ GB}$$
  This exhausts server RAM and causes an immediate kernel **Out-Of-Memory (OOM) panic** and database crash.
- *Remediation*: Keep `work_mem` modest (4–16 MB) and enforce connection pooling (PgBouncer / AWS RDS Proxy).

---

## 3. In-Memory Cache Sizing: Amazon ElastiCache (Redis)

### 3.1 Redis Memory Budgeting & The 25% Reserve Rule
When Redis performs background snapshots (`BGSAVE`) or replication synchronization, the operating system forks the parent process. The child process uses **Copy-on-Write (COW)**:

```
[ Total ElastiCache Node RAM ]
  │
  ├── 75%: Active Redis Dataset (maxmemory)
  │     └── Key-value data, hash sets, lists, strings
  │
  └── 25%: Reserved Memory (reserved-memory-percent = 25)
        └── Dedicated buffer for Copy-on-Write page modifications during BGSAVE
```
*Rule*: Never configure Redis `maxmemory` to 100% of physical instance RAM. If write activity is heavy during a snapshot, Copy-on-Write memory consumption will trigger Linux OOM termination.

### 3.2 Redis Eviction Policies (`maxmemory-policy`)
| Eviction Policy | Behavior when Memory is Full | SolsArch Architectural Recommendation |
|---|---|---|
| **`volatile-lru`** | Evicts least recently used keys **with an explicit TTL** | **Default Recommendation** for shared caching layers |
| **`allkeys-lru`** | Evicts least recently used keys across **all keys** | Best for pure read-through web session caches |
| **`volatile-ttl`** | Evicts keys with the **shortest remaining TTL** | Ideal for expiring auth tokens and rate-limiting counters |
| **`noeviction`** | Returns an error on new write commands (`OOM command not allowed`) | **Mandatory** when Redis is used as an authoritative primary database |

---

## 4. Container Sizing & Linux OOM Killer Mechanics

### 4.1 Requests vs. Limits in Kubernetes and ECS
```
[ Node Physical Capacity ]
  │
  ├── Memory Request (Guaranteed Minimum)
  │     └── Kubernetes scheduler uses this to place the pod on a node with capacity.
  │
  └── Memory Limit (Hard Ceiling)
        └── If the container's Resident Set Size (RSS) exceeds this ceiling,
            the Linux kernel terminates the container immediately with OOMKilled (Exit Code 137).
```

### 4.2 SolsArch Sizing Invariant: The 25% Buffer
To prevent containers from oscillating in CrashLoopBackOff:

$$\text{Container Memory Limit} \ge \text{Peak Measured Application Memory} \times 1.25$$

---

## 5. Formal SolsArch Memory & Capacity Validation Rules

| Rule Code | Severity | Trigger Condition | Remediation Guidance |
|---|:---:|---|---|
| `MEM-JVM-001` | **HIGH** | Java / JVM container has memory limit set without `-XX:MaxRAMPercentage` or `-Xmx` | Set `-XX:MaxRAMPercentage=75.0` to prevent JVM from ignoring container memory ceilings |
| `MEM-RDS-001` | **CRITICAL** | PostgreSQL database allows `max_connections > 200` without RDS Proxy or PgBouncer | Attach AWS RDS Proxy to pool connections and prevent OOM backend exhaustion |
| `MEM-RED-001` | **HIGH** | Redis cluster has `reserved-memory-percent` set to 0% | Configure `reserved-memory-percent: 25` to reserve memory for BGSAVE Copy-on-Write |
| `MEM-CNT-001` | **CRITICAL** | Container definition specifies CPU reservation but omits hard memory limit | Define an explicit memory limit to prevent runaway memory leaks from evicting adjacent containers |
---

## 6. Comprehensive Sizing Calculation Worksheets & Production Formulas (from SolsRef)

### 6.1 Amazon ElastiCache (Redis) Memory Sizing Formula
To size Redis cluster memory accurately without triggering OOM eviction crashes:
$$\text{Total Cluster RAM} = \frac{\text{Raw Data Working Set Size} \times (1 + \text{Growth Buffer})}{\text{Usable Memory Fraction} \times (1 - \text{Reserved Memory})}$$
- **Key Production Variables**:
  - `Raw Data Working Set Size`: Total estimated volume of keys and values.
  - `Metadata Overhead`: Redis requires ~100–150 bytes of RAM per key for internal data structures (pointers, dict entries, expiry timers).
  - `Reserved Memory`: Always configure `reserved-memory-percent` to **25%** to reserve memory for Redis background snapshotting (BGSAVE) and replication buffer synchronization during node failovers.
  - `Growth Buffer`: Recommended 20% for unexpected traffic spikes.

### 6.2 Amazon Kinesis Shard Sizing Formula
$$\text{Required Shards } (S) = \max \left( \left\lceil \frac{\text{Peak Ingress Data Rate (MB/s)}}{1 \text{ MB/s}} \right\rceil, \left\lceil \frac{\text{Peak Ingress Records per Second}}{1000 \text{ records/s}} \right\rceil \right)$$
- If consumers use **Standard Consumers**, ensure total consumer egress rate does not exceed $2 \text{ MB/s}$ per shard. If multiple downstream applications consume simultaneously, deploy **Enhanced Fan-Out (EFO)** consumers (providing dedicated $2 \text{ MB/s}$ per consumer per shard).

### 6.3 Amazon DynamoDB Capacity Planning Formula
- **Read Capacity Units (RCU)**:
  $$\text{RCU} = \left\lceil \frac{\text{Item Size (KB)}}{4 \text{ KB}} \right\rceil \times \text{Strongly Consistent Reads/sec} + \left( 0.5 \times \left\lceil \frac{\text{Item Size (KB)}}{4 \text{ KB}} \right\rceil \times \text{Eventually Consistent Reads/sec} \right)$$
- **Write Capacity Units (WCU)**:
  $$\text{WCU} = \left\lceil \frac{\text{Item Size (KB)}}{1 \text{ KB}} \right\rceil \times \text{Writes per second}$$
- *Decision Threshold*: If traffic variance exceeds $4\times$ with unpredictable traffic patterns, select **On-Demand Mode**. If traffic is predictable with consistent baseline load, select **Provisioned Mode with Auto-Scaling** to achieve 50–70% cost savings.
---

## 7. Storage Gateway, Kinesis Sharding & OpenSearch Capacity Sizing Worksheets (from SolsRef)

### 7.1 Kinesis Data Streams Shard Capacity Mathematics
- **Shard Baseline Metrics**:
  - **Write Throughput**: $1 \text{ MB/sec}$ or $1,000 \text{ records/sec}$ per shard.
  - **Standard Read Throughput**: $2 \text{ MB/sec}$ shared across all standard consumers per shard.
  - **Enhanced Fan-Out (EFO) Read Throughput**: $2 \text{ MB/sec}$ *dedicated per consumer* per shard.
- **Sizing Formula**:
  $$\text{Required Shards} = \max \left( \left\lceil \frac{\text{Ingestion Rate (MB/sec)}}{1 \text{ MB/sec}} \right\rceil, \left\lceil \frac{\text{Ingestion Records/sec}}{1000} \right\rceil, \left\lceil \frac{\text{Egress Rate (MB/sec)}}{2 \text{ MB/sec}} \right\rceil \right)$$
  - *Example*: Workload ingest $12 \text{ MB/sec}$ and $8,500 \text{ records/sec}$, read by 2 standard consumers ($24 \text{ MB/sec}$ egress).
  - Shards based on MB in: $\lceil 12 / 1 \rceil = 12$.
  - Shards based on records: $\lceil 8500 / 1000 \rceil = 9$.
  - Shards based on MB out: $\lceil 24 / 2 \rceil = 12$.
  - **Result**: Cluster requires **12 shards**.

### 7.2 Amazon OpenSearch Shard & Storage Capacity Sizing
- **Recommended Shard Size Targets**:
  - Search Workloads: **10 GB to 30 GB** per shard.
  - Log Analytics Workloads: **30 GB to 50 GB** per shard.
- **Total Storage Requirement Formula**:
  $$\text{Total Disk Storage} = \text{Daily Ingest (GB)} \times \text{Retention (Days)} \times (1 + \text{Replica Count}) \times 1.45$$
  - *Note*: $1.45$ factor accounts for OpenSearch indexing overhead (10%), Linux OS reserved space (5%), and OpenSearch watermark headroom (20% to prevent read-only lock).
