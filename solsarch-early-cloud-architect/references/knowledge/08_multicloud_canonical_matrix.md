# SolsBrain — Chapter 8: 50+ Multi-Cloud Canonical Mapping Matrix

**Domain:** Universal Service Taxonomy, Cross-Cloud Equivalents & Protocol Allow-Lists  
**Target Clouds:** AWS, Azure, Google Cloud, Cloudflare, Vercel, Supabase, Neon, Render, Fly.io, Upstash, etc.  
**Purpose:** Canonical IR translation matrix for SolsArch's Cloud Decider and Deterministic Validator.

---

## 1. Unified 40-Type Canonical Service Matrix

| Canonical Primitive | AWS Service | Microsoft Azure | Google Cloud (GCP) | Specialized / Modern PaaS |
|---|---|---|---|---|
| `virtual_machine` | Amazon EC2 | Azure Virtual Machines | Compute Engine | Hetzner, DigitalOcean Droplets, Linode |
| `container_service` | AWS ECS Fargate | Azure Container Apps | Cloud Run | Render, Railway, Fly.io |
| `serverless_function` | AWS Lambda | Azure Functions | Cloud Functions | Cloudflare Workers, Vercel Functions |
| `batch_job` | AWS Batch | Azure Batch | Cloud Run Jobs | Modal, RunPod |
| `k8s_cluster` | Amazon EKS | Azure Kubernetes Service (AKS) | Google Kubernetes Engine (GKE) | DigitalOcean Kubernetes, Civo |
| `relational_db` | Amazon RDS / Aurora | Azure Database for PostgreSQL | Cloud SQL / Spanner | Neon, Supabase, PlanetScale |
| `document_db` | Amazon DynamoDB / DocDB | Azure Cosmos DB | Cloud Firestore | MongoDB Atlas |
| `key_value_cache` | ElastiCache (Redis) | Azure Cache for Redis | Memorystore for Redis | Upstash Redis, Redis Cloud |
| `data_warehouse` | Amazon Redshift | Azure Synapse Analytics | BigQuery | Snowflake, Databricks |
| `object_storage` | Amazon S3 | Azure Blob Storage | Google Cloud Storage (GCS) | Cloudflare R2, Backblaze B2 |
| `message_queue` | Amazon SQS | Azure Service Bus Queue | Cloud Tasks | Upstash QStash, RabbitMQ |
| `event_stream` | Amazon Kinesis / MSK | Azure Event Hubs / Kafka | Cloud Pub/Sub / Managed Kafka | Confluent Cloud |
| `load_balancer` | Application Load Balancer | Azure Application Gateway | Cloud Load Balancing | Cloudflare Load Balancing |
| `api_gateway` | Amazon API Gateway | Azure API Management (APIM) | Apigee / API Gateway | Cloudflare API Gateway, Kong |
| `cdn_edge` | Amazon CloudFront | Azure Front Door / CDN | Cloud CDN | Cloudflare CDN, Fastly |
| `dns_service` | Amazon Route 53 | Azure DNS / Traffic Manager | Cloud DNS | Cloudflare DNS |
| `firewall_waf` | AWS WAF | Azure WAF | Cloud Armor | Cloudflare WAF |
| `secret_manager` | AWS Secrets Manager / SSM | Azure Key Vault | Secret Manager | Doppler, HashiCorp Vault |
| `identity_auth` | Amazon Cognito | Microsoft Entra ID (B2C) | Firebase Authentication | Clerk, Auth0, Supabase Auth |
| `gpu_cluster` | Amazon EC2 (G5/P4) | Azure N-Series VMs | Compute Engine GPUs / TPU | CoreWeave, Lambda Labs, RunPod |

---

## 2. Cross-Boundary Protocol Allow-Lists & Rules

The SolsArch Deterministic Validator enforces valid protocol hops between canonical components:

