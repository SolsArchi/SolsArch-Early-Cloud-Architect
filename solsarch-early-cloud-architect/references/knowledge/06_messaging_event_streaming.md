# SolsBrain — Chapter 6: Asynchronous Messaging & Event Streaming

**Domain:** Message Queuing, Publish/Subscribe, Event Buses & Streaming Data  
**Target Clouds:** AWS, Azure, Google Cloud  
**Purpose:** Microservice decoupling, backpressure management, and event-driven patterns.

---

## 1. Message Queuing: Amazon SQS

### 1.1 Standard Queues vs. FIFO Queues
| Attribute | SQS Standard Queue | SQS FIFO Queue (`.fifo`) |
|---|---|---|
| **Throughput** | **Unlimited** transactions per second | 300 msg/sec (up to 3,000 msg/sec with batching) |
| **Delivery Guarantee** | **At-Least-Once** (duplicates possible) | **Exactly-Once** delivery guarantee |
| **Ordering** | **Best-Effort Ordering** (occasional out-of-order) | **Strict First-In-First-Out (FIFO)** order preserved |
| **Deduplication** | Application must implement idempotent handling | Content-based deduplication or `MessageDeduplicationId` (5-min window) |
| **Grouping** | Single global queue partition | `MessageGroupId` enables parallel processing per group |

### 1.2 Queue Mechanics & Tuning Parameters
- **Visibility Timeout**:
  - The duration (default 30 seconds, max 12 hours) that a message becomes invisible to other consumers after being received.
  - *Golden Rule*: Set Visibility Timeout to **6x the maximum expected processing time** of your consumer.
  - *Dynamic Extension*: If processing runs long, the worker must call `ChangeMessageVisibility` to prevent redelivery.
- **Dead Letter Queue (DLQ)**:
  - Redrive policy specifies `maxReceiveCount` (e.g., 3–5 attempts).
  - Unprocessed messages are automatically routed to a DLQ for manual inspection and alarming.
- **Long Polling (`WaitTimeSeconds = 20`)**:
  - Worker holds HTTP connection open up to 20s waiting for messages.
  - Reduces empty responses and slashes SQS API request costs by up to 90%.

---

## 2. Publish / Subscribe: Amazon SNS & Fan-Out Patterns

### 2.1 The Fan-Out Architecture Pattern
Instead of a producer sending messages directly to multiple independent downstream services, it publishes once to an SNS Topic:
```
                    [ Ordering Service ]
                             │
                  [ SNS Topic: OrderCreated ]
                             │
     ┌───────────────────────┼───────────────────────┐
     │                       │                       │
     ▼                       ▼                       ▼
 [ SQS Queue ]           [ SQS Queue ]           [ SQS Queue ]
 (Inventory)             (Billing)               (Notification)
     │                       │                       │
     ▼                       ▼                       ▼
[ Inventory Worker ]    [ Billing Worker ]      [ Email Worker ]
```
- **Advantages**:
  - Loose coupling: Adding a new subscriber service requires zero modifications to the producer.
  - Fault isolation: If the Billing service crashes, messages buffer safely in its dedicated SQS queue without impacting Inventory or Notifications.

### 2.2 SNS Message Filtering
- Subscriber queues attach JSON **Filter Policies** to their SNS subscription.
- Only messages matching the filter attributes are delivered to that queue (e.g., `{"order_type": ["premium"]}`), filtering out irrelevant traffic before it reaches the consumer.

---

## 3. Enterprise Event Bus: Amazon EventBridge

### 3.1 EventBridge vs. SNS vs. SQS
| Capability | Amazon SQS | Amazon SNS | Amazon EventBridge |
|---|---|---|---|
| **Type** | Point-to-Point Queue | Pub/Sub Topic | Enterprise Event Bus |
| **Filtering** | None | Simple message attributes | Advanced JSON payload content-based matching |
| **SaaS Integration** | None | Limited | Native ingestion from DataDog, Auth0, Shopify, GitHub |
| **Schema Registry** | No | No | Yes (Generates code bindings for Java, Python, TypeScript) |
| **Latency** | ~10–20 ms | ~10–30 ms | ~50–100 ms |

---

## 4. Real-Time Data Streaming: Kinesis vs. Kafka (MSK)

