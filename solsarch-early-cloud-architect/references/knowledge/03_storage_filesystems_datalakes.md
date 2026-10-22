# SolsBrain — Chapter 3: Storage, File Systems & Data Migration (Deep-Dive Edition)

**Domain:** Object Storage, Block Storage, Network File Systems, Hybrid Storage Gateways & Physical Data Transfer  
**Target Clouds:** AWS, Microsoft Azure, Google Cloud Platform (GCP)  
**Target Engine:** SolsArch Analyzer (Agent 6 Cloud Decider, Agent 1 Code Reader, Core Validator)  
**Version:** 2.0 (Exhaustive Technical Specification)

---

## 1. Amazon Simple Storage Service (S3) Deep Dive

### 1.1 Storage Class Economics, Durability & Access Tiers
All S3 storage classes (except One Zone-IA) distribute data synchronously across a minimum of **3 physical Availability Zones** providing **99.999999999% (11 9s) durability**.

| Storage Class | Availability SLA | Min Billable Size | Min Storage Duration | First-Byte Latency | Retrieval Fee | Lifecycle Transition Target |
|---|:---:|:---:|:---:|:---:|:---:|---|
| **S3 Standard** | 99.99% | None | None | Milliseconds | None | Baseline ingestion tier |
| **S3 Intelligent-Tiering** | 99.9% | None (Monitoring fee/obj) | None | Milliseconds | None | Auto-tiers between Frequent, Infrequent (30d), Archive Instant (90d), Archive (90-730d) |
| **S3 Standard-IA** | 99.9% | 128 KB | 30 days | Milliseconds | $0.01 / GB | Transition active data after 30–60 days |
| **S3 One Zone-IA** | 99.5% (Single AZ) | 128 KB | 30 days | Milliseconds | $0.01 / GB | Secondary backups, easily reproducible thumbnails |
| **S3 Glacier Instant Retrieval** | 99.9% | 128 KB | 90 days | Milliseconds | $0.03 / GB | Medical imaging, quarterly statements, audit logs |
| **S3 Glacier Flexible Retrieval** | 99.99% | 40 KB | 90 days | Exp: 1–5 min<br>Std: 3–5 hr<br>Bulk: 5–12 hr | $0.01–$0.03 / GB | Long-term archival with planned retrieval |
| **S3 Glacier Deep Archive** | 99.99% | 40 KB | 180 days | Std: 12 hr<br>Bulk: 48 hr | $0.0025 / GB | 7–10 year regulatory/compliance retention ($1/TB/mo) |

---

### 1.2 Replication Architecture: CRR vs. SRR
```
                      [ Source Bucket: us-east-1 ]
                                   │
              ┌────────────────────┴────────────────────┐
              │                                         │
              ▼                                         ▼
   [ Cross-Region (CRR) ]                    [ Same-Region (SRR) ]
   • Target: eu-central-1                    • Target: us-east-1 (Secondary account)
   • Disaster Recovery & Compliance          • Log aggregation, Dev/Prod isolation
   • Lower latency for European users        • Strict in-region data residency
   • Optional KMS Key re-encryption          • Preserves original replica ownership
```
*Prerequisite*: **Bucket Versioning must be ENABLED** on both source and destination buckets before replication can be configured. Existing objects prior to enabling replication require an S3 Batch Replication job.

---

### 1.3 S3 Security, Object Lock & Access Points

#### S3 Object Lock (WORM - Write Once, Read Many):
- Blocks deletion or overwriting of specific object versions for a fixed retention period or indefinite legal hold.
- **Compliance Mode**:
  - The retention period CANNOT be decreased or bypassed by any user (including root account).
  - Essential for SEC Rule 17a-4, FINRA, and healthcare compliance.
- **Governance Mode**:
  - Specific IAM users with `s3:BypassGovernanceRetention` can override retention or delete objects.
- **Legal Hold**:
  - Indefinite retention period flag; prevents deletion until explicitly cleared via `s3:PutObjectLegalHold`.

#### S3 Multi-Region Access Points (MRAP):
- Delivers a single global endpoint (`arn:aws:s3::account:accesspoint/mrap-alias`) spanning buckets across multiple AWS regions.
- Routes user traffic through AWS Global Accelerator to the closest regional bucket dynamically, achieving up to 60% latency reduction.

---

## 2. Amazon Elastic Block Store (EBS) Deep Dive

