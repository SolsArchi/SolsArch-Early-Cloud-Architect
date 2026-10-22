# SolsBrain — Chapter 2: Compute, Containers & Serverless (Deep-Dive Edition)

**Domain:** Virtual Compute, Elastic Auto Scaling, Container Orchestration & Serverless FaaS  
**Target Clouds:** AWS, Microsoft Azure, Google Cloud Platform (GCP)  
**Target Engine:** SolsArch Analyzer (Agent 6 Cloud Decider, Agent 1 Code Reader, Core Validator)  
**Version:** 2.0 (Exhaustive Technical Specification)

---

## 1. Virtual Machine Compute: Amazon EC2

### 1.1 Detailed Instance Family Sizing & Selection Matrix
| Family | Sub-Type | CPU Architecture | vCPU : RAM Ratio | Network Bandwidth | Typical Production Use Case |
|---|---|---|---|---|---|
| **`m7g` / `m6g`** | General Purpose | AWS Graviton3 (ARM64) | 1 vCPU : 4 GiB | Up to 30 Gbps | Standard web APIs, backend microservices, moderate caching |
| **`m6i` / `m5`** | General Purpose | Intel Xeon (x86_64) | 1 vCPU : 4 GiB | Up to 50 Gbps | Legacy x86 binaries, Windows Server applications |
| **`c7g` / `c6i`** | Compute Optimized | Graviton3 / Intel Xeon | 1 vCPU : 2 GiB | Up to 30 Gbps | Video transcoding, algorithmic trading, batch pipelines |
| **`r7g` / `r6i`** | Memory Optimized | Graviton3 / Intel Xeon | 1 vCPU : 8 GiB | Up to 50 Gbps | In-memory Redis/Memcached, distributed Spark nodes |
| **`x2gd` / `u-12tb1`** | High Memory | Graviton2 / Intel Xeon | 1 vCPU : 16–32 GiB | Up to 100 Gbps | Enterprise in-memory databases (SAP HANA, large graph DBs) |
| **`i4i` / `i3en`** | Storage Optimized | Intel Xeon (NVMe SSD) | 1 vCPU : 8 GiB | Up to 100 Gbps | High-IOPS databases (Apache Cassandra, ScyllaDB, Kafka) |
| **`g5` / `p4d`** | Accelerated / GPU | NVIDIA A10G / A100 | GPU + vCPU | Up to 400 Gbps | Deep learning model training, LLM inference, 3D simulation |

### 1.2 Spot Instance Fleet Engineering & Termination Handling
- **Rebalance Recommendation & Interruption Warnings**:
  - AWS emits an `EC2 Spot Instance Interruption Notice` via Amazon EventBridge and instance metadata (`http://169.254.169.254/latest/meta-data/spot/instance-action`) exactly **2 minutes** before termination.
  - **Spot Instance Rebalance Recommendation**: Emitted minutes earlier when an instance is at elevated risk of interruption, allowing graceful draining before termination.
- **Graceful Draining Architecture**:
```mermaid
sequenceDiagram
  autonumber
  participant EC2 as Spot Instance
  participant EventBridge as Amazon EventBridge
  participant ALB as Target Group
  participant Drainer as Lambda Drainer

  EC2->>EventBridge: Spot Interruption Notice (2-min warning)
  EventBridge->>Drainer: Trigger Draining Function
  Drainer->>ALB: Deregister Target (Connection Draining = 90s)
  ALB->>ALB: Stop new requests; allow in-flight HTTP requests to complete
  EC2->>EC2: Flush in-flight queue jobs / save checkpoint to S3
  Note over EC2: 120 seconds elapse -> Instance Terminated
```

### 1.3 Placement Group Topology Constraints
- **Cluster**:
  - Single AZ only. Cannot span multiple AZs or VPCs.
  - SolsArch rule: Recommended for MPI / HPC, but flagged with a high-availability warning for user-facing production services.
- **Spread**:
  - Max 7 running instances per Availability Zone per group.
  - Guarantees instances do not share common power sources or network racks.
- **Partition**:
  - Up to 7 partitions per AZ. Each partition contains multiple instances.
  - SolsArch rule: Match partition count to Kafka partition or Cassandra replica count.

---

## 2. Elastic Auto Scaling Groups (ASG)

### 2.1 Cooldown, Warmup & Health Check Grace Periods
- **Default Cooldown Period (Default: 300s)**:
  - Ensures the ASG does not launch or terminate additional instances before the previous scaling activity's effects can be measured.
- **Instance Warmup**:
  - For Step Scaling and Target Tracking, specifies the time required for a newly launched instance to finish application boot and warm up its caches before it begins contributing to aggregated CloudWatch metrics.
- **Health Check Grace Period**:
  - Prevents the ASG from terminating a newly booted instance while its Docker daemon, JVM, or Node runtime is initializing.
  - *SolsArch Standard*: 300 seconds for containerized microservices; 600 seconds for heavy monolithic applications.