### 4.1 Amazon Kinesis Data Streams Architecture
- **Shard Capacity**:
  - **Write**: 1 MB/sec or 1,000 records/sec per shard.
  - **Read**: 2 MB/sec per shard (shared across all consumers).
- **Enhanced Fan-Out (EFO)**:
  - Allocates dedicated 2 MB/sec per shard per registered consumer using HTTP/2 push.
  - Eliminates consumer contention and drops latency to ~70 ms.
- **Kinesis Data Firehose**:
  - Fully managed, zero-code ingestion service.
  - Buffers data by size (1–128 MB) or time (60–900 seconds) and automatically loads into S3, Redshift, OpenSearch, or Splunk.

---

## 5. Messaging Validation Rules for SolsArch

1. **RULE-MSG-01**: A background worker consuming from an SQS queue must have an associated Dead Letter Queue (DLQ) configured with a CloudWatch alarm on `ApproximateNumberOfMessagesVisible > 0`.
2. **RULE-MSG-02**: A high-throughput API that triggers asynchronous background processing must place an SQS queue or EventBridge bus between the web tier and worker tier.
3. **RULE-MSG-03**: Workloads requiring strict transactional message ordering must specify FIFO SQS queues with `MessageGroupId` and must NOT exceed 3,000 msg/sec throughput.
---

## 4. Production Messaging, Decoupling & Streaming Best Cases (from SolsRef)

### Best Case 6.1: SNS-to-SQS Pub/Sub Fan-Out Architecture with Filter Policies
- **Context & Problem**: A microservice produces core business events (e.g. `OrderPlaced`) that must be ingested by multiple distinct downstream systems (Billing, Fulfillment, Fraud Detection, Analytics) without creating direct point-to-point network coupling.
- **Architecture Blueprint**:
```
[ Order Processing Service ]
              │ (Publishes once: OrderPlaced Event)
              ▼
    [ Amazon SNS Topic: Orders ]
              │
   ┌──────────┼──────────────────────┬──────────────────────┐
   │ (All)    │ (Total > $1000)      │ (All)                │ (Region: EU)
   ▼          ▼                      ▼                      ▼
[ SQS Queue ] [ SQS Queue ]          [ SQS Queue ]          [ SQS Queue ]
 Billing Svc   Fraud Detection        Fulfillment Svc        GDPR Audit Svc
   │          │                      │                      │
   ▼          ▼                      ▼                      ▼
[ Worker ]   [ Worker ]             [ Worker ]             [ Worker ]
```
- **Key Architectural Properties**:
  - **Decoupled Throughput**: A slow or crashed worker fleet on the Fulfillment queue has zero impact on the Billing queue.
  - **SNS Subscription Filter Policies**: Messages are filtered at the SNS layer, eliminating compute waste in downstream worker queues.
  - **Dedicated Dead Letter Queues (DLQ)**: Each SQS queue maintains its own DLQ and alarm for poisoned messages.

### Best Case 6.2: Asynchronous Request/Response Pattern with SQS Correlation ID & Reply Queues
- **Context & Problem**: Heavy computational tasks (generating complex PDFs, transcoding video, running machine learning inference) must be decoupled from user-facing APIs, but the requesting client must eventually receive the output.
- **Architecture Blueprint**:
```
[ API Gateway / Client ]
           │
           ├── 1. Enqueue Job: SQS Request Queue
           │      MessageAttributes:
           │      - CorrelationId: "uuid-9921-bc"
           │      - ReplyToQueueUrl: "https://sqs.us-east-1.amazonaws.com/.../client-replies"
           │
           ▼
[ Worker Fleet (EC2 / ECS / Lambda) ]
   └── Consumes message, executes heavy processing job
           │
           └── 2. Put Result: SQS Reply Queue
                  MessageAttributes:
                  - CorrelationId: "uuid-9921-bc"
                  - Payload: Result data
           │
           ▼
[ Client / WebSocket Listener ] (Correlates response via matching CorrelationId)
```