### 2.1 Volume Types, Burst Buckets & Throughput Limits
| Volume Type | Max Size | Max IOPS | Max Throughput | IOPS : Storage Ratio | Performance Characteristics |
|---|:---:|:---:|:---:|:---:|---|
| **General Purpose (`gp3`)** | 16 TiB | 16,000 | 1,000 MB/s | Decoupled | Baseline 3,000 IOPS & 125 MB/s free; scale IOPS/throughput independently |
| **General Purpose (`gp2`)** | 16 TiB | 16,000 | 250 MB/s | 3 IOPS / GiB | Burst bucket up to 3,000 IOPS; performance tied directly to disk capacity |
| **Provisioned IOPS (`io2`)** | 64 TiB | 256,000 | 4,000 MB/s | 1,000 IOPS / GiB | Sub-millisecond latency, 99.999% durability (EBS Block Express) |
| **Throughput Optimized (`st1`)** | 16 TiB | 500 | 500 MB/s | Sequential | Magnetic HDD; cannot be used as an EC2 boot volume |
| **Cold HDD (`sc1`)** | 16 TiB | 250 | 250 MB/s | Sequential | Lowest cost magnetic storage for cold sequential log analysis |

### 2.2 Software RAID Configurations on EBS
When a single `gp3` or `io2` volume cannot provide sufficient IOPS or throughput for extreme database workloads:
- **RAID 0 (Striping)**:
  - Spans I/O across multiple EBS volumes.
  - Combines throughput (e.g., 2x `gp3` volumes delivering 2,000 MB/s aggregate throughput).
  - *Risk*: Zero fault tolerance; if one volume fails, the entire RAID array is corrupted. Must have automated EBS snapshots.
- **RAID 1 (Mirroring)**:
  - Writes identical data simultaneously to two EBS volumes.
  - Fault tolerant; expensive (2x storage cost). Rarely used since EBS already mirrors data across multiple servers in an AZ.
- *Rule*: Never use RAID 5 or RAID 6 on EBS; parity calculation overhead creates severe I/O bottlenecks.

---

## 3. Network File Systems: Amazon EFS vs. Amazon FSx

### 3.1 Amazon EFS (Elastic File System)
- **Architecture**: POSIX-compliant distributed network file system (NFSv4.1) mounted simultaneously across thousands of EC2 instances, ECS Fargate containers, and Lambda functions.
- **Throughput Modes**:
  - **Elastic Mode**: Automatically scales throughput to match active workload demands (ideal for spiky, unpredictable jobs).
  - **Provisioned Mode**: Pre-allocates dedicated throughput (e.g., 500 MB/s) independent of the amount of data stored.
- **Storage Lifecycle**:
  - Primary Storage (Standard) automatically transitions inactive files to **EFS Infrequent Access (EFS IA)** after 7–90 days, slashing storage costs by 92%.

### 3.2 Amazon FSx Family Selection
| FSx Variant | Native OS Protocol | Underline Storage | Key Features | Primary Architectural Workload |
|---|---|---|---|---|
| **FSx for Windows File Server** | SMB 2.0–3.1.1 | NTFS | Active Directory integration, DFS, shadow copies | Enterprise Windows apps, shared enterprise network drives |
| **FSx for Lustre** | POSIX / Linux | NVMe / High-Throughput HDD | Millions of IOPS, sub-ms latency, direct S3 linkage | High-Performance Computing (HPC), AI/ML training, financial modeling |
| **FSx for NetApp ONTAP** | NFS, SMB, iSCSI | Multi-protocol | Snapshot branching, deduplication, thin provisioning | Cross-cloud enterprise migration, VMware Cloud on AWS |
| **FSx for OpenZFS** | NFS (v3, v4) | ZFS | Up to 1M IOPS, 12.5 GB/s throughput, instant snapshots | Linux application modernization from on-premises ZFS |

---

## 4. Hybrid Cloud Storage: AWS Storage Gateway

Bridges on-premises environments with AWS cloud storage without refactoring existing legacy applications:

```
[ On-Premises Corporate Data Center ]                 [ AWS Cloud ]
  │
  ├── File Gateway (NFS / SMB) ──────────────────────> Amazon S3 (Standard / IA / Glacier)
  │     (Local cache of recent files)
  │
  ├── Volume Gateway:
  │     ├── Cached Volumes (Primary data in S3; ─────> Amazon S3 (EBS Snapshots for recovery)
  │     │   frequently accessed data cached locally)
  │     └── Stored Volumes (Primary data on-prem; ───> Scheduled EBS Snapshots in AWS
  │         asynchronously backed up to S3)
  │
  └── Tape Gateway (VTL - Virtual Tape Library) ─────> Amazon S3 Glacier & Glacier Deep Archive
        (Replaces physical magnetic tape infrastructure with iSCSI tape drive emulation)
```

---

## 5. Physical Bulk Data Migration: AWS Snow Family

When moving terabytes or petabytes of data over standard internet connections would take months due to bandwidth saturation:

$$\text{Transfer Time (Days)} = \frac{\text{Total Data (Bits)}}{\text{Effective Bandwidth (Bits/sec)} \times 86,400}$$

*Rule of thumb*: If network transfer of data will take **more than 7 days**, order an AWS Snow Family physical appliance.

| Appliance | Usable Storage | Onboard Compute | Clustering Support | Primary Use Case |
|---|:---:|:---:|:---:|---|
| **AWS Snowcone** | 8 TB SSD | 2 vCPUs, 4 GB RAM | No | Edge collection, drones, healthcare mobile units, offline IoT |
| **Snowball Edge Storage Optimized** | 80 TB HDD | 40 vCPUs, 80 GB RAM | Yes | Large-scale data migration (up to petabytes), S3 bucket seeding |
| **Snowball Edge Compute Optimized** | 42 TB NVMe | 104 vCPUs, 416 GB RAM | Yes (Up to 16 nodes) | Edge AI inference, tactical military edge processing, video transcoding |
| **AWS Snowmobile** | Up to 100 PB | Dedicated 45-foot container | N/A | Exabyte-scale data center evacuation (shipped via semi-truck) |

---

## 6. Formal SolsArch Storage Validation Rules

| Rule Code | Severity | Trigger Condition | Remediation Guidance |
|---|:---:|---|---|
| `STR-S3-001` | **CRITICAL** | Production S3 bucket has versioning disabled and lacks cross-region backup replication | Enable S3 Bucket Versioning and configure Cross-Region Replication (CRR) for disaster recovery |
| `STR-S3-002` | **HIGH** | S3 bucket stores unstructured objects without an automated Lifecycle Rule | Configure lifecycle policy transitioning objects to Standard-IA after 30d and Glacier after 90d |
| `STR-EBS-001` | **MEDIUM** | Boot volume uses legacy `gp2` rather than `gp3` | Migrate volume type to `gp3` to gain 20% cost savings and 3,000 baseline IOPS decoupling |
| `STR-EFS-001` | **HIGH** | Highly available multi-AZ compute cluster attaches an `EFS One Zone` storage system | Migrate to standard Amazon EFS to ensure multi-AZ redundancy and fault tolerance |
| `STR-HYB-001` | **HIGH** | On-premises data migration plan specifies network upload of > 50 TB over < 100 Mbps link | Provision an AWS Snowball Edge appliance to perform physical bulk offline transfer |
---

## 4. Production Storage & Data Transfer Best Cases (from SolsRef)

### Best Case 3.1: Pre-Signed URL Direct Upload Architecture (Bypassing API Gateway 10MB Limit)
- **Context & Problem**: Amazon API Gateway enforces a strict, non-configurable 10 MB payload size limit. Routing large file uploads (videos, high-resolution scans, logs) through API Gateway and Lambda causes HTTP 413 Payload Too Large errors and inflates Lambda execution memory costs.
- **Architecture Blueprint**:
```
[ Client Browser / Mobile App ]
      │
      ├── 1. Request Upload Ticket: GET /api/v1/upload-url?file=sample.mp4
      │      │
      │      ▼
      │   [ Amazon API Gateway + Lambda Function ]
      │      └── Generates S3 Pre-Signed PUT URL (ExpiresIn: 900s)
      │      │
      │   ◄── Returns Pre-Signed URL: https://mybucket.s3.amazonaws.com/uploads/...
      │
      └── 2. Direct Binary Upload: HTTP PUT (Payload up to 5 GB direct to S3)
             │
             ▼
      [ Amazon S3 Bucket ]
             │
             ▼ (S3 Event Notification: s3:ObjectCreated:*)
      [ EventBridge / Lambda / SQS Downstream Processing ]
```
- **Key Architectural Rules**:
  - Pre-signed URLs must specify strict content length ranges and exact MIME types in the signed policy to prevent malicious clients from uploading oversized or unauthorized files.
  - Bucket CORS must explicitly permit `PUT` operations from authorized client domains.

### Best Case 3.2: Offline Bulk Petabyte Ingestion: Snowball Edge with 0-Day S3 Glacier Transition
- **Context & Problem**: Migrating 100 TB to 10 PB of historical archives over WAN would take months and saturate corporate internet connections. Furthermore, AWS Snowball Edge cannot directly import data into Amazon S3 Glacier or Glacier Deep Archive.
- **Architecture Blueprint**:
```
[ On-Premises Data Center ]
        │ (10 Gbps Local Network Transfer)
        ▼
[ AWS Snowball Edge Storage Optimized Appliance (80 TB / 210 TB) ]
        │ (UPS / Freight Courier Shipping)
        ▼
[ AWS Cloud Ingestion Facility ]
        │
        ▼ (Loads data into Staging S3 Bucket)
[ Amazon S3 Standard Staging Bucket ]
        │
        ▼ (S3 Lifecycle Transition Rule configured for 0-Day Transition)
[ Amazon S3 Glacier Flexible Archive / Glacier Deep Archive ]
```
- **Lifecycle Configuration**:
  ```xml
  <LifecycleConfiguration>
    <Rule>
      <ID>ImmediateGlacierTransition</ID>
      <Status>Enabled</Status>
      <Filter><Prefix>archive/</Prefix></Filter>
      <Transition>
        <Days>0</Days>
        <StorageClass>DEEP_ARCHIVE</StorageClass>
      </Transition>
    </Rule>
  </LifecycleConfiguration>
  ```