---

## 3. Container Orchestration: Amazon ECS & Amazon EKS

### 3.1 ECS Task Definition Deep Dive (Fargate Constraints)
Fargate enforces strict, coupled CPU and Memory sizing allocations:

| vCPU Allocation | Allowed Memory Allocations (GB) | SolsArch Microservice Sizing Guide |
|---|---|---|
| **0.25 vCPU (256 units)** | 0.5 GB, 1 GB, 2 GB | Lightweight background workers, cron dispatchers |
| **0.50 vCPU (512 units)** | 1 GB, 2 GB, 3 GB, 4 GB | Standard Node.js / Go microservices |
| **1.00 vCPU (1024 units)** | 2 GB to 8 GB (1 GB increments) | Python FastAPI, Ruby on Rails, medium traffic APIs |
| **2.00 vCPU (2048 units)** | 4 GB to 16 GB (1 GB increments) | JVM / Spring Boot services, local data parsers |
| **4.00 vCPU (4096 units)** | 8 GB to 30 GB (1 GB increments) | Memory-intensive batch compute, local embedding models |

#### Production ECS Fargate Task Definition Example:
```json
{
  "family": "solsarch-api-task",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "1024",
  "memory": "2048",
  "executionRoleArn": "arn:aws:iam::111122223333:role/ECSTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::111122223333:role/AppRuntimeRole",
  "containerDefinitions": [
    {
      "name": "api",
      "image": "111122223333.dkr.ecr.eu-west-1.amazonaws.com/api:v2.4",
      "essential": true,
      "portMappings": [
        {
          "containerPort": 8000,
          "protocol": "tcp"
        }
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "curl -f http://localhost:8000/healthz || exit 1"],
        "interval": 30,
        "timeout": 5,
        "retries": 3,
        "startPeriod": 60
      },
      "secrets": [
        {
          "name": "DATABASE_URL",
          "valueFrom": "arn:aws:secretsmanager:eu-west-1:111122223333:secret:app/db_url-XyZ"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/solsarch-api",
          "awslogs-region": "eu-west-1",
          "awslogs-stream-prefix": "api"
        }
      }
    }
  ]
}
```

---

### 3.2 Amazon EKS Architecture & Karpenter Autoscaling
- **Control Plane Redundancy**: Managed across at least 3 AZs; automated etcd snapshotting and backup.
- **IAM Roles for Service Accounts (IRSA)**:
  - Links a Kubernetes `ServiceAccount` directly to an AWS IAM Role via an OpenID Connect (OIDC) identity provider.
  - Pods assume the role via temporary projection of STS tokens at `/var/run/secrets/eks.amazonaws.com/serviceaccount/token`.
  - Eliminates node-level over-permissioning.
- **Karpenter Next-Gen Autoscaler**:
  - Bypasses traditional Kubernetes Cluster Autoscaler and EC2 Auto Scaling Groups.
  - Directly launches right-sized EC2 instances in response to unscheduled pods in under 45 seconds.

---

## 4. Serverless Compute: AWS Lambda

### 4.1 Memory-to-vCPU Scaling Ratios
AWS Lambda allocates CPU power proportionally to the configured memory:
- **128 MB**: ~0.08 vCPU (ideal for lightweight event triggers).
- **1,769 MB (1.75 GB)**: Exactly **1 full vCPU thread**.
- **10,240 MB (10 GB)**: Maximum allocation (~6 full vCPUs).
- *Optimization Rule*: If a CPU-bound function runs slowly, increasing memory to 1,769 MB grants a full dedicated vCPU and often reduces execution time enough to lower total invocation cost.

### 4.2 Provisioned Concurrency vs. Cold Start Mitigation
```
                     [ Inbound HTTP Request ]
                                │
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
     [ Provisioned Concurrency ]     [ Standard On-Demand ]
     • Environment pre-warmed        • Download container/zip
     • Runtime initialized in RAM    • Start Firecracker microVM
     • First-byte latency: < 15ms    • Run init code outside handler
                                     • First-byte latency: 250ms - 2,500ms
```

---

## 5. Formal SolsArch Compute Validation Rules

| Rule Code | Severity | Condition Trigger | Remediation Guidance |
|---|:---:|---|---|
| `CMP-EC2-001` | **CRITICAL** | Standalone EC2 instance declared without an Auto Scaling Group in production | Wrap compute nodes in a multi-AZ Auto Scaling Group with minimum capacity >= 2 |
| `CMP-ECS-001` | **HIGH** | ECS Task Definition omits container `healthCheck` block | Define an HTTP/CMD health check to enable automated bad-task replacement |
| `CMP-ECS-002` | **HIGH** | ECS Task Execution Role merged with Task Application Role | Separate execution role (ECR/Secrets) from runtime application role (S3/DB) |
| `CMP-LMB-001` | **HIGH** | Lambda function connected to private VPC lacks dedicated subnets across >= 2 AZs | Attach Lambda to at least two private subnets to avoid AZ outage disruptions |
| `CMP-LMB-002` | **MEDIUM** | User-facing synchronous HTTP API function has timeout > 29 seconds | Reduce timeout to <= 29s (API Gateway hard ceiling) or convert to async workflow |
---

