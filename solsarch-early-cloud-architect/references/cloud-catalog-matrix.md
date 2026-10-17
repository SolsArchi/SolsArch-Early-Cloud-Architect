# Cloud catalog matrix

Canonical needs mapped to services on AWS, Azure and GCP, plus modern platform options.
Compiled from the SolsArch knowledge base and general cloud practice. Written 2026-10-17.

Service names, features and prices change. Before quoting a price, a quota or regional
availability, check the vendor page with a web search if you have one, and state the date.
Without a search tool, label the figure "estimate, unverified".

## How to choose

1. Start from evidence in the scan, not from the catalog.
2. Prefer the user's primary cloud. If none is set, compare two and recommend one.
3. Prefer managed services over self-run ones unless the user needs portability or has a cost reason.
4. Keep stateful services in private subnets. Keep compute stateless.
5. Record the rejected alternative and the reason.

## Compute and containers

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Always-on containers, queue workers, WebSockets | ECS on Fargate | Container Apps | Cloud Run (min instances) | Render, Railway, Fly.io | Dockerfile exists; background consumers or streaming need long-lived processes |
| Scale-to-zero HTTP containers | App Runner | Container Apps | Cloud Run | Fly.io | Spiky traffic, stateless service |
| Serverless functions | Lambda | Functions | Cloud Run functions | Vercel Functions, Cloudflare Workers | Short request/response handlers, event glue |
| Kubernetes | EKS | AKS | GKE | DigitalOcean Kubernetes | Team already runs Kubernetes, many services |
| Virtual machines | EC2 | Virtual Machines | Compute Engine | Hetzner, DigitalOcean | Legacy software, special kernels, licensing |
| Batch and scheduled jobs | AWS Batch, EventBridge Scheduler | Container Apps jobs, Batch | Cloud Run jobs | Modal | Nightly or on-demand processing |
| GPU inference | EC2 G/P instances, SageMaker | N-series VMs, Azure ML | GPU on GKE or Compute Engine | RunPod, CoreWeave, Modal | Self-hosted models (see AI rows) |
| Static site and frontend | S3 + CloudFront, Amplify | Static Web Apps | Firebase Hosting, Cloud Storage + CDN | Vercel, Netlify, Cloudflare Pages | Next.js or SPA with no server state |

## Data

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Relational (default) | Aurora PostgreSQL, RDS | Database for PostgreSQL Flexible Server | Cloud SQL, AlloyDB | Neon, Supabase | Code uses Postgres, MySQL, or SQLite that must become shared |
| Relational, spiky or small | Aurora Serverless v2 | Azure SQL serverless | Cloud SQL (small tier) | Neon | Idle most of the day |
| Global SQL | Aurora Global, DSQL | Cosmos DB for PostgreSQL | Spanner | CockroachDB | Multi-region writes are a hard requirement |
| Document | DynamoDB, DocumentDB | Cosmos DB | Firestore | MongoDB Atlas | Code uses MongoDB or key-based access patterns |
| Cache and short-lived state | ElastiCache (Redis or Valkey) | Cache for Redis | Memorystore | Upstash Redis | Code uses Redis, sessions, rate limits |
| Search | OpenSearch Service | AI Search | Vertex AI Search, Elastic on GCP | Elastic Cloud, Meilisearch Cloud | Full-text search; replaces embedded search stores |
| Vector search | Aurora pgvector, OpenSearch | PostgreSQL pgvector, AI Search | AlloyDB pgvector, Vertex AI Vector Search | Pinecone, Qdrant Cloud | RAG; start with pgvector when Postgres already exists |
| Object storage | S3 | Blob Storage | Cloud Storage | Cloudflare R2, Backblaze B2 | Uploads, artifacts, backups. R2 for heavy egress |
| Shared file system | EFS, FSx | Azure Files | Filestore | | Several containers must share files |
| Data warehouse | Redshift | Synapse, Fabric | BigQuery | Snowflake, Databricks | Analytics over large tables |

## Messaging and integration

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Job queue | SQS | Service Bus queues, Storage queues | Cloud Tasks | Upstash QStash | Celery, BullMQ or RQ present; at-least-once jobs |
| Broker with Redis semantics | ElastiCache | Cache for Redis | Memorystore | Upstash | BullMQ or Sidekiq require Redis itself |
| Pub/sub fan-out | SNS, EventBridge | Event Grid, Service Bus topics | Pub/Sub | | One event, many consumers |
| Event streaming | Kinesis, MSK | Event Hubs | Pub/Sub, Managed Kafka | Confluent Cloud | Kafka client in manifests, high volume ordered streams |
| Workflow orchestration | Step Functions | Durable Functions, Logic Apps | Workflows | Temporal Cloud | Multi-step jobs with retries and state |
| Email and notifications | SES, SNS | Communication Services | (partner services) | Resend, Postmark | Transactional mail |