### Best Case 6.3: Consumer Message Idempotency & Deduplication Pattern with DynamoDB
- **Context & Problem**: Standard SQS queues offer *at-least-once delivery*. Network timeouts, consumer restarts, or visibility timeout expirations can cause the same message to be delivered more than once. If consumer operations involve financial transactions or order creation, duplicate executions cause severe data corruption.
- **Idempotency Algorithm**:
```
[ Consumer receives message from SQS ]
           │
           ▼
[ Extract Unique Transaction ID (e.g., transaction_id: "tx-49821") ]
           │
           ▼
[ Execute Conditional Put to DynamoDB Idempotency Table ]
  Query: PutItem {
    TableName: "ProcessedTransactions",
    Item: { "tx_id": "tx-49821", "status": "PROCESSING", "ttl": 172800 },
    ConditionExpression: "attribute_not_exists(tx_id)"
  }
           │
           ├── [ Condition Succeeded ] ──► Execute business logic (charge card, ship item)
           │                               Update status to "COMPLETED"
           │                               Delete message from SQS
           │
           └── [ ConditionalCheckFailedException ] ──► DUPLICATE DETECTED!
                                                       Log warning, skip processing
                                                       Delete duplicate message from SQS
```

### Best Case 6.4: SQS Large Payload Processing with S3 Extended Client (> 256 KB)
- **Context & Problem**: SQS message size is strictly capped at 256 KB. Workloads transmitting document blobs, DICOM medical images, or detailed telemetry packets exceed this limit.
- **Architecture**:
  - The producer utilizes the **Amazon SQS Extended Client Library for Java/Python**.
  - If the payload exceeds 256 KB, the library automatically uploads the payload to an designated S3 bucket and sends an SQS message containing an Amazon S3 pointer reference.
  - The consumer library transparently retrieves the S3 object when polling SQS, passes the full payload to the worker logic, and automatically deletes the S3 payload when the SQS message is deleted.

### Best Case 6.5: End-to-End Real-Time Big Data Streaming Pipeline: Kinesis -> Firehose -> S3 -> Athena
```
[ High-Volume IoT / Clickstream Producers ]
                    │ (Sub-second Ingestion)
                    ▼
[ Amazon Kinesis Data Streams (On-Demand Mode) ]
                    │
                    ▼
[ Amazon Kinesis Data Firehose ]
  ├── Inline Data Transformation (Lambda): Unpack JSON, enrich IP geo-data
  ├── Format Conversion: Convert raw JSON to Apache Parquet with Snappy compression
  └── Dynamic Partitioning: Group files into S3 by `year=!{timestamp:yyyy}/month=!{timestamp:MM}/`
                    │
                    ▼
       [ Amazon S3 Data Lake Bucket ]
                    │
                    ▼ (Serverless SQL Queries)
[ Amazon Athena + Amazon QuickSight / Looker BI ]
```
- **Cost & Performance Optimization**: Converting JSON to columnar Parquet cuts Athena query scan volumes and query costs by 95% while speeding up analytical query performance by 10x.
---

## 7. Advanced Messaging Mechanics, Protocols & Enterprise Integration (from SolsRef)

### 7.1 SQS Standard vs SQS FIFO Deep Dive
```
┌───────────────────────────────────────────────┬───────────────────────────────────────────────┐
│ Amazon SQS Standard                           │ Amazon SQS FIFO (First-In-First-Out)          │
├───────────────────────────────────────────────┼───────────────────────────────────────────────┤
│ • Unlimited Throughput (nearly infinite TPS)  │ • 300 msg/sec (3,000 with batching)           │
│ • At-Least-Once Delivery (occasional dupes)   │ • Exactly-Once Processing (via Deduplication) │
│ • Best-Effort Ordering                        │ • Strict First-In-First-Out Ordering          │
│ • No Message Group ID required                │ • Message Group ID enables parallel groups    │
│ • Optimal for decoupled, idempotent workers   │ • Optimal for financial ledgers, order queues │
└───────────────────────────────────────────────┴───────────────────────────────────────────────┘
```
- **FIFO High Throughput Mode**: Enables partitioning across Message Group IDs, scaling FIFO throughput up to **70,000 transactions per second**.
- **Message Deduplication ID**: Within a 5-minute deduplication window, duplicate messages sharing the same deduplication ID (or content hash via SHA-256) are silently dropped.

### 7.2 Amazon MQ: Managed Message Broker for Hybrid & Legacy Migration
- **When to Choose Amazon MQ over SQS / SNS**:
  - Legacy on-premises applications built on standard open enterprise messaging protocols:
    - **AMQP 0-9-1 / 1.0**
    - **JMS (Java Message Service)**
    - **MQTT** (IoT devices)
    - **STOMP**
    - **OpenWire**
  - Managed engines: **Apache ActiveMQ** (High-availability active-standby with shared EFS storage) and **RabbitMQ** (Multi-AZ 3-node cluster).
  - Eliminates the need to refactor existing enterprise software to proprietary AWS SDK SQS/SNS APIs.