## 4. Production Architecture Best Cases & Sizing Comparisons (from SolsRef)

### Best Case 2.1: The 9-Way Cloud Compute Topology Decision Matrix
When architecting applications, SolsArch Analyzer evaluates workloads against this canonical 9-tier compute taxonomy:

| Architecture Topology | Scaling Mechanism | High Availability | Ops Burden | Typical Production Fit |
|---|---|---|---|---|
| **1. EC2 + Elastic IP** | Manual vertical resize | Single Point of Failure (Single AZ) | High (OS, patching, backups) | Legacy monolithic dev/test, non-critical bastion hosts. |
| **2. EC2 + Route 53** | Manual DNS failover | Multi-AZ (Active-Passive) | High (DNS TTL delay on failover) | Simple legacy DR failover for stateful software. |
| **3. ALB + Auto Scaling Group (EC2)** | Target tracking (CPU, ALB Request Count) | High (Multi-AZ, auto-replace unhealthy instances) | Moderate (AMI bake pipelines, golden images) | Enterprise backend APIs, monolithic web apps, custom kernel modules. |
| **4. ALB + ECS on EC2** | ECS Service Auto Scaling + EC2 Auto Scaling | High (Multi-AZ container density) | Moderate (EC2 host fleet management) | High container density workloads requiring local NVMe storage or custom daemonsets. |
| **5. ALB + ECS on Fargate** | ECS Service Auto Scaling (Task CPU/RAM) | High (Multi-AZ serverless tasks) | Low (Zero host patching or OS management) | Standard microservices, scheduled batch jobs, REST APIs. |
| **6. ALB + AWS Lambda** | Concurrency scaling (up to account limit) | Built-in Multi-AZ | Zero | Low-latency HTTP webhooks, migrating micro-endpoints from monolithic ALBs. |
| **7. API Gateway + Lambda** | Built-in request-based autoscaling | Built-in Multi-AZ | Zero | Fully serverless REST/HTTP APIs, mobile backends, event-driven web services. |
| **8. API Gateway + AWS Service Proxy** | Built-in AWS Service integration | Built-in Multi-AZ | Zero | Ultra-high throughput data ingestion directly into SQS, Kinesis, or DynamoDB (no Lambda cold starts/costs). |
| **9. API Gateway + HTTP Backend** | Edge routing facade | Multi-AZ / Hybrid | Low | Enterprise facade, rate limiting, and mTLS termination in front of legacy on-premises HTTP APIs. |

### Best Case 2.2: Resilient Spot Fleet & On-Demand Mixed Fleets with 2-Minute Termination Drain
- **Context & Problem**: Spot instances offer up to 90% cost savings compared to On-Demand, but AWS can terminate them with a 2-minute notice when capacity is reclaimed.
- **Architecture Blueprint**:
```
[ Auto Scaling Group Mixed Instances Policy ]
├── Base On-Demand: 20% (guarantees core service capacity)
└── Spot Allocation: 80% (capacity-optimized-prioritized strategy across 4+ instance types)
        │
        ├── AWS EventBridge Rule:
        │   Pattern: { "source": ["aws.ec2"], "detail-type": ["EC2 Spot Instance Interruption Notice"] }
        │        │
        │        ▼
        │   [ AWS Lambda Termination Handler ]
        │        ├── 1. Put ASG Lifecycle Hook into 'Terminating:Wait'
        │        ├── 2. Deregister instance from ALB Target Group (Connection Draining: 60s)
        │        ├── 3. Flush in-flight job checkpoints & logs to S3/DynamoDB
        │        └── 4. Complete Lifecycle Action -> Clean EC2 Termination
        │
        ▼
[ SQS / Kinesis Worker Fleet ] (Zero dropped tasks, sub-second graceful handoff)
```
- **SolsArch Evaluator Rule**: `CMP-SPT-001` — Workloads utilizing Spot Fleets for batch or worker tiers must implement EventBridge termination notices and graceful connection draining.

### Best Case 2.3: Serverless Ephemeral Cron Jobs vs High-Throughput Batch
- **Cron Tasks (< 15 min)**: Amazon EventBridge Scheduler -> AWS Lambda with dead-letter queue (DLQ) attached. Ideal for nightly database cleanups, cache invalidation, and report generation.
- **Batch Tasks (> 15 min or GPU/HPC)**: AWS Batch with Managed Compute Environments on Fargate Spot or EC2 Spot. Automatically provisions container instances based on queue depth, executes Docker containers with retry policies, and deprovisions compute to zero upon queue drain.
---