## Networking and edge

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Public HTTP ingress | Application Load Balancer | Application Gateway | Cloud Load Balancing | Cloudflare Load Balancing | Always in front of private containers (see SEC-001) |
| CDN | CloudFront | Front Door | Cloud CDN | Cloudflare, Fastly | Static assets, caching, global latency |
| WAF | AWS WAF | Front Door WAF | Cloud Armor | Cloudflare WAF | Any public endpoint handling user input |
| API gateway | API Gateway | API Management | API Gateway, Apigee | Kong | Many clients, quotas, key management |
| DNS | Route 53 | Azure DNS | Cloud DNS | Cloudflare DNS | Always |
| Private connectivity | VPC endpoints, PrivateLink | Private Link | Private Service Connect | | Reach managed services without public internet |
| Outbound internet from private subnets | NAT Gateway | NAT Gateway | Cloud NAT | | Private compute calling external APIs |
| Private network | VPC | Virtual Network | VPC | | Anything stateful |

## Security, identity and operations

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Secrets | Secrets Manager, SSM Parameter Store | Key Vault | Secret Manager | Doppler, HashiCorp Vault | Any credential in code or .env (SEC-005) |
| Encryption keys | KMS | Key Vault | Cloud KMS | | Compliance scopes that need customer-managed keys |
| End-user auth | Cognito | Entra External ID | Identity Platform | Auth0, Clerk, Supabase Auth | App has login; passport, next-auth or JWT libraries found |
| Workload identity | IAM roles for tasks | Managed identities | Service accounts, Workload Identity | | Replace static access keys |
| Logging and metrics | CloudWatch | Azure Monitor | Cloud Logging, Cloud Monitoring | Grafana Cloud, Datadog | Always |
| Tracing | X-Ray, OpenTelemetry on ADOT | Application Insights | Cloud Trace | | Several services call each other |
| Audit trail | CloudTrail | Activity Log | Cloud Audit Logs | | SOC 2, ISO 27001, regulated work |
| Backups | AWS Backup, automated snapshots | Backup | Backup and DR | | Any data tier |
| Container registry | ECR | Container Registry | Artifact Registry | GitHub Container Registry | Dockerfile present |
| CI/CD | CodePipeline, GitHub Actions | Azure DevOps, GitHub Actions | Cloud Build | GitHub Actions | Pipelines to deploy |
| Infrastructure as code | CloudFormation, CDK, Terraform | Bicep, Terraform | Deployment Manager, Terraform | Pulumi | Repeatable environments |

## AI workloads

| Need | AWS | Azure | GCP | Platform options | Pick it when |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Hosted foundation models | Bedrock | Azure OpenAI, AI Foundry | Vertex AI | Direct provider APIs | Code calls openai, anthropic or similar SDKs |
| Self-hosted open models | SageMaker, EC2 GPU | Azure ML, N-series | Vertex AI, GKE GPU | RunPod, Modal | vllm, ollama or torch in manifests |
| Embeddings and ingestion queue | Bedrock embeddings + SQS | Azure OpenAI + Service Bus | Vertex embeddings + Pub/Sub | | RAG ingestion runs async |
| Streaming responses to browsers | ALB with long idle timeout | Application Gateway with extended timeout | Cloud Run (request timeout up to 60 min) | | Chat UI; also see SEC-004 |

## Placement heuristics

- Heavy outbound media or large downloads: object storage on Cloudflare R2 avoids egress fees.
- Serverless web plus AI: Vercel or Workers for the API, Neon for metadata, Pinecone or Qdrant Cloud for vectors.
- Regulated workloads: AWS or Azure with private endpoints, customer-managed keys, multi-AZ replication, audit logging.
- Small team, one service, one database: a single container platform with managed Postgres beats Kubernetes.
- SQLite or embedded stores found next to a Dockerfile: plan the move to managed Postgres in this design (SEC-003).

## Allowed hops between tiers

| From | To | Allowed | Never |
| :--- | :--- | :--- | :--- |
| Browser | Load balancer or CDN | HTTPS, WSS | Direct database or cache ports |
| Load balancer | Application containers | HTTP, HTTPS, gRPC | Database protocols |
| Application | Relational database | PostgreSQL, MySQL | HTTP, SSH |
| Application | Cache | Redis protocol over TLS where offered | |
| Application | Queue or object storage | HTTPS, AMQP | Unencrypted raw sockets |
| Database | Application | None. Databases do not initiate connections to compute | Any inbound from the data tier |

## Migration shortcuts (when the repo is a lift-and-shift)

Rehost with the cloud's migration service, replatform databases to the managed engine,
refactor to events only when the user asks. Size from measured utilization, not the old
server's specification; "as-is" sizing is a cost finding.