- **Result**: Data is staged in S3 Standard for less than 24 hours, automatically transitioning to Glacier Deep Archive ($0.00099/GB/month) with near-zero S3 Standard storage costs.

### Best Case 3.3: Continuous Hybrid File System Migration: AWS DataSync for On-Premises NAS to EFS/FSx/S3
- **Context & Problem**: Enterprises migrating petabyte-scale file shares from NetApp/Isilon NAS systems to AWS cannot afford days of offline migration downtime.
- **Architecture Blueprint**:
```
[ On-Premises Data Center ]
  [ Enterprise NAS (NFS / SMB) ]
          │ (Local Network)
          ▼
  [ AWS DataSync Agent VM (VMware ESXi / Hyper-V) ]
          │
          ▼ (Encrypted TLS over AWS Direct Connect or Site-to-Site VPN)
[ AWS Cloud: AWS DataSync Service ]
          │
          ├── Continuous Scheduled Delta Synchronization
          ├── Preserves POSIX Metadata, Ownership, Permissions, Timestamps
          ├── Automated End-to-End Checksum Integrity Verification
          │
          ▼
  [ Target AWS Storage: Amazon EFS / FSx for Windows / Amazon S3 ]
```
- **Cutover Strategy**: Run initial synchronization while on-premises applications remain live. Run daily delta syncs. During final 1-hour maintenance window, freeze writes on-premise, run final DataSync job, and switch DNS/mount targets to AWS EFS/FSx.

### Best Case 3.4: High-Performance Distributed POSIX File Systems: EFS vs FSx for Lustre for AI/HPC
| Feature | Amazon EFS | Amazon FSx for Lustre |
|---|---|---|
| **Protocol** | NFSv4.0 / NFSv4.1 | Lustre (Parallel Distributed File System) |
| **Throughput Scaling** | Up to 10+ GB/s, millions of IOPS (Elastic Mode) | Hundreds of GB/s, millions of IOPS, sub-millisecond latencies |
| **S3 Integration** | DataSync or manual batch scripts | Native seamless bi-directional synchronization with S3 buckets |
| **Typical Fit** | Container shared storage, web serving, CMS, home directories | Deep learning model training (PyTorch/TensorFlow), financial modeling, HPC |
---

## 8. Enterprise S3 Governance, Storage Gateways & FSx Portfolio (from SolsRef)

### 8.1 S3 Access Control, Governance & Multi-Region Access Points (MRAP)
- **S3 Block Public Access (The 4 Levers)**:
  1. `BlockPublicAcls`: Prevents newly uploaded objects/buckets from granting public ACLs.
  2. `IgnorePublicAcls`: Ignores all existing public ACLs on the bucket.
  3. `BlockPublicPolicy`: Rejects any bucket policy that grants public access.
  4. `RestrictPublicBuckets`: Restricts access to public and cross-account access to only AWS services and authorized users.
- **S3 Object Lock (WORM Compliance)**:
  - **Retention Period**: Specifies the exact number of days/years an object version cannot be deleted.
  - **Governance Mode**: Protects objects from deletion; however, users with the specific IAM permission `s3:BypassGovernanceRetention` can override or delete the object.
  - **Compliance Mode**: Strictly enforces that NO user—including the AWS Account Root User—can delete or overwrite the object until the retention period expires. Mandatory for SEC Rule 17a-4, FINRA, and CFTC compliance.
  - **Legal Hold**: Indefinite freeze placed on an object without an expiration date; remains until explicitly removed.
- **S3 Multi-Region Access Points (MRAP)**: Provides a single global endpoint (`s3://mrap-...`) that routes client requests across multi-region S3 buckets using AWS Global Accelerator, dynamically routing clients to the lowest-latency healthy S3 bucket.