## 7. Advanced Compute Execution Lifecycles, Concurrency & Orchestration (from SolsRef)

### 7.1 AWS Lambda Execution Lifecycle & Concurrency Engineering
- **Cold Start vs Warm Start Decomposition**:
  1. **Download Code**: AWS downloads function deployment package from internal S3 repository.
  2. **Start Runtime Environment**: AWS spins up microVM (Firecracker) with chosen runtime (Python, Node.js, Java).
  3. **Initialization (Init Code)**: Executes code outside the event handler function (imports packages, instantiates database connection pools, initializes AWS SDK clients).
  4. **Handler Execution**: Invokes the handler function with the incoming event payload.
  - *Warm Start*: Steps 1–3 are bypassed; the existing microVM is reused directly, slashing execution latency from 1,200ms to < 10ms.
- **Concurrency Controls**:
  - **Unreserved Concurrency**: Pool of execution capacity shared across all functions in an AWS account (default soft limit: 1,000 per region).
  - **Reserved Concurrency**: Guarantees a dedicated maximum execution concurrency for a critical function, while simultaneously capping it to prevent exhausting backend database connection limits.
  - **Provisioned Concurrency**: Pre-allocates and initializes execution microVMs ahead of time (executing steps 1–3 continuously), completely eliminating cold start latency for interactive web applications.
- **Lambda VPC Hyperplane ENI**: Lambda functions connected to private VPC subnets share managed Elastic Network Interfaces (Hyperplane ENIs) across executions. Functions within the same security group and subnet reuse the existing ENI tunnel, resulting in zero ENI allocation delay on invocations.

### 7.2 AWS Step Functions: Standard vs Express Workflows
| Architectural Dimension | Step Functions Standard Workflows | Step Functions Express Workflows |
|---|---|---|
| **Maximum Execution Duration** | Up to **1 Year** | Up to **5 Minutes** |
| **Execution Rate Capacity** | Over 2,000 executions per second | Over **100,000+ executions per second** |
| **Execution Semantics** | **Exactly-once** execution | **At-least-once** (Asynchronous) or **At-most-once** (Synchronous) |
| **Pricing Model** | Charged per state transition ($0.025 per 1,000 transitions) | Charged per execution count + duration + memory consumption |
| **Execution History & Audit** | Full visual execution history in AWS Console, inspectable for 90 days. | Execution details emitted to Amazon CloudWatch Logs. |
| **Optimal Production Fit** | Long-running orchestrations: Human approvals, multi-day payment workflows, ML pipelines. | High-volume IoT telemetry processing, streaming data ingestion, high-throughput microservices. |

### 7.3 AWS Elastic Beanstalk Deployment Strategies
| Deployment Policy | Deployment Mechanism | Downtime | Impact on Capacity | Rollback Speed | Recommended Production Fit |
|---|---|:---:|---|:---:|---|
| **All at Once** | Deploys new version to all instances simultaneously. | **Yes** | 0% capacity during deploy | Slow | Non-production dev/test environments only. |
| **Rolling** | Deploys in batches (e.g. 20% at a time). | No | Capacity reduced by batch size | Slow | Internal applications with spare capacity headroom. |
| **Rolling with Additional Batch** | Launches an extra batch of new instances first before deploying. | No | Full 100% capacity maintained | Slow | Production workloads with strict capacity SLAs. |
| **Immutable** | Launches a temporary parallel Auto Scaling Group with new instances. | No | Full 100% capacity maintained | **Instant** | Mission-critical production releases; zero risk to existing fleet. |
| **Traffic Splitting (Canary)** | Deploys new ASG, routes percentage (e.g. 10%) of user traffic for evaluation. | No | Full 100% capacity maintained | **Instant** | A/B testing, evaluating error metrics on new software revisions. |
---

## 8. EC2 Purchasing Strategies, Placement Groups & Capacity Planning (from SolsRef)