### 7.3 Amazon AppSync: Serverless GraphQL & Real-Time Sync
- **Architecture**:
  - Exposes a unified GraphQL schema aggregating multiple backend data sources:
    - Amazon DynamoDB (Direct resolver)
    - AWS Lambda (Business logic computation)
    - Amazon OpenSearch (Full-text search)
    - Amazon Aurora Serverless / HTTP endpoints
  - Real-time client updates over WebSockets without maintaining persistent EC2 WebSocket servers.
  - Client-side offline storage with automated conflict resolution algorithms (Optimistic Concurrency Control, Automerge).
---

## 8. SQS Visibility Timeout, Long Polling & DLQ Redrive Policies (from SolsRef)

### 8.1 SQS Message Visibility Lifecycle
```
[ Message Arrives in SQS ]
           │
           ▼
[ Consumer A Polls Queue: Receives Message ]
           │
           ▼ (Message enters Visibility Timeout: Default 30s)
┌────────────────────────────────────────────────────────────────────────┐
│ Message remains in queue, but is INVISIBLE to Consumer B, C, D...       │
│                                                                        │
│ Scenario 1: Consumer A finishes processing ──► Calls DeleteMessage ──► [ Deleted ]
│                                                                        │
│ Scenario 2: Consumer A crashes / times out ──► Timeout Expires ──────► [ Becomes Visible Again ]
│                                                                        │
│ Scenario 3: Consumer A needs more time ─────► Calls ChangeMessageVisibility ──► [ Extends Timeout ]
└────────────────────────────────────────────────────────────────────────┘
```
- **Visibility Timeout Configuration**: Range is 0 seconds to **12 Hours**. If a worker fails to process the message before the timer expires, the message is released back to the queue and processed by another consumer, guaranteeing at-least-once delivery.

### 8.2 SQS Long Polling vs Short Polling
- **Short Polling (`WaitTimeSeconds: 0`)**: The receive request queries a subset of SQS storage servers and returns immediately, even if the queue is empty. Results in high empty-receive API costs and occasional empty responses when messages exist.
- **Long Polling (`WaitTimeSeconds: 1 to 20`)**: The request waits up to 20 seconds for messages to arrive across all storage servers. Slashes empty receive API calls by up to 90%, lowers costs, and reduces message delivery latency to near-zero as soon as a message is published.

---

## 9. Amazon Kinesis Family Architecture Deep Dive (from SolsRef)

| Dimension | Kinesis Data Streams | Kinesis Data Firehose | Kinesis Data Analytics |
|---|---|---|---|
| **Primary Function** | Real-time custom data stream ingestion and storage. | Near real-time data streaming and delivery to data stores. | Real-time stream processing using SQL or Apache Flink. |
| **Data Retention** | **24 Hours up to 365 Days** | **No storage** (Transient buffer only: 60s - 900s). | In-memory stream processing state. |
| **Ingestion Scaling** | **Shard-based**: 1 MB/s or 1,000 records/s in; 2 MB/s out per shard. Or On-Demand mode. | Fully serverless; automatically scales up to GBs/sec. | Automatically allocates Kinesis Processing Units (KPUs). |
| **Consumer Model** | Pull (Standard: 2 MB/s per shard) or Push (Enhanced Fan-Out: 2 MB/s per consumer). | Managed delivery targets: Amazon S3, Redshift, OpenSearch, HTTP endpoints, Splunk. | Emits processed output streams to Kinesis Data Streams or Firehose. |
| **Inline Transformation** | Custom worker code (Lambda, KCL, Flink). | **Native Lambda transformation** (JSON to Parquet conversion, data masking). | SQL windowing functions (`TUMBLING`, `HOPPING`, `SLIDING`). |

---

## 10. Big Data & Real-Time Analytics Reference Flows (from SolsRef)

### Flow 1: Big Data Batch Ingest & Lakehouse Pipeline
```
[ Operational Databases / Logs ] ──► [ AWS Glue / Dataflow ETL ]
                                               │ (Cleans, partitions, converts to Parquet)
                                               ▼
                                  [ Amazon S3 Data Lake ]
                                               │
               ┌───────────────────────────────┴───────────────────────────────┐
               ▼                                                               ▼
[ Amazon Athena (Ad-hoc SQL) ]                                 [ Amazon Redshift Spectrum ]
               │                                                               │
               └───────────────────────────────┬───────────────────────────────┘
                                               ▼
                         [ Amazon QuickSight / Looker BI Dashboards ]
```