### 8.2 AWS Storage Gateway Portfolio Architecture
| Gateway Type | Primary Protocol | Storage Destination | Operational Mechanism & Best Fit |
|---|---|---|---|
| **Amazon S3 File Gateway** | NFS v3 / v4.1, SMB 2.0 / 3.0 | Amazon S3 Standard, S3-IA, Glacier | Maps on-premises file shares directly to S3 objects 1:1. Maintains local read/write cache for low latency. |
| **Amazon FSx File Gateway** | SMB | Amazon FSx for Windows File Server | Seamless on-premises local caching for Windows group shares hosted in AWS FSx. |
| **Volume Gateway (Cached)** | iSCSI Block Storage | Amazon S3 (as primary storage) | Retains only frequently accessed data on local storage; primary storage resides in S3. Slashes on-premises storage hardware costs. |
| **Volume Gateway (Stored)** | iSCSI Block Storage | On-Premises (as primary storage) | Entire data volume stored locally on-premises; asynchronously backed up to S3 as point-in-time EBS Snapshots. |
| **Tape Gateway (VTL)** | iSCSI VTL (Virtual Tape Library) | S3 Glacier Flexible & Glacier Deep Archive | Emulates physical tape drives and tape autoloaders (compatible with Veeam, Commvault, Veritas); eliminates physical tape handling. |

### 8.3 Amazon FSx Enterprise File System Portfolio
- **FSx for Windows File Server**: Fully managed native Microsoft Windows file system. Built on Windows Server, supports native SMB, Active Directory join, Windows NTFS permissions, Distributed File System (DFS) namespaces, and Multi-AZ automatic failover.
- **FSx for Lustre**: Parallel distributed POSIX file system designed for HPC, machine learning, financial simulation, and seismic processing. Natively synchronizes with Amazon S3 data buckets, delivering hundreds of GB/s throughput and millions of IOPS with sub-millisecond latencies.
- **FSx for NetApp ONTAP**: Enterprise multi-protocol storage supporting NFS, SMB, and iSCSI. Provides block-level deduplication, data compression, instant NetApp Snapshot technology, and cross-region replication using NetApp SnapMirror.
- **FSx for OpenZFS**: Fully managed OpenZFS file system providing high IOPS (up to 1,000,000 IOPS) and throughput for Linux and Unix workloads with instant ZFS point-in-time cloning.
---

## 9. S3 Performance Optimization & Partitioning Architecture (from SolsRef)

### 9.1 S3 Prefix Partitioning & Request Scaling Limits
- **Baseline Limits per Prefix**:
  - **3,500 PUT / POST / DELETE** requests per second per prefix.
  - **5,500 GET / HEAD** requests per second per prefix.
- **Horizontal Scaling via Prefix Architecture**:
  - In Amazon S3, a "prefix" is any string between the bucket name and the object name (e.g. `s3://mybucket/folder1/subfolder2/file.jpg` has the prefix `/folder1/subfolder2/`).
  - There is **no limit** to the number of prefixes in an S3 bucket.
  - By distributing writes across 10 prefixes, an application can achieve **35,000 PUTs/sec** and **55,000 GETs/sec**.
  - *Historical Note*: Hash prefixes (e.g. `s3://mybucket/2a8f-2026-10-05/...`) are no longer required because AWS automatically scales partitions under the hood; however, distinct logical folder paths scale partitions independently.

### 9.2 High-Throughput Transfer Patterns
- **Multipart Upload Architecture**:
  - Mandatory for objects $> 5 \text{ GB}$; strongly recommended for objects $> 100 \text{ MB}$.
  - Splits file into parallel chunks (from 5 MB to 5 GB per part).
  - Maximizes network bandwidth utilization via parallel connections and allows retrying only failed individual parts rather than restarting the entire multi-gigabyte upload.
  - *Automated Hygiene*: Configure an S3 Lifecycle Rule with `AbortIncompleteMultipartUpload` set to 7 days to eliminate hidden storage charges from orphaned upload fragments.
- **S3 Byte-Range Fetches**:
  - Parallelizes downloads of large files by requesting specific byte ranges concurrently (`Range: bytes=0-1048575`).
  - Accelerates video streaming (delivering the first 5 seconds instantly) and enables querying file headers or specific Parquet/ORC row groups without downloading entire gigabyte datasets.
- **S3 Transfer Acceleration**:
  - Accelerates global uploads by having clients send data to the nearest AWS Edge Location (over 600+ worldwide). Data is routed over the congestion-free, optimized AWS global private fiber network to the target regional bucket.

---

## 10. EBS Volume Types Sizing & Multi-Attach Matrix (from SolsRef)