### 8.1 EC2 Pricing & Purchasing Models Decision Matrix
| Model | Commitment | Discount Level | Upfront Options | Best Production Workload Fit |
|---|---|:---:|---|---|
| **On-Demand** | None (per-second billing) | **0%** (Baseline) | None | Unpredictable, short-term workloads, development, spike testing. |
| **Compute Savings Plans** | 1 or 3 Years | Up to **66%** | All Upfront, Partial Upfront, No Upfront | Baseline steady-state compute. Automatically applies across EC2, AWS Fargate, and AWS Lambda regardless of instance family, OS, or AWS Region. |
| **EC2 Instance Savings Plans** | 1 or 3 Years | Up to **72%** | All Upfront, Partial Upfront, No Upfront | Consistent usage within a specific instance family (e.g. `m7g` in `us-east-1`). Allows changing instance size, OS, and tenancy within the family. |
| **Standard Reserved Instances (RI)** | 1 or 3 Years | Up to **72%** | All Upfront, Partial Upfront, No Upfront | Fixed instance attributes; can be bought and sold on the official AWS Reserved Instance Marketplace. |
| **Convertible Reserved Instances** | 1 or 3 Years | Up to **66%** | All Upfront, Partial Upfront, No Upfront | Workloads requiring flexibility to switch instance families, operating systems, or tenancies over a 3-year term. |
| **Spot Instances** | None (interruptible with 2-min warning) | Up to **90%** | None | Stateless web tiers, batch processing, video rendering, Big Data Spark workers, ML model training. |
| **Dedicated Hosts** | 1 or 3 Years / On-Demand | High | Variable | Physical servers dedicated to your use. Required for strict server-bound software licenses (BYOL: Windows Server, SQL Server, Oracle). |
| **Dedicated Instances** | None / RI | Moderate | Extra $2/hr fee | Instances running on hardware dedicated to a single customer account; other accounts never share the physical host. |
| **Capacity Reservations** | None (billed at On-Demand rates) | 0% (unless paired with Savings Plan) | None | Guarantees physical capacity in a specific Availability Zone for disaster recovery or critical launch events. |

### 8.2 EC2 Placement Groups Architecture & Selection Guide
```
1. CLUSTER PLACEMENT GROUP            2. SPREAD PLACEMENT GROUP              3. PARTITION PLACEMENT GROUP
┌───────────────────────────────┐     ┌────────────────────────────────┐     ┌────────────────────────────────┐
│ Availability Zone 1           │     │ Availability Zone 1            │     │ Availability Zone 1            │
│  ┌─────────────────────────┐  │     │ ┌──────┐   ┌──────┐   ┌──────┐ │     │ ┌────────────┐  ┌────────────┐ │
│  │ EC2   EC2   EC2   EC2   │  │     │ │Rack 1│   │Rack 2│   │Rack 3│ │     │ │Partition 1 │  │Partition 2 │ │
│  │  │     │     │     │    │  │     │ │ EC2  │   │ EC2  │   │ EC2  │ │     │ │ EC2   EC2  │  │ EC2   EC2  │ │
│  │ ─┴─────┴─────┴─────┴─   │  │     │ └──────┘   └──────┘   └──────┘ │     │ └────────────┘  └────────────┘ │
│  │ 10 - 100 Gbps Low Latency│ │     │ (Max 7 instances per AZ)       │     │ (Up to 7 partitions per AZ)    │
│  └─────────────────────────┘  │     │ Single rack failure isolates   │     │ Each partition on isolated rack│
│ Fast inter-node communication │     │ strictly 1 instance.           │     │ Distributed HDFS, Kafka, Cass. │
└───────────────────────────────┘     └────────────────────────────────┘     └────────────────────────────────┘
```

---

## 9. Elastic Load Balancing Deep Dive: ALB vs NLB vs GLB (from SolsRef)

### 9.1 Technical Comparison Matrix
| Architectural Feature | Application Load Balancer (ALB) | Network Load Balancer (NLB) | Gateway Load Balancer (GLB) |
|---|---|---|---|
| **OSI Layer** | **Layer 7** (Application: HTTP/HTTPS/gRPC) | **Layer 4** (Transport: TCP/UDP/TLS) | **Layer 3** (Network: Gateway & Route Table) |
| **Throughput & Latency** | Millions of requests/sec; ~10-25 ms latency | **Tens of millions of requests/sec; sub-millisecond latency** | Scales horizontally to inspect Gbps of fleet traffic |
| **Static IP / Elastic IP** | No (resolves to dynamic DNS A-records) | **Yes** (1 static Elastic IP per AZ) | Transparent bump-in-the-wire |
| **Routing Capabilities** | Path-based (`/api`), Host-based (`api.corp.com`), HTTP Header, Query string, Cookie | Port and IP protocol routing only | Transparent routing via **GENEVE Protocol (Port 6081)** |
| **Target Types** | EC2 Instances, ECS Tasks, Lambda Functions, Private IP addresses | EC2 Instances, ECS Tasks, Private IP addresses, ALB | Third-party Virtual Appliance Fleets (Firewalls, IDS/IPS) |
| **SSL / TLS Termination** | Supports SNI (Server Name Indication), ACM integration | High-performance TLS offloading at kernel level | Passthrough |
| **Sticky Sessions** | Duration-based (ALB generated) or Application-based cookies | Source IP Hash sticky routing | Consistent 5-tuple flow hashing |

### 9.2 Advanced ELB Engineering Features
- **Cross-Zone Load Balancing**:
  - When enabled: ALB/NLB distributes incoming traffic evenly across *all* registered instances in *all* enabled Availability Zones.
  - When disabled: Traffic is distributed evenly only across instances within the *local* Availability Zone receiving the request.
  - *Standard SolsArch Rule*: Always enable for ALB (free of charge). For NLB, cross-zone data transfer incurs inter-AZ fees, but prevents overloaded instances when traffic is unevenly distributed across AZs.