### Flow 2: Real-Time IoT Telemetry & Anomaly Detection Pipeline
```
[ IoT Fleet / Mobile Devices ]
               │ (MQTT / HTTPS)
               ▼
      [ AWS IoT Core ]
               │ (IoT Topic Rules Engine)
               ▼
[ Amazon Kinesis Data Streams ]
               │
   ┌───────────┴───────────────────────────────────────────┐
   ▼                                                       ▼
[ Kinesis Data Analytics (Apache Flink) ]      [ Kinesis Data Firehose ]
   │ (Detects anomalous temperature spikes)                │ (Batches & converts to Parquet)
   ▼                                                       ▼
[ Amazon SNS Alert -> PagerDuty ]               [ Amazon S3 Historical Lake ]
```
---

## 11. Enterprise Event Routing & SaaS Ingestion: EventBridge Pipes & Amazon AppFlow (from SolsRef)

### 11.1 Amazon EventBridge Pipes Architecture
- **Context & Problem**: Point-to-point event streaming historically required writing and operating boilerplate AWS Lambda glue functions to consume records from DynamoDB Streams or SQS, filter them, transform the JSON schema, and forward them to targets.
- **EventBridge Pipes Blueprint**:
```
[ SOURCE ]                  [ FILTER ]                 [ ENRICHMENT ]              [ TARGET ]
┌─────────────────┐         ┌────────────────┐         ┌────────────────┐         ┌─────────────────┐
│ DynamoDB Streams│ ──────► │ Event Pattern  │ ──────► │ AWS Lambda /   │ ──────► │ AWS Step        │
│ Amazon SQS      │         │ Filtering      │         │ API Destination│         │ Functions       │
│ Amazon Kinesis  │         │ (Drop unneeded │         │ (Augment event │         │ Amazon SNS      │
│ Apache Kafka    │         │  events free)  │         │  with CRM data)│         │ Amazon EventBus │
└─────────────────┘         └────────────────┘         └────────────────┘         └─────────────────┘
```
- **Architectural Mechanics**:
  - **Zero Compute Code**: Eliminates dedicated polling Lambda functions, slashing operational overhead and runtime costs.
  - **Free Filtering**: Events filtered out at the Filter stage do not incur enrichment or target invocation charges.
  - **Strict Ordering Preservation**: Maintains strict per-partition or per-message-group ordering when streaming from DynamoDB Streams, Kinesis, or SQS FIFO.

### 11.2 Amazon AppFlow: Enterprise SaaS Data Integration
- **Functionality**: Fully managed bidirectional integration service that automates secure data transfer between Software-as-a-Service (SaaS) applications and AWS data stores.
- **Supported SaaS Sources & Destinations**:
  - **Sources**: Salesforce, ServiceNow, SAP, Slack, Zendesk, Marketo, Google Analytics.
  - **Destinations**: Amazon S3 (Data Lake), Amazon Redshift, Snowflake, Salesforce.
- **Security & Network Topologies**:
  - Natively integrates with **AWS PrivateLink**: SaaS data travels directly across private VPC endpoints rather than traversing the public internet, satisfying strict financial and healthcare compliance.
  - End-to-end data encryption using AWS KMS Customer Managed Keys (CMK).
- **Execution Triggers**: On-demand execution, scheduled execution (e.g. hourly incremental sync), or event-driven execution (triggered via Salesforce Webhooks).
---

## 12. AWS Step Functions Advanced Orchestration: Distributed Map & Callback Tokens (from SolsRef)

### 12.1 Step Functions Distributed Map (Large-Scale Parallelism)
- **Architecture**: A specialized state within Step Functions designed for extreme scale data processing:
  - Iterates over millions of objects stored in Amazon S3, CSV records, or JSON arrays.
  - Spawns up to **10,000 concurrent child workflow executions** in parallel.
  - Processes petabyte-scale data lakes with built-in concurrency rate limiting, batching, and error failure thresholds.