### 10.1 Complete EBS Volume Types Specification
| Volume Type | API Name | Max IOPS | Max Throughput | Baseline Performance | Typical Production Fit |
|---|---|---|---|---|---|
| **General Purpose SSD** | `gp3` | **16,000 IOPS** | **1,000 MB/s** | Baseline 3,000 IOPS and 125 MB/s included free; independently provision extra IOPS/throughput. | Default volume for 90% of enterprise workloads: virtual desktops, dev/test, boot volumes, microservices. |
| **General Purpose SSD (Legacy)** | `gp2` | 16,000 IOPS | 250 MB/s | Baseline 3 IOPS/GB (e.g. 1,000 GB = 3,000 IOPS); burstable to 3,000 IOPS via I/O credits. | Legacy architectures; migrate to `gp3` for 20% lower cost and decoupled performance. |
| **Provisioned IOPS SSD** | `io2` | **64,000 IOPS** | **1,000 MB/s** | 500 IOPS per GB; **99.999% volume durability** (vs 99.8% for gp3). Sub-millisecond latency. | Mission-critical enterprise relational databases (Oracle, Microsoft SQL Server, PostgreSQL, SAP HANA). |
| **Provisioned IOPS Block Express** | `io2 Block Express` | **256,000 IOPS** | **4,000 MB/s** | 1,000 IOPS per GB; sub-millisecond latency; max volume size 64 TiB. | SAN replacement, extreme OLTP transactional databases, core banking engines. |
| **Throughput Optimized HDD** | `st1` | 500 IOPS | 500 MB/s | Magnetic storage; burstable up to 500 MB/s per volume. | Big Data HDFS clusters, Apache Kafka, data warehousing, heavy sequential log processing. *Cannot be boot volume.* |
| **Cold HDD** | `sc1` | 250 IOPS | 250 MB/s | Magnetic storage; lowest-cost block storage on AWS ($0.015/GB/month). | Cold historical data requiring block storage access; large file servers. *Cannot be boot volume.* |

### 10.2 EBS Multi-Attach Architecture
- **Mechanics**: Allows attaching a single Provisioned IOPS volume (`io1` or `io2`) to up to **16 EC2 instances simultaneously** within the *same Availability Zone*.
- **Prerequisite**: Applications must use a **cluster-aware distributed file system** (such as GFS2, OCFS2, or Veritas InfoScale) to arbitrate concurrent write locks and prevent catastrophic disk corruption. Standard ext4 or NTFS will corrupt instantly if mounted concurrently!

---

## 11. Enterprise Data Protection: AWS Backup & Vault Lock WORM (from SolsRef)

```
[ Central Backup Administrator Account ]
                    │
                    ▼ (AWS Organizations Cross-Account Backup Policy)
    ┌───────────────┼───────────────────────────────┐
    ▼               ▼                               ▼
[ Account A ]   [ Account B ]                   [ Account C ]
  EC2, RDS,       EBS Volumes,                    DynamoDB,
  Aurora          EFS File Systems                S3 Buckets
    │               │                               │
    └───────────────┼───────────────────────────────┘
                    ▼ (Centralized Encrypted Backup Vault)
        [ AWS Backup Vault Lock (Compliance Mode) ]
        ├── MinRetentionDays: 30, MaxRetentionDays: 2555 (7 Years)
        ├── WORM Enforced: Immutable snapshots
        └── ZERO DELETIONS PERMITTED (Even by AWS Account Root User)
```
---

## 12. S3 Deep Dive: Glacier Retrieval Tiers, Replication (CRR/SRR) & S3 Select (from SolsRef)

### 12.1 S3 Glacier & Glacier Deep Archive Retrieval Tiers
| Storage Class | Retrieval Option | Retrieval Latency | Relative Retrieval Cost | Optimal Production Fit |
|---|---|---|:---:|---|
| **S3 Glacier Flexible Archive** | **Expedited** | **1 to 5 Minutes** | High ($0.03/GB + $10/1k requests) | Emergency medical scans, sudden regulatory audits, active incident response. |
| | **Standard** | **3 to 5 Hours** | Low ($0.01/GB + $0.05/1k requests) | Routine historical analysis, scheduled overnight reporting. |
| | **Bulk** | **5 to 12 Hours** | **Lowest** ($0.0025/GB + $0.025/1k requests) | Large batch data restores (100+ TB), petabyte-scale machine learning training datasets. |
| **S3 Glacier Deep Archive** | **Standard** | **12 Hours** | Standard ($0.02/GB) | Retrieval of long-term compliance archives. |
| | **Bulk** | **48 Hours** | Lowest ($0.0025/GB) | Retrieval of historical data where 2-day SLA is acceptable ($0.00099/GB/mo storage). |

### 12.2 S3 Replication Architecture: CRR vs SRR
- **Cross-Region Replication (CRR)**: Asynchronously replicates objects across different AWS regions for global compliance, latency reduction, and disaster recovery.
- **Same-Region Replication (SRR)**: Asynchronously replicates objects within the same region across different accounts (e.g. replicating production logs to a centralized Log Archive account) or for development environment synchronization.
- **S3 Replication Time Control (S3 RTC)**: Enterprise SLA guaranteeing that **99.9% of newly uploaded objects are replicated within 15 minutes**, providing detailed CloudWatch metrics on replication latency and pending operations.