- **Connection Draining / Deregistration Delay (Default: 300s, Range: 1–3600s)**:
  - When an instance is deregistered or becomes unhealthy, the load balancer stops routing *new* requests to it while keeping existing in-flight connections open until the timeout expires.
  - Slashes HTTP 502/504 errors during rolling deployments and Auto Scaling scale-in activities.

---

## 10. CloudFront Functions vs Lambda@Edge: The Edge Compute Matrix (from SolsRef)

| Architectural Dimension | CloudFront Functions | Lambda@Edge |
|---|---|---|
| **Runtime Language** | Lightweight JavaScript (ECMAScript 5.1 compliant) | Node.js, Python |
| **Execution Locations** | **600+ CloudFront Edge Points of Presence (PoPs)** | 13 Regional Edge Caches |
| **Execution Event Triggers** | Viewer Request, Viewer Response only | Viewer Request, Viewer Response, **Origin Request, Origin Response** |
| **Max Execution Time** | **< 1 Millisecond** | 5 seconds (Viewer) / 30 seconds (Origin) |
| **Memory Allocation** | 2 MB (strictly enforced) | 128 MB to 10 GB |
| **Network & Filesystem Access** | **None** (No internet access, no external AWS API calls) | **Full Network Access**, AWS SDK access, `/tmp` storage |
| **Request Body Access** | No | Yes |
| **Pricing** | $0.10 per 1 million requests (1/6th the cost) | $0.60 per 1 million requests + compute duration fees |
| **Optimal Production Fit** | High-scale URL redirects, header manipulation, JWT signature validation. | Dynamic HTML generation, image resizing on-the-fly, complex database lookups. |
---

## 12. Edge & Hybrid Compute: AWS Outposts, Wavelength, Local Zones & App Runner (from SolsRef)

### 12.1 AWS Hybrid & Edge Compute Topology Matrix
| Service | Deployment Location | Network Latency | Target Workload Profile |
|---|---|---|---|
| **AWS Outposts** | **Customer On-Premises Data Center** (Physical 42U Rack or 1U/2U servers installed by AWS). | **< 1 Millisecond** to local on-premises hardware. | Strict data residency compliance, low-latency factory robotics, local manufacturing execution systems (MES). |
| **AWS Local Zones** | **Major Metropolitan Centers** close to population centers (e.g. London, Los Angeles, Chicago). | **Single-digit milliseconds** (1–5 ms). | Real-time video rendering, cloud gaming, high-frequency algorithmic trading, interactive CAD/CAM. |
| **AWS Wavelength** | Embedded inside **5G Telecom Carrier Data Centers** (Verizon, Vodafone, KDDI). | **Sub-10 milliseconds** to mobile 5G devices. | Autonomous driving telemetry, connected smart city sensors, AR/VR mobile streaming. |

### 12.2 Container Compute Platform Selection Guide: App Runner vs ECS vs EKS
```
Complexity / Customization ───────────────────────────────────────────────────────► High
                        ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
                        │ AWS App Runner   │   │ Amazon ECS       │   │ Amazon EKS       │
                        │ (Fully Managed)  │   │ (AWS Opinionated)│   │ (Open Kubernetes)│
                        └──────────────────┘   └──────────────────┘   └──────────────────┘
Infrastructure Mgmt:    Zero                   Minimal (Fargate)      Cluster Management
Underlying Tech:        AWS Fargate Engine     AWS Native Control     Upstream CNCF K8s
Ecosystem Portability:  AWS only               AWS only               Multi-Cloud (GKE, AKS)
Best Production Fit:    Web APIs, Node/Python  Microservices, batch   Enterprise K8s fleets
```
---

## 13. High-Performance Networking Silicon: ENA, ENA Express & EFA (from SolsRef)

| Network Technology | Protocol Architecture | Throughput Capacity | Latency Profile | Primary Workload Profile |
|---|---|---|---|---|
| **Elastic Network Adapter (ENA)** | Standard TCP/IP with hardware acceleration | Up to **100 Gbps** | Microsecond level | General-purpose web APIs, microservices, standard database clusters. |
| **ENA Express** | **Scalable Reliable Datagram (SRD)** protocol over custom AWS Nitro silicon | Up to **25 Gbps per flow** | **50% lower P99 latency** | Eliminates TCP head-of-line blocking; high-throughput database replication, distributed in-memory caches (Redis). |
| **Elastic Fabric Adapter (EFA)** | **OS-Bypass (Libfabric / MPI)**; bypasses Linux kernel network stack | Up to **400+ Gbps** | Sub-microsecond latency | High-Performance Computing (HPC), computational fluid dynamics, distributed deep learning model training (NCCL, PyTorch). |

---

## 14. AWS Custom Silicon Architecture: Nitro System, Graviton & AI Accelerators (from SolsRef)