### 12.2 The Callback Pattern (`.waitForTaskToken`)
- **Use Case**: Pauses an automated state machine workflow to await an asynchronous external event that may take minutes, hours, or days (e.g. human manager approval, physical warehouse fulfillment).
- **Execution Flow**:
```
[ Step Functions Workflow Execution ]
                 │
                 ▼ Task: Resource: "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
[ Generates Unique Task Token (Secret Token: "AAAAK...") ]
                 │
                 ▼ (Puts message in SQS with Task Token)
[ External System / Human Approval Worker / Third-Party Webhook ]
   └── Receives task token, human clicks "APPROVE" link
                 │
                 ▼ Calls: SendTaskSuccess(taskToken="AAAAK...", output="{approved: true}")
[ Step Functions Workflow Resumes Execution Immediately ]
```

### 12.3 Robust Error Handling: Retry & Catch Specifications
```json
{
  "Type": "Task",
  "Resource": "arn:aws:lambda:us-east-1:123456789012:function:ProcessOrder",
  "Retry": [
    {
      "ErrorEquals": ["DatabaseUnavailableException", "States.Timeout"],
      "IntervalSeconds": 2,
      "MaxAttempts": 5,
      "BackoffRate": 2.0
    }
  ],
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "Next": "SendToDeadLetterQueue"
    }
  ]
}
```
---

## 13. Amazon SQS FIFO Architecture: Message Deduplication, Grouping & Throughput (from SolsRef)

### 13.1 FIFO Guaranteed Ordering & Deduplication Mechanics
- **Throughput Limits**: Standard FIFO queues support **300 transactions/sec** (send, receive, delete) without batching, or **3,000 messages/sec** with batching (10 messages per batch). High-throughput FIFO mode supports up to **70,000 transactions/sec**.
- **Message Deduplication ID**:
  - Every message sent to a FIFO queue must have a Deduplication ID.
  - SQS stores message IDs for a **5-minute deduplication window**. If an identical Deduplication ID is sent within 5 minutes, SQS acknowledges the send but does not deliver the duplicate message to consumers.
  - *Content-Based Deduplication*: If enabled, SQS automatically computes a SHA-256 hash of the message body to generate the Deduplication ID.
- **Message Group ID (Strict Per-Partition Ordering)**:
  - Mandatory tag for FIFO messages. All messages with the *same Message Group ID* are guaranteed to be delivered and processed in strict chronological order.
  - Messages with *different Message Group IDs* can be processed concurrently by multiple worker threads or consumers!
```
[ SQS FIFO Queue ]
├── Group "Tenant-A": [ Msg 1 ] ──► [ Msg 2 ] ──► [ Msg 3 ] ──► Worker 1 (Strict Order for Tenant A)
├── Group "Tenant-B": [ Msg 1 ] ──► [ Msg 2 ] ──► [ Msg 3 ] ──► Worker 2 (Strict Order for Tenant B)
└── Group "Tenant-C": [ Msg 1 ] ──► [ Msg 2 ] ──► [ Msg 3 ] ──► Worker 3 (Strict Order for Tenant C)
```
- **Dead-Letter Queue (DLQ) Redrive**:
  - Automatically moves poisoned messages to a DLQ after `maxReceiveCount` failures.
  - SQS DLQ Redrive allows administrators to inspect failed messages in the console, fix application bugs, and redrive the poisoned messages back to the source queue with a single click.
---

## 14. AWS AppSync GraphQL Architectures & Streaming Ingestion Decision Matrix (from SolsRef)

### 14.1 AWS AppSync Managed GraphQL Architecture
```
                                 [ Web, Mobile, IoT Client Applications ]
                                                    │
                                (HTTPS Queries / Mutations / WSS Subscriptions)
                                                    ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                        AWS AppSync Engine                              │
       │                                                                        │
       │  ├── Unified GraphQL Schema (Types, Queries, Mutations, Subscriptions) │
       │  ├── Built-in Server-Side In-Memory Cache (Per-Resolver TTL)           │
       │  ├── Authorization: API Key, Cognito User Pools, IAM, OIDC, Lambda     │
       │  └── Real-Time Pub/Sub Subscriptions over WebSockets                   │
       └────────────────────────────────────────────────────────────────────────┘
                                                    │
                                                    ├── Direct Resolvers (Zero Lambda code required!)
                                                    ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                       Downstream Data Sources                          │
       │  ├── Amazon DynamoDB (Single-table reads and transactional writes)     │
       │  ├── Amazon Aurora Serverless (SQL relational data via Data API)       │
       │  ├── Amazon OpenSearch Service (Full-text search queries)              │
       │  ├── AWS Lambda (Custom backend microservices / third-party REST APIs) │
       │  └── HTTP Endpoints (Legacy enterprise SOAP/REST services)             │
       └────────────────────────────────────────────────────────────────────────┘
```
- **Offline Synchronization**: Mobile clients using AWS Amplify leverage local SQLite storage to queue mutations offline and synchronize automatically upon reconnection with built-in conflict resolution (Optimistic Concurrency, Auto-Merge, Lambda Conflict Handler).