```
[ Ingress / Client ]
       │  (HTTPS / WebSocket only)
       ▼
[ Edge CDN / WAF ]
       │  (HTTPS only)
       ▼
[ Load Balancer / API Gateway ]
       │  (HTTP / gRPC / TCP)
       ▼
[ Application Compute (Containers / VMs) ]
       │  ┌───────────────────────────┬───────────────────────────┐
       │  │ (PostgreSQL: 5432)        │ (Redis: 6379)             │ (HTTPS: 443)
       ▼  ▼                           ▼                           ▼
[ Relational Database ]      [ In-Memory Cache ]         [ Object Storage / S3 ]
```

### Protocol Validation Matrix
| Source Category | Target Category | Allowed Protocols | Forbidden Protocols |
|---|---|---|---|
| `client_browser` | `load_balancer` | `HTTPS`, `WSS` | `TCP`, `POSTGRES`, `REDIS` (Direct access blocked) |
| `load_balancer` | `container_service` | `HTTP`, `HTTPS`, `gRPC` | `POSTGRES`, `AMQP` |
| `container_service` | `relational_db` | `POSTGRES`, `MYSQL`, `MSSQL` | `HTTP`, `SSH` |
| `container_service` | `key_value_cache` | `REDIS`, `MEMCACHED` | `HTTP`, `POSTGRES` |
| `container_service` | `message_queue` | `HTTPS`, `AMQP`, `MQTT` | Raw unencrypted sockets |
| `relational_db` | `container_service` | **None** (Databases do not initiate connections to compute) | Any incoming |

---

## 3. Multi-Cloud Placement Optimization Heuristics

1. **High Data Egress Applications**:
   - If an application serves high outbound video, images, or large binary artifacts, place object storage on **Cloudflare R2** (Zero egress fees) rather than direct AWS S3.
2. **Serverless AI Applications with Vector Storage**:
   - Web API: Cloudflare Workers or Vercel.
   - Vector Store: Pinecone or Qdrant Cloud.
   - Relational Metadata: Neon (Serverless Postgres with branching).
3. **Enterprise Compliance & Regulated Workloads**:
   - Primary: AWS or Azure with dedicated PrivateLink, customer-managed KMS encryption, and multi-AZ synchronous replication.
---

## 4. Cloud Migration Strategies & SolsArch Migration Playbook (from SolsRef)

### Best Case 8.1: The 7 Rs Cloud Migration Framework & Decision Logic
SolsArch Analyzer evaluates enterprise workloads against the canonical AWS/Multi-Cloud 7 Rs migration methodology:

```
[ Application Portfolio Discovery & Assessment ]
                       │
       ┌───────────────┼───────────────┬───────────────┐
       ▼               ▼               ▼               ▼
  [ Retire ]      [ Retain ]     [ Repurchase ]   [ Relocate ]
  Turn off dead   Keep on-prem   Drop & Shop to   VMware Cloud
  workloads       due to debt    SaaS (Salesforce, on AWS / VMC
  (10-20% save)   or compliance  Workday, M365)
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
  [ Rehost ]                      [ Replatform ]              [ Refactor / Re-architect ]
  "Lift & Shift"                  "Lift, Tinker & Shift"      Cloud-Native Modernization
  AWS MGN / Azure Migrate         DB to RDS / App to ECS      Serverless, Event-Driven
```