### 14.1 The AWS Nitro System
- Historically, hypervisors on server hardware consumed 15–30% of CPU and RAM for management, networking, storage virtualization, and security monitoring.
- **The Nitro Architecture**: Offloads networking, local NVMe storage, remote EBS storage, and cryptographic security onto dedicated **Nitro PCIe Cards**:
  - **Nitro Card for VPC**: Hardware packet processing, ENI virtualization, security groups.
  - **Nitro Card for EBS**: Hardware NVMe controller for EBS volumes.
  - **Nitro Security Chip**: Hardware root of trust and continuous firmware verification.
  - **Result**: Virtually **100% of host CPU and memory** is allocated to customer virtual machines with near-bare-metal performance.

### 14.2 AWS Custom Silicon Portfolio
- **AWS Graviton3 / Graviton4 (ARM64)**: Custom 64-bit ARM processors delivering up to **40% better price-performance** and 60% lower energy consumption compared to comparable x86 processors.
- **AWS Trainium**: Custom silicon engineered specifically for deep learning model training; features hardware FP8/BF16 engines delivering up to 50% savings over GPU clusters.
- **AWS Inferentia2**: Custom inference accelerator delivering up to $4	imes$ higher throughput and $10	imes$ lower latency for frontier LLMs compared to standard GPU instances.
---

## 15. EC2 Hibernation, Automated Recovery & Status Checks (from SolsRef)

### 15.1 EC2 Hibernation Architecture & Lifecycle
- **Context & Problem**: Heavy monolithic applications, Java/JVM enterprise stacks, or memory-intensive analytical models require 10–20 minutes to boot, load caches, and initialize in-memory working sets from scratch when launched.
- **Hibernation Mechanics**:
```
[ Running EC2 Instance ] ──► User / ASG calls ec2:StopInstances with Hibernate=True
                                    │
                                    ├── 1. Operating system freeze: RAM contents (up to 150 GB)
                                    │      are flushed directly to root EBS Volume
                                    │
                                    ├── 2. EBS Volume must be encrypted with KMS
                                    │
                                    ├── 3. Instance enters 'Stopped' state (Zero EC2 compute charges!)
                                    │
                                    ▼ (User calls ec2:StartInstances)
[ Fast Resume: Sub-Second Boot ] ◄── RAM contents read from EBS back into memory
                                    Application resumes exactly where it stopped;
                                    caches and open OS processes intact!
```
- **Requirements**: Supported on C3, C4, C5, M3, M4, M5, R3, R4, R5 instances; instance RAM $< 150 \text{ GB}$; root volume must be encrypted with AWS KMS.

### 15.2 EC2 Status Checks & Automated CloudWatch Recovery
| Status Check Type | Underlying Cause | Automated Remediation Action |
|---|---|---|
| **System Status Check** (`StatusCheckFailed_System`) | Underlying AWS physical hardware issues: loss of power, hardware failures, physical network loss on host. | **CloudWatch Automated Recovery Action**: Automatically migrates instance to new physical hardware; preserves private IP, Elastic IP, and EBS volume attachments! |
| **Instance Status Check** (`StatusCheckFailed_Instance`) | Software issues on guest OS: kernel panic, exhausted memory/disk, corrupted file system, misconfigured networking. | CloudWatch Alarm triggers an **EC2 Reboot** action or executes an automated Systems Manager (SSM) runbook. |

---

## 16. Amazon ECS Task Placement Strategies & Amazon EKS IRSA (from SolsRef)

### 16.1 ECS Task Placement Strategies vs Placement Constraints
- **Task Placement Strategies (Best Effort)**:
  - **`binpack`**: Places tasks on instances with the *least available* CPU or memory. Maximizes container density and minimizes total EC2 instance count, slashing compute costs.
  - **`spread`**: Places tasks evenly across a specified attribute (e.g. `attribute:ecs.availability-zone` or `instanceId`). Maximizes high availability and blast radius isolation.
  - **`random`**: Random task placement.
  - *Chaining Strategies*: Best practice combines `spread(attribute:ecs.availability-zone)` followed by `binpack(memory)` to achieve cross-AZ resilience with maximum node density.
- **Task Placement Constraints (Strict Rules)**:
  - **`distinctInstance`**: Guarantees that no two tasks of the same service run on the same EC2 container instance.
  - **`memberOf`**: Enforces custom cluster expressions (e.g. `attribute:ecs.instance-type =~ m5.*` or `attribute:ecs.os-type == linux`).