### 12.3 Amazon S3 Select & Glacier Select
- **Performance Optimization**: Enables applications to run serverless SQL expressions (`SELECT s.id, s.amount FROM s3object s WHERE s.status = 'PAID'`) directly on uncompressed or GZIP/BZIP2-compressed CSV, JSON, and Apache Parquet objects stored in S3.
- **Benefits**:
  - Instead of downloading a 20 GB log file to an EC2 instance or Lambda function and filtering locally, S3 Select executes filtering on the S3 storage node, returning only the 2 MB matching payload.
  - Slashes network transfer costs and client-side memory consumption by up to **90%**, accelerating query processing by up to $4	imes$.
---

## 13. Amazon S3 Event Notifications, Batch Operations & Versioning Lifecycle (from SolsRef)

### 13.1 S3 Event Notifications Architecture & EventBridge Integration
- **Direct S3 Notifications**: S3 can send notifications directly to Amazon SNS, Amazon SQS, or AWS Lambda upon events (`s3:ObjectCreated:*`, `s3:ObjectRemoved:*`, `s3:ObjectRestore:*`, `s3:Replication:*`).
  - *Limitation*: Direct notifications do not support multiple targets with identical prefixes or advanced metadata filtering.
- **Amazon EventBridge S3 Integration (Best Practice)**:
  - When enabled, S3 emits all bucket events directly onto the Amazon EventBridge default event bus.
  - Provides advanced filtering by object size (`detail.object.size > 10485760`), file extensions, user identity, and routes events to **over 20+ AWS service targets** with automated retries and dead-letter queues.

### 13.2 Amazon S3 Batch Operations Architecture
- **Functionality**: Fully managed service for executing large-scale batch operations across **billions of objects containing petabytes of data** with a single API request or console job.
- **Supported Batch Actions**:
  - **Copy Objects**: Mass replication across buckets, changing storage classes (e.g. bulk tiering to Glacier), or modifying KMS encryption keys.
  - **Replace Tag Sets**: Batch tagging of millions of objects for cost allocation or lifecycle filtering.
  - **Replace Access Control Lists (ACLs)**: Enforcing bucket-owner-full-control across all objects.
  - **Initiate Glacier Restores**: Bulk restoration of archived datasets.
  - **Invoke AWS Lambda**: Executes custom Python/Node.js logic per object (e.g. watermarking images, transcribing audio files).
- **Execution Mechanism**: Consumes an **Amazon S3 Inventory Report** (CSV/Parquet manifest of bucket objects), provisions managed serverless worker fleets, tracks completion progress in real time, and emits a detailed audit completion report.

### 13.3 S3 Versioning Mechanics, Delete Markers & MFA Delete
- **Delete Markers**: When an object is deleted in a versioned bucket without specifying a Version ID, S3 does *not* delete the data; it inserts a **Delete Marker** with a unique Version ID. The object appears deleted to GET requests. Deleting the Delete Marker instantly restores the previous object version.
- **MFA Delete**:
  - Requires a physical hardware or virtual Multi-Factor Authentication (MFA) TOTP code to:
    1. Permanently delete an object version (`s3:DeleteObjectVersion`).
    2. Suspend or change the versioning state of the bucket (`s3:PutBucketVersioning`).
  - *Operational Rule*: MFA Delete can **only be enabled via the AWS CLI or SDK using the root account credentials**; it cannot be enabled or disabled via the AWS Management Console!
---

## 14. AWS Big Data & Data Lakehouse Architecture: Glue, Athena & EMR (from SolsRef)

### 14.1 AWS Glue Data Integration Engine
- **AWS Glue Data Catalog**: Universal Apache Hive-compatible metastore that stores metadata (table schemas, partitions, data types) for data stored in S3, RDS, DynamoDB, or on-premises databases.
- **Glue Crawlers**: Automated processes that connect to source data stores, infer schema definitions and file formats (JSON, CSV, Parquet, Avro, ORC), and automatically register or update table schemas in the Data Catalog.
- **Glue ETL Jobs**: Serverless distributed data processing using Apache Spark, Python Shell, or Ray.
  - *DynamicFrames*: Glue's native distributed table abstraction that extends Apache Spark DataFrames by handling semi-structured data, messy records, and nested schemas without schema errors.
  - *Job Bookmarks*: Maintains state across recurring ETL runs to process only newly arrived S3 objects, preventing duplicate processing.