| Migration Strategy (7 Rs) | Transformation Effort | Cloud Optimization | Typical Use Case | Primary AWS Tooling |
|---|:---:|:---:|---|---|
| **1. Rehost ("Lift & Shift")** | Minimal | Low | Rapid data center exit; legacy commercial off-the-shelf software (COTS). | AWS Application Migration Service (AWS MGN) |
| **2. Replatform ("Lift, Tinker & Shift")** | Low | Medium | Migrate self-managed DB to RDS/Aurora; containerize VM into ECS/EKS without changing code. | AWS MGN + AWS DMS |
| **3. Repurchase ("Drop & Shop")** | Moderate | High | Replace customized legacy CRM/HR software with modern SaaS alternatives. | Third-party SaaS / Marketplace |
| **4. Refactor / Re-architect** | High | Maximum | Break monolith into event-driven serverless microservices for unlimited scaling. | AWS Lambda, Fargate, SQS, EventBridge |
| **5. Relocate** | Zero code change | Low | Transfer VMware vSphere clusters directly to AWS without converting VMs. | VMware Cloud on AWS |
| **6. Retain** | None | None | Workloads that are newly built on-premise or subject to strict air-gap compliance. | AWS Outposts / Local Zones |
| **7. Retire** | None (Shutdown) | Cost Saving | Decommission obsolete services discovered during portfolio scanning. | AWS Application Discovery Service |

### Best Case 8.2: Zero-Downtime Heterogeneous Database Migration: AWS DMS + Schema Conversion Tool (SCT)
- **Context & Problem**: Migrating mission-critical on-premises databases (Oracle / Microsoft SQL Server) to cloud-native open source engines (Amazon Aurora PostgreSQL / MySQL) without taking days of application downtime.
- **Architecture Blueprint**:
```
[ On-Premises Source Database (Oracle / SQL Server) ]
                     │
                     ├── 1. Schema & Object Conversion
                     │      [ AWS Schema Conversion Tool (SCT) ]
                     │      Converts tables, views, stored procedures to PostgreSQL
                     │
                     └── 2. Live Data Migration
                            [ AWS Database Migration Service (DMS) ]
                            ├── Phase A: Initial Full Load (historical data)
                            ├── Phase B: Ongoing Replication (CDC via transaction logs)
                            │
                            ▼ (Near-zero replication lag)
             [ Target Database: Amazon Aurora PostgreSQL ]
                     ▲
                     │ (Final Cutover: Redirect application connection pool in < 2 minutes)
         [ Cloud Microservices / Application Tier ]
```
---

## 5. Enterprise Migration Discovery, Evaluator & Transfer Family (from SolsRef)

### 5.1 AWS Migration Discovery & Business Case Tooling
- **AWS Application Discovery Service**:
  - **Agentless Discovery Connector**: Deployed as an OVA virtual appliance on VMware vCenter. Discovers VM inventory, CPU allocation, RAM utilization, and disk sizing without installing software on guest operating systems.
  - **Agent-Based Discovery Agent**: Installed directly inside physical servers or virtual machines. Captures detailed network connection dependencies (inbound/outbound TCP/UDP sockets) and performance profiles.
- **AWS Migration Evaluator (formerly TSO Logic)**:
  - Generates executive-level Total Cost of Ownership (TCO) financial models.
  - Analyzes historical CPU and RAM utilization patterns to project exact cloud costs with optimal rightsizing and multi-year Savings Plans.

### 5.2 AWS Transfer Family (SFTP, FTPS, FTP)
- **Architecture**: Fully managed, highly available file transfer endpoints supporting SFTP (SSH File Transfer Protocol), FTPS (FTP over SSL), and plain FTP.
- **Storage Backing**: Transparently reads and writes files directly to **Amazon S3** or **Amazon EFS**.
- **Authentication**: Native integration with Microsoft Active Directory, AWS Secrets Manager, or custom Identity Providers via API Gateway and Lambda.
---

## 6. Cloud Migration Financial Modeling & Performance Rightsizing (from SolsRef)

### 6.1 Total Cost of Ownership (TCO) Methodology
When evaluating enterprise migrations from on-premises data centers to AWS, SolsArch Analyzer evaluates TCO across direct and indirect cost vectors:
$$\text{Total Cloud ROI} = (\text{On-Prem Direct Costs} + \text{On-Prem Indirect Costs}) - (\text{AWS Compute/Storage} + \text{Migration Effort})$$
- **On-Premises Direct Costs**: Server hardware amortized over 3–5 years, storage SAN/NAS maintenance, network switch licensing, hypervisor licenses (VMware vSphere), database licenses (Oracle/SQL Server).
- **On-Premises Indirect Costs**: Data center floor space real estate, electricity and cooling (HVAC), physical security personnel, rack engineering, hardware spare parts inventory, IT administration staffing.