### 16.2 Amazon EKS IAM Roles for Service Accounts (IRSA)
- **Problem**: Historically, all pods running on a Kubernetes worker node inherited the EC2 instance profile's broad IAM role, violating least-privilege security.
- **IRSA Architecture**:
```
[ Kubernetes Pod ] ──► Pod Spec specifies: serviceAccountName: "billing-sa"
                                │
                                ▼ (EKS OIDC Identity Provider)
                     [ Kubernetes ServiceAccount: billing-sa ]
                        Annotations: eks.amazonaws.com/role-arn: "arn:aws:iam::...:role/BillingRole"
                                │
                                ▼
                     [ AWS Security Token Service (STS) ]
                        Invokes: AssumeRoleWithWebIdentity
                        Validates: OIDC JWT token injected into pod by EKS Pod Identity Webhook
                                │
                                ▼
                     [ Scoped Temporary AWS Credentials Injected into Pod Container ]
```
- **Security Enforcement**: Pod A (Billing) only accesses Billing S3 buckets; Pod B (Analytics) on the *same physical node* cannot access Billing resources!
---

## 17. AWS Lambda Advanced Concurrency, Lifecycle & Scaling Controls (from SolsRef)

### 17.1 Lambda Concurrency Models & Blast-Radius Isolation
```
[ AWS Account Concurrency Limit: 1,000 Concurrent Executions (Default) ]
├── Allocated to Function A: [ Reserved Concurrency = 200 ]
│   ├── Guaranteed pool: Function A can always scale up to 200 concurrent executions
│   └── Hard ceiling: Throttled if invocations exceed 200 (protects downstream DB!)
│
├── Allocated to Function B: [ Reserved Concurrency = 300 ]
│   └── Isolated pool: Traffic bursts on Function B cannot starve other functions!
│
└── Remaining: [ Unreserved Concurrency Pool = 500 ]
    └── Shared dynamically by all other functions in the account (Function C, D, E...)
```
- **Provisioned Concurrency**:
  - Allocates pre-warmed execution environments that initialize the runtime, download dependencies, and execute initialization code *before* requests arrive.
  - Eliminates **cold starts** completely; guarantees consistent sub-10ms response times for latency-critical APIs.
- **Lambda Function URLs**:
  - Dedicated HTTP(S) endpoints for Lambda functions without requiring an Application Load Balancer or API Gateway.
  - Auth types: `AWS_IAM` (requires SigV4 signing) or `NONE` (publicly accessible with built-in CORS configuration).
- **Lambda Destinations vs Dead-Letter Queues (DLQs)**:
  - *DLQ*: Handles asynchronous invocation *failures only*; targets SQS queues or SNS topics; receives only raw payload.
  - *Destinations*: Handles *both success and failure* for asynchronous invocations and stream events; targets SQS, SNS, EventBridge, or another Lambda function; includes execution metadata, stack traces, and request context.
---

## 18. AWS Elastic Beanstalk Deployment Strategies: Rolling, Immutable & Traffic Splitting (from SolsRef)

### 18.1 Comprehensive Deployment Policy Comparison
| Deployment Strategy | Deployment Speed | Downtime? | Production Capacity During Deploy | Rollback Impact & Mechanics | Best Use Case |
|---|---|---|---|---|---|
| **All at Once** | **Fastest** | **Yes** (temporary service outage) | **0%** during code replacement | Re-deploy old version across entire fleet; high downtime. | Local development, non-critical test environments. |
| **Rolling** | Moderate | **No** | **Reduced capacity** (e.g. 75% if batch size is 25%) | Must roll back sequentially batch-by-batch across fleet. | Cost-sensitive environments that can tolerate temporary capacity dips. |
| **Rolling with Additional Batch** | Moderate | **No** | **100% Guaranteed Capacity** (spins up new batch before updating old) | Rolls back sequentially; existing capacity never drops below baseline. | Production environments with strict peak capacity SLAs. |
| **Immutable** | Slow | **No** | **100% Guaranteed Capacity** (launches temporary ASG with new version) | **Instant, zero-impact rollback**: simply terminates temporary ASG if health checks fail! | Mission-critical production deployments; zero blast radius on failure. |
| **Traffic Splitting (Canary)** | Slow | **No** | **100% Guaranteed Capacity** | Instant rollback; redirects traffic back to 100% old version. | Canary testing: routes 5% to 10% of real production traffic to evaluate new version. |
| **Blue / Green** | Fast cutover | **No** | **100% Guaranteed Capacity** | Instant DNS swap back to Blue environment. | Major version upgrades, database schema migrations, runtime updates. |

### 18.2 Immutable Deployment Mechanics
```
[ Production ASG: v1 Fleet (4 Instances Healthy) ] ◄── Traffic from ALB
                         │
                         ├── 1. Beanstalk launches temporary ASG with 1 instance of v2
                         ├── 2. Evaluates ELB Health Check on v2 instance
                         │      ├── FAIL: Beanstalk terminates temporary ASG (Zero impact on v1!)
                         │      └── PASS: Beanstalk scales temporary ASG to match full capacity (4 instances)
                         │
                         ├── 3. Attaches all 4 v2 instances to production ALB
                         └── 4. Gracefully drains connection and terminates original v1 ASG!
```