### 14.2 Amazon Athena Serverless Query Optimization
- **Execution Model**: Serverless Presto/Trino SQL query engine that queries data directly in Amazon S3 without loading or transforming it.
- **Cost & Performance Optimization Matrix**:
  $$\text{Athena Query Cost} = \$5.00 \text{ per TB of data scanned}$$
  - **Columnar Formats**: Converting CSV/JSON to Apache Parquet or ORC reduces scanned data volume by **80% to 90%**, slashing both cost and query latency proportionally.
  - **Data Partitioning**: Partitioning by date (`s3://bucket/year=2026/month=10/day=05/`) allows Athena to prune partitions, scanning only relevant prefixes.
  - **Partition Projection**: Bypasses Glue Data Catalog metadata lookups for highly partitioned tables, eliminating catalog query throttling for millions of partitions.

### 14.3 Amazon EMR (Elastic MapReduce) Fleet Topologies & Cost Optimization
```
                                ┌──────────────────────────────────────┐
                                │        Master Node (Primary)         │
                                │  Coordinates cluster, runs YARN      │
                                │  Resource Manager, NameNode.         │
                                │  * MUST BE On-Demand or Reserved!    │
                                └──────────────────────────────────────┘
                                                   │
                   ┌───────────────────────────────┴───────────────────────────────┐
                   ▼                                                               ▼
┌──────────────────────────────────────┐                       ┌──────────────────────────────────────┐
│          Core Nodes (Storage)        │                       │          Task Nodes (Compute Only)   │
│  Runs TaskTracker / DataNode.        │                       │  Executes tasks only; NO HDFS data.  │
│  Stores data in HDFS + computes.     │                       │  Safe for termination without data   │
│  * High risk if terminated!          │                       │  loss.                               │
│  * Use On-Demand or Instance Fleets! │                       │  * 100% SPOT INSTANCES! (Slashing    │
│                                      │                       │    compute costs by 70–90%!)         │
└──────────────────────────────────────┘                       └──────────────────────────────────────┘
```
---

## 15. AWS Storage Gateway Suite: S3 File, FSx File, Volume & Tape Gateways (from SolsRef)

### 15.1 Architectural Topology & Protocol Matrix
```
[ On-Premises Corporate Data Center ]                              [ AWS Cloud Storage ]
                                             Network WAN / Direct Connect
  App Servers ──► NFS / SMB Mount ──► [ S3 File Gateway VM ] ────────► Amazon S3 (Standard, IA, Glacier)
                                        (Local SSD Cache)

  Windows Clients ─► SMB Mount ─────► [ FSx File Gateway VM ] ───────► Amazon FSx for Windows File Server
                                        (Local Windows Cache)

  App Servers ──► iSCSI Block ──────► [ Volume Gateway: Stored ] ────► Scheduled Asynchronous EBS Snapshots
                                        (All data stored locally)

  App Servers ──► iSCSI Block ──────► [ Volume Gateway: Cached ] ────► Primary Data in Amazon S3
                                        (Frequently accessed cached)

  Backup App ───► iSCSI VTL ────────► [ Tape Gateway (VTL) ] ────────► S3 (Virtual Tapes) ──► Glacier Deep Archive
                                        (Replaces tape drives)
```

### 15.2 Detailed Gateway Selection Criteria
| Gateway Type | Supported Client Protocols | Local Storage Behavior | Primary Storage Location | Production Fit |
|---|---|---|---|---|
| **Amazon S3 File Gateway** | NFS v3, NFS v4.1, SMB 2.x, SMB 3.x | Frequently accessed files cached on local disk. | **Amazon S3 directly** (objects mapped 1:1 with POSIX permissions & S3 lifecycle rules). | File backup, cost-effective S3 data lakes, hybrid on-prem data ingestion. |
| **Amazon FSx File Gateway** | SMB | Local low-latency cache for Windows SMB shares. | **Amazon FSx for Windows File Server**. | Multi-site office file shares, group directory drives, Active Directory integration. |
| **Volume Gateway: Stored** | iSCSI Block Storage | **100% of data stored locally on-premise** (1 TB – 16 TB). | Local on-premises SAN/NAS; async incremental **EBS snapshots** in S3. | Ultra-low latency on-prem applications needing cloud-backed disaster recovery. |
| **Volume Gateway: Cached** | iSCSI Block Storage | Only hot working data cached locally (1 GB – 32 TB). | **Amazon S3** is the primary source of truth. | Expanding on-prem storage capacity without buying SAN hardware. |
| **Tape Gateway (VTL)** | iSCSI Virtual Tape Library (VTL) | Local cache holding virtual tapes during backup jobs. | Virtual Tapes stored in **Amazon S3**; archived to **Glacier Flexible** & **Glacier Deep Archive**. | Replacing physical tape robots, offsite tape vaulting, and tape cartridges. |