### 6.2 Server Rightsizing Models: Direct Match vs Performance-Based Match
```
[ On-Premises Server: 64 vCPUs, 256 GB RAM (Peak CPU: 12%, Peak RAM: 28%) ]
                       │
       ┌───────────────┴───────────────┐
       ▼                               ▼
[ Direct Sizing ("As-Is Match") ]    [ Performance-Based Rightsizing ("Best Practice") ]
Allocates: m6i.16xlarge (64 vCPU, 256 GB) Allocates: m6i.4xlarge (16 vCPU, 64 GB)
Cost: ~$2,800 / month               Cost: ~$700 / month (75% Immediate Cost Reduction!)
```
- **SolsArch Evaluator Rule**: `FIN-MIG-001` — Migration plans based on "As-Is" hardware sizing must be flagged as Critical Severity for cost inefficiency. Migrations must utilize performance-based rightsizing based on 95th percentile historical utilization metrics.
---

## 7. Enterprise Migration Decision Trees: DMS, SCT, DataSync & Snow Family (from SolsRef)

### 7.1 Database Migration: AWS DMS & Schema Conversion Tool (SCT)
```
                               [ Existing Database Migration Assessment ]
                                                   │
                                                   ▼
                                  ┌───────────────────────────────────┐
                                  │ Are Source and Target DB engines  │
                                  │ the SAME? (e.g. Oracle -> RDS     │
                                  │ Oracle, or Postgres -> Aurora PG) │
                                  └───────────────────────────────────┘
                                         │ YES                   │ NO (Heterogeneous)
                                         ▼                       ▼
                               ┌───────────────────┐   ┌─────────────────────────────────────────┐
                               │ SCT is NOT needed │   │ AWS Schema Conversion Tool (SCT) MUST   │
                               │ Native backup/    │   │ be used to convert schema, DDL, stored  │
                               │ restore or DMS    │   │ procedures, views, and functions        │
                               └───────────────────┘   └─────────────────────────────────────────┘
                                         │                       │
                                         └───────────┬───────────┘
                                                     ▼
                                       ┌───────────────────────────┐
                                       │ AWS DMS Replication Fleet │
                                       │ 1. Full Load initial data │
                                       │ 2. Change Data Capture    │
                                       │    (CDC) transaction logs │
                                       └───────────────────────────┘
                                                     │
                                                     ▼
                                     [ Near-Zero Downtime Cutover ]
```

### 7.2 Large-Scale Data Transfer: AWS DataSync vs AWS Snow Family
| Data Volume | Available Network Bandwidth | Estimated Network Transfer Duration | Architectural Choice |
|---|---|---|---|
| $< 10 \text{ TB}$ | $100 \text{ Mbps} - 10 \text{ Gbps}$ | Hours to a few days | **AWS DataSync** over internet or Direct Connect. |
| $10 \text{ TB} - 500 \text{ TB}$ | Limited ($< 100 \text{ Mbps}$) or Expensive | Weeks to months | **AWS Snowball Edge Storage Optimized** (80 TB / 210 TB per device). |
| $500 \text{ TB} - 10 \text{ PB}$ | Direct Connect unavailable or congested | Many months | **Multi-device Snowball Edge fleet** clustered together. |
| $> 10 \text{ PB}$ | Exceeds all physical WAN capacity | Years | **AWS Snowmobile** (45-foot ruggedized shipping container, up to 100 PB per container). |
| Rugged / Edge Compute | Disconnected / military / maritime | No internet connectivity | **AWS Snowcone** (8 TB / 14 TB, battery-powered) or **Snowball Edge Compute Optimized** (104 vCPUs). |