### 14.2 Streaming & Messaging Ingestion Decision Matrix
| Service | Ingestion Model | Ordering Guarantee | Replayability & Retention | Consumer Model | Best Production Fit |
|---|---|---|---|---|---|
| **Amazon SQS** | Pull (Polling) | Best-effort (Standard) or Strict FIFO ($< 70\text{k msg/s}$) | **No replay** (deleted upon acknowledgment); max 14 days retention | 1-to-1: Single consumer group processes each message | Microservice decoupling, asynchronous background job queuing. |
| **Amazon SNS** | Push | Best-effort (Standard) or Strict FIFO | **No replay** (instant fan-out push; zero storage retention) | 1-to-Many: Fan-out to thousands of HTTP/S, SQS, Email, SMS, Lambda endpoints | Event broadcast, emergency push notifications, fan-out pipelines. |
| **Amazon Kinesis Data Streams** | Pull (Standard) or Push (Enhanced Fan-Out) | **Strict per-shard chronological ordering** | **Full replayability**; retention 24 hours to **365 days** | Many-to-Many: Multiple consumer applications read identical streams independently | Real-time clickstream analytics, IoT metrics, real-time fraud scoring. |
| **Amazon Kinesis Data Firehose** | Push / Ingestion pipeline | Best-effort | **No replay**; streaming buffer (60s–900s or 1MB–128MB) | Managed delivery directly to S3, Redshift, OpenSearch, Splunk | Automated serverless data ingestion to data lakes with Parquet conversion. |
| **Amazon MSK (Apache Kafka)** | Pull | **Strict per-partition ordering** | **Full replayability**; configurable multi-year retention | High-scale Kafka Consumer Groups with partition rebalancing | Enterprise Apache Kafka workloads, multi-cloud event streaming. |
---

## 15. AWS Step Functions Standard vs Express Workflows: Throughput, Pricing & Semantics (from SolsRef)

### 15.1 Architectural Comparison Matrix
| Architectural Dimension | Standard Workflows | Express Workflows |
|---|---|---|
| **Maximum Execution Duration** | **Up to 1 Year** | **Up to 5 Minutes** |
| **Execution Rate (Scale)** | Over 2,000 executions / second | **Over 100,000 executions / second** |
| **Execution Semantics** | **Exactly-once** execution guarantee | **At-least-once** (Asynchronous) or **At-most-once** (Synchronous) |
| **Execution History & State** | Complete step-by-step visual execution history in AWS Console; stored for 90 days. | Execution details sent directly to **Amazon CloudWatch Logs**; no console visual debugger. |
| **Pricing Model** | Charged per **State Transition** ($0.025 per 1,000 transitions). | Charged per **Execution Count and Duration/Memory** (identical to Lambda pricing). |
| **Execution Modes** | Asynchronous only | **Synchronous** (returns result directly to API caller) & **Asynchronous**. |
| **Best Production Fit** | Long-running workflows, order fulfillment pipelines, human-in-the-loop approvals, payment checkouts. | High-volume IoT telemetry ingestion, real-time microservices coordination, high-throughput streaming ETL. |

### 15.2 Synchronous Express Workflow API Integration
```
[ Mobile / Web Client ] ──► [ Amazon API Gateway ] ──► (HTTP POST /checkout)
                                                        │
                                                        ▼ (Direct Native Integration)
                                         [ Synchronous Express Step Function ]
                                         ├── 1. Validate Inventory (DynamoDB)
                                         ├── 2. Process Stripe Payment (Lambda)
                                         └── 3. Generate Receipt (SES)
                                                        │
                                                        ▼ (Returns Execution Output directly!)
[ Immediate HTTP 200 OK Response ] ◄────────────────────┘
```
