# SolsBrain — Chapter 10: Infrastructure as Code & Deployment at Scale

**Domain:** Declarative Infrastructure, Multi-Account Governance, CI/CD Strategies & Fleet Management  
**Target Clouds:** AWS, Microsoft Azure, Google Cloud Platform (GCP)  
**Target Engine:** SolsArch Analyzer (Ops Evaluator `ops.py`, Agent 6 Cloud Decider, Core Validator)  
**Version:** 2.0 (Exhaustive Technical Specification)

---

## 1. Declarative Infrastructure as Code (IaC)

### 1.1 AWS CloudFormation & StackSets
CloudFormation provides deterministic resource provisioning through declarative JSON/YAML templates.

#### Key Mechanics:
- **Change Sets**: Previews the exact resources that will be created, modified, or deleted before executing a stack update (prevents unexpected database deletions).
- **StackSets**: Deploys CloudFormation stacks across **multiple AWS accounts and multiple AWS regions** in parallel using a single master template.
- **Drift Detection**: Detects manual configuration changes ("drift") made directly via the AWS Console or CLI outside CloudFormation management.
- **DeletionPolicy**:
  - `Retain`: Preserves the underlying resource (e.g., S3 bucket or RDS database) if the CloudFormation stack is deleted.
  - `Snapshot`: Automatically creates a final backup snapshot before deleting the resource (EBS, RDS, ElastiCache).
  - `Delete` (Default): Destroys the resource on stack termination.
- **Nested Stacks**: Decomposes monolithic templates into modular sub-stacks (e.g., Network Stack -> Database Stack -> Application Stack) using the `AWS::CloudFormation::Stack` resource.

---

### 1.2 AWS Cloud Development Kit (CDK)
The AWS CDK allows software engineers to define cloud infrastructure using familiar programming languages (TypeScript, Python, Java, Go, C#) which synthesize directly into CloudFormation JSON/YAML templates (`cdk synth`).

#### Construct Levels:
1. **L1 Constructs (CFN Primitives)**:
   - Direct 1:1 mapping to raw CloudFormation resources (e.g., `CfnBucket`).
   - Requires explicit configuration of every single property; zero defaults.
2. **L2 Constructs (AWS Curated)**:
   - Encodes AWS best practices with intelligent defaults (e.g., `s3.Bucket`).
   - Automatically provisions KMS encryption, blocks public access by default, and simplifies IAM grant methods (`bucket.grantRead(role)`).
3. **L3 Constructs (Architectural Patterns)**:
   - Multi-service solution templates (e.g., `ApplicationLoadBalancedFargateService`).
   - Provisions an entire production architecture (VPC, ALB, ECS cluster, Fargate task definition, Route 53 record, ACM certificate) in under 15 lines of code.

---

### 1.3 Terraform & OpenTofu Enterprise State Architecture
When managing multi-cloud or hybrid environments with HashiCorp Terraform / OpenTofu:

```
[ Developer / CI/CD Runner ]
             │
             ├── 1. Acquire Distributed Mutex Lock (DynamoDB Table: LockID)
             │
             ├── 2. Download Current Remote State (S3 Bucket: terraform.tfstate)
             │
             ├── 3. Execute 'terraform apply' (Reconcile State against Cloud APIs)
             │
             ├── 4. Upload Updated State with Versioning (S3 Bucket)
             │
             └── 5. Release Mutex Lock (DynamoDB Table)
```

#### Production Backend Configuration:
```hcl
terraform {
  required_version = ">= 1.5.0"
  backend "s3" {
    bucket         = "corp-terraform-state-prod-eu-west-1"
    key            = "networking/vpc.tfstate"
    region         = "eu-west-1"
    encrypt        = true
    dynamodb_table = "terraform-state-lock"
  }
}
```

---

## 2. Advanced Deployment Strategies & Traffic Shifting

| Deployment Strategy | Downtime | Rollback Latency | Cost Overhead | Failure Blast Radius | Ideal Workloads |
|---|:---:|:---:|:---:|:---:|---|
| **All-at-Once (In-Place)** | Short | Slow (Redeploy previous build) | **Zero (1.0x)** | High (100% of users affected on bad deploy) | Dev/Test environments, internal tooling |
| **Rolling Deployment** | Zero | Moderate (Roll backwards batch-by-batch) | **Zero (1.0x)** | Moderate (Only users hitting the updated batch) | Standard microservices, stateless web servers |
| **Blue/Green Deployment** | **Zero** | **Near Instant** (DNS / ALB target group switch) | **High (2.0x)** | Near Zero (Old environment remains warm on standby) | Mission-critical financial apps, payment gateways |
| **Canary Deployment** | **Zero** | **Fast** (Shift traffic back to baseline) | **Low (1.1x)** | **Minimal** (e.g., 5% traffic for 10 min before full rollout) | High-scale user APIs, algorithmic search tiers |

#### AWS CodeDeploy Deployment Configurations:
- `CodeDeployDefault.LambdaCanary10Percent5Minutes`: Shifts 10% of traffic to the new Lambda version; waits 5 minutes. If CloudWatch alarms remain green, shifts the remaining 90%.
- `CodeDeployDefault.LambdaLinear10PercentEvery1Minute`: Increments traffic to the new version by 10% every minute over a 10-minute duration.

---

## 3. Fleet Management & Security: AWS Systems Manager (SSM)

### 3.1 SSM Session Manager (Eliminating Bastion Hosts)
- **Traditional Anti-Pattern**: Deploying a public EC2 "Bastion" or "Jump" host with open inbound SSH port 22 and managed SSH keys.
- **SSM Session Manager Standard**:
  - Requires zero open inbound firewall ports (`0.0.0.0/0 -> 22` is blocked).
  - Outbound communication is handled via the SSM Agent over TLS to AWS Systems Manager endpoints.
  - Authentication and authorization are governed entirely by **AWS IAM policies**.
  - All interactive terminal keystrokes and session logs are encrypted and streamed directly to **CloudWatch Logs** or an **S3 bucket** with digital signature verification.

### 3.2 Automated Fleet Maintenance
- **SSM Patch Manager**: Automatically scans and deploys OS patches, security hotfixes, and compliance baselines across mixed Windows and Linux fleets.
- **SSM Automation**: Pre-defined or custom Python/PowerShell runbooks to remediate drifted instances, restart hung containers, or isolate compromised VMs.

---

## 4. Formal SolsArch IaC & Deployment Validation Rules

| Rule Code | Severity | Trigger Condition | Remediation Guidance |
|---|:---:|---|---|
| `IAC-CFN-001` | **CRITICAL** | Production database or storage resource in CloudFormation template omits `DeletionPolicy: Retain` | Add `DeletionPolicy: Retain` to prevent catastrophic data loss during accidental stack teardown |
| `IAC-TFM-001` | **HIGH** | Terraform configuration uses local backend or remote backend without state locking | Configure remote S3 backend with AES-256 encryption and DynamoDB state locking table |
| `OPS-DPL-001` | **HIGH** | Production user-facing microservice specifies in-place deployment with downtime | Migrate to Canary or Blue/Green deployment using AWS CodeDeploy or ALB weighted target groups |
| `OPS-SSM-001` | **CRITICAL** | EC2 instance security group allows inbound SSH port 22 from `0.0.0.0/0` | Remove inbound port 22 rule; attach `AmazonSSMManagedInstanceCore` role and use SSM Session Manager |
---

## 4. Production Deployment & Release Engineering Best Cases (from SolsRef)

### Best Case 10.1: Zero-Downtime Blue/Green Deployments via Route 53 Weighted DNS & ALB Target Group Swapping
- **Context & Problem**: Updating monolithic or microservice applications in production risks breaking user sessions or exposing bugs to 100% of user traffic simultaneously.
- **Two Canonical Blue/Green Patterns**:
  1. **ALB Target Group Weight Shift**:
     - Deploy Green version instances in parallel with Blue version.
     - Configure ALB listener rule to shift traffic weighted: 10% Green / 90% Blue -> 50%/50% -> 100% Green.
     - Keep Blue instances running in standby for 1 hour. If error metrics rise, instantly revert weight to 100% Blue in 1 second.
  2. **Route 53 Weighted DNS Routing**:
     - Blue environment has dedicated ALB; Green environment has separate ALB.
     - Route 53 Weighted Records route traffic between the two ALBs.
     - Set DNS TTL to 0 or 60 seconds to ensure client resolvers reflect routing shifts promptly.

### Best Case 10.2: Progressive Canary Deployment Strategy with AWS CodeDeploy & CloudWatch Alarms
- **Architecture**:
  - AWS CodeDeploy manages automated traffic shifting for AWS Lambda and Amazon ECS services.
  - Deployment configuration: `CodeDeployDefault.LambdaCanary10Percent15Minutes` or `Linear10PercentEvery10Minutes`.
  - CodeDeploy shifts 10% of traffic to the newly deployed revision, pauses for 15 minutes, and continuously monitors CloudWatch Alarms (`HTTP 5xx Error Rate > 0.05%`, `Lambda Latency P99 > 500ms`).
  - If any alarm triggers: CodeDeploy automatically aborts deployment and rolls back 100% of traffic to the previous version instantly.

### Best Case 10.3: Centralized Multi-Account CI/CD Pipeline Architecture
```
[ Tools / DevOps AWS Account (111111111111) ]
  [ AWS CodePipeline / GitHub Actions Runners ]
    ├── Source: GitHub Enterprise / CodeCommit
    ├── Build & Test: AWS CodeBuild (runs unit tests, lints, security SAST)
    └── Deploy Step:
          │
          ├── Assumes Role 'RoleDeployToDev' ─────► [ Dev Account (222222222222) ]
          ├── Assumes Role 'RoleDeployToStage' ───► [ Staging Account (333333333333) ]
          └── Manual Approval + Multi-Region ─────► [ Prod Account (444444444444) ]
```
- **Security Rule**: The pipeline assumes cross-account IAM roles with strictly scoped policies (`Action: ["cloudformation:*", "ecs:UpdateService", "s3:PutObject"]`), ensuring credentials never leak into build runner disks.
---

## 6. Enterprise Governance, Multi-Account Landing Zones & Fleet Operations (from SolsRef)

### 6.1 AWS Control Tower & Multi-Account Landing Zone Architecture
```
[ AWS Organizations Core Organization Hierarchy ]
                       │
      ┌────────────────┴────────────────┐
      ▼                                 ▼
[ Core / Security OU ]             [ Workloads OU ]
├── Log Archive Account            ├── Production Account
│   └── Centralized S3 Logging     ├── Staging Account
│       (SSE-KMS, MFA Delete)      └── Development Account
└── Audit Account
    └── Security Hub, GuardDuty,
        AWS Config Aggregator
```
- **Guardrails Framework**:
  - **Preventive Guardrails**: Enforced via Service Control Policies (SCPs) at the Organization / OU level (e.g. `DenyLeaveOrganization`, `DenyDisablingCloudTrail`, `DenyUnapprovedRegions`).
  - **Detective Guardrails**: Enforced via AWS Config Rules and Conformance Packs (e.g. `DetectUnencryptedEBSVolumes`, `DetectPublicS3Buckets`).

### 6.2 AWS Systems Manager (SSM) Fleet Management & Session Manager
- **Elimination of SSH Bastion Hosts**:
  - Traditional architecture required bastion jump-boxes in public subnets with open inbound port 22.
  - **SSM Session Manager**: EC2 instances run the SSM Agent in private subnets with no inbound security group rules open. Connections are tunneled securely outbound via AWS Systems Manager endpoints.
  - Enforces IAM authentication, Multi-Factor Authentication (MFA), and logs 100% of shell keystrokes and session recordings directly to encrypted S3 and CloudWatch Logs.

### 6.3 The Triad of Operational Governance: AWS Config vs CloudTrail vs CloudWatch
| Operational Dimension | AWS Config | AWS CloudTrail | Amazon CloudWatch |
|---|---|---|---|
| **Core Question Answered** | **"How is my infrastructure configured right now, and how did it change over time?"** | **"Who made this API call, when, from which IP, and using what identity?"** | **"How is my application performing right now (metrics, CPU, RAM, latencies, logs, alarms)?"** |
| **Data Emitted** | Resource configuration items, relationship graphs, compliance snapshots. | JSON API call audit events (Management Events, Data Events, Insights Events). | Metrics (time series data), Log streams, composite alarms, synthetic canaries. |
| **Primary Consumer** | Compliance officers, cloud governance engineers, security auditors. | Security Incident Response Teams, forensic investigators, SOC analysts. | SREs, DevOps engineers, auto-scaling engines, alerting systems. |
---

## 7. AWS CodeSuite CI/CD Architecture & AppSpec Hooks (from SolsRef)

### 7.1 AWS CodeDeploy In-Place Deployment Lifecycle Hooks
When deploying code to EC2 instances or on-premises servers, CodeDeploy executes lifecycle hook scripts in strict sequential order:
```
1. ApplicationStop     ──► Gracefully stop existing web server / application
2. DownloadBundle      ──► CodeDeploy agent downloads new revision bundle from S3
3. BeforeInstall       ──► Pre-installation tasks (decrypt config files, backup databases)
4. Install             ──► Copies files from revision to destination folder
5. AfterInstall        ──► Post-installation tasks (configure permissions, run migrations)
6. ApplicationStart    ──► Starts web server / service daemon (systemctl start myapp)
7. ValidateService     ──► CRITICAL HOOK: Runs curl / health check tests against local port
```
- *Failure Behavior*: If the script in `ValidateService` exits with a non-zero status code, CodeDeploy immediately halts the deployment and initiates an automated rollback to the previous version.

---

## 8. CloudFormation & CDK Engineering: StackSets & Drift Detection (from SolsRef)

### 8.1 AWS CloudFormation StackSets Architecture
- **Multi-Account, Multi-Region Governance**:
  - Deploy CloudFormation stacks across **hundreds of AWS accounts and multiple AWS regions** from a central administrator account with a single template operation.
  - Automatically provisions baseline security infrastructure (IAM roles, VPC Flow Logs, GuardDuty detectors, Config rules) into newly created AWS accounts in an AWS Organization.
  - Configurable deployment parameters:
    - **Max Concurrent Accounts**: Limits the number of accounts deployed to simultaneously.
    - **Failure Tolerance**: Maximum number of account deployments that can fail before the entire StackSet operation is aborted.

### 8.2 Drift Detection & Resource Deletion Policies
- **Drift Detection**: Identifies resources that have been modified outside of CloudFormation management (e.g. an engineer manually modifying a Security Group rule in the AWS Console). Emits a detailed diff showing property-level drift.
- **DeletionPolicy Attribute**:
  - `Delete`: Default behavior; resource is deleted when stack is deleted.
  - `Retain`: Preserves the resource (and its data) when the stack is deleted. Mandatory for production S3 buckets, RDS databases, and KMS keys.
  - `Snapshot`: Creates an automated snapshot before deleting the resource (supported for Amazon RDS, Amazon Aurora, Amazon DocumentDB, Amazon Neptune, and EBS volumes).
---

## 9. AWS Service Catalog & AWS Proton Platform Engineering (from SolsRef)

### 9.1 AWS Service Catalog Architecture
- **Problem**: Developers spinning up unvetted CloudFormation/Terraform templates launch non-compliant, insecure, or oversized cloud resources, causing security violations and cost overruns.
- **Architecture**:
  - Cloud Administrators curate a **Portfolio** of standardized, pre-approved **Products** (e.g. a secure 3-tier VPC with encrypted Aurora database).
  - Products are distributed across organizational AWS accounts using AWS Organizations.
  - **Launch Constraints**: Allows end-users (developers, data scientists) to provision infrastructure using an assumed IAM service role with elevated privileges, without granting developers broad administrative permissions in the target account.
  - Enforces tag governance, approved AMI IDs, and budgetary limits automatically upon provisioning.

### 9.2 AWS Proton: Automated Infrastructure for Microservices
- **Architecture**: Separates responsibilities between Platform Engineers and Application Developers:
  - **Platform Engineers** define **Environment Templates** (VPC, ECS cluster, service mesh, observability) and **Service Templates** (task definitions, load balancer routing, CI/CD pipeline).
  - **Application Developers** deploy microservices by providing application code and service inputs via the standardized Proton console or CLI, ensuring complete organizational infrastructure standardization.

---

## 10. AWS AppConfig: Dynamic Feature Flag & Runtime Configuration Architecture (from SolsRef)

- **Context & Problem**: Modifying application configuration parameters or toggling feature flags historically required executing a full CI/CD deployment or restarting application containers.
- **AppConfig Architectural Flow**:
```
[ Developer / Release Engineer ]
               │
               ▼ Updates Feature Flag: { "new_checkout_flow": true }
     [ AWS AppConfig Service ]
               │
               ├── 1. Validation Step:
               │      ├── Syntactic Validation (JSON Schema)
               │      └── Semantic Validation (AWS Lambda Function verifies integrity)
               │
               ├── 2. Progressive Deployment Strategy:
               │      ├── Deployment Profile: Linear 10% every 5 minutes (Bake time: 10 min)
               │      └── Monitors CloudWatch Alarms: ErrorRate > 0.05%
               │
               ├── 3. Automated Rollback:
               │      If CloudWatch Alarm triggers, AppConfig instantly reverts config in 1 sec.
               │
               ▼
[ Application Instances / Lambda Functions ] (Polls local AppConfig Agent at 0ms latency)
```
---

## 11. AWS CodeArtifact & CloudFormation Custom Resources (from SolsRef)

### 11.1 AWS CodeArtifact Architecture
- **Context & Problem**: Enterprise build pipelines depend on open-source package registries (npm, PyPI, Maven, NuGet, RubyGems). Outages of public package registries halt software deployments, and direct public downloads expose build fleets to supply chain dependency attacks (typosquatting, malicious package takeovers).
- **Architecture**:
  - Managed artifact repository service that securely stores, publishes, and shares software packages within an enterprise.
  - **External Connections**: Automatically pulls, caches, and verifies packages from public registries. If a public registry goes down, build runners continue pulling cached artifacts from CodeArtifact without failure.
  - Integrates with AWS KMS for encryption at rest and IAM for granular package read/publish permissions.

### 11.2 CloudFormation Custom Resources Architecture
- **Problem**: CloudFormation templates occasionally need to provision resources or execute tasks unsupported natively by AWS CloudFormation (e.g. creating a local database user, configuring a third-party SaaS webhook, or automatically emptying an S3 bucket before deletion).
- **Execution Lifecycle**:
```
[ CloudFormation Engine ]
            │
            ├── 1. Dispatches JSON Event to Custom Resource
            │      { "RequestType": "Create", "ResponseURL": "https://...", "ResourceProperties": {...} }
            │
            ▼
[ AWS Lambda Function (Custom Resource Handler) ]
   ├── Executes custom provisioning logic (e.g. empty S3 bucket)
   └── 2. Returns JSON Result to Presigned S3 ResponseURL:
          { "Status": "SUCCESS", "PhysicalResourceId": "MyCustomID", "Data": {...} }
            │
            ▼
[ CloudFormation proceeds to next template resource ]
```
---

## 12. CloudFormation Nested Stacks, Macros & Template Transformation (from SolsRef)

### 12.1 CloudFormation Nested Stacks Architecture
- **Problem**: AWS CloudFormation limits single templates to a maximum of **500 resources** and 51,200 bytes. Large enterprise systems easily exceed these limits.
- **Architecture**:
  - Uses the `AWS::CloudFormation::Stack` resource to nest child templates inside a root master template.
  - Promotes modularity and DRY (Don't Repeat Yourself) design:
    - Root Template: Coordinates deployment order.
    - Child Stack 1: Standard VPC and Subnet topology.
    - Child Stack 2: Security Groups and IAM Roles.
    - Child Stack 3: Aurora Database cluster (passes database endpoint output to Application Stack).
    - Child Stack 4: Application Load Balancer and Auto Scaling Group.

### 12.2 CloudFormation Macros & Template Transforms
- Executes an AWS Lambda function that parses and mutates CloudFormation template syntax *prior to stack execution*.
- **Use Cases**:
  - Automatically injecting corporate tags (`CostCenter`, `Owner`) across all resources in a template.
  - Enforcing mandatory encryption by default (e.g. finding all `AWS::S3::Bucket` resources and injecting SSE-KMS encryption configurations).
---

## 13. AWS CloudFormation StackSets & Custom Resources (from SolsRef)

### 13.1 CloudFormation StackSets Architecture
- Extends CloudFormation stacks across multiple AWS accounts and multiple AWS regions in a single automated operation:
  - **Administrator Account**: Orchestrates StackSet definitions and initiates operations.
  - **Target Accounts**: Member accounts where individual stack instances are created, updated, or deleted.
  - **Permissions Models**:
    - *Service-Managed*: Integrated with AWS Organizations; automatically deploys stacks to new accounts added to target Organizational Units (OUs).
    - *Self-Managed*: Requires explicit IAM trust roles between administrator and target accounts.

### 13.2 Lambda-Backed Custom Resources Architecture
- Executes an AWS Lambda function during CloudFormation stack provisioning, update, or deletion to handle non-supported AWS APIs or third-party integrations:
  - Sends a pre-signed S3 URL to Lambda containing the request type (`Create`, `Update`, `Delete`), Resource Properties, and ResponseURL.
  - The Lambda function executes the custom Python/Node.js logic and uploads a JSON response status (`SUCCESS` or `FAILED`) back to the pre-signed S3 URL to notify CloudFormation to proceed.
---

## 14. AWS Systems Manager (SSM) Operational Suite: Session Manager, Run Command & Patch Manager (from SolsRef)

### 14.1 SSM Session Manager (Zero-Trust Bastionless Shell Access)
```
  [ DevOps / Systems Engineer ]
                │
                ▼ (AWS CLI / AWS Management Console via HTTPS Port 443)
       ┌─────────────────────────────────────────────────────────┐
       │               AWS Systems Manager Service               │
       │  ├── IAM Role Authentication & Authorization            │
       │  ├── Inbound Port 22 is COMPLETELY CLOSED!              │
       │  └── No Bastion Host, No Public IP, No SSH Keys needed! │
       └─────────────────────────────────────────────────────────┘
                │
                ▼ Outbound HTTPS (TLS) connection from agent
       ┌─────────────────────────────────────────────────────────┐
       │     Target EC2 Instance in Private Subnet / On-Prem     │
       │  └── SSM Agent installed & running                      │
       └─────────────────────────────────────────────────────────┘
                │
                ├── Full Command Audit Logs ──► Amazon S3 Bucket (Encrypted)
                └── Real-Time Keystroke Logs ─► Amazon CloudWatch Logs
```

### 14.2 SSM Run Command vs Patch Manager vs State Manager
| Tool | Core Functionality | Execution Model | Production Best Practice |
|---|---|---|---|
| **SSM Run Command** | Execute administrative shell scripts or Ansible playbooks across thousands of instances simultaneously. | On-demand or Event-driven. Rate control via concurrency limits and error abort thresholds. | Emergency fleet-wide security remediation, ad-hoc diagnostic log collection. |
| **SSM Patch Manager** | Automates OS and security patching across Linux and Windows fleets based on **Patch Baselines**. | Scheduled Maintenance Windows. | Auto-approving critical CVE security patches 7 days after vendor release. |
| **SSM State Manager** | Enforces desired configuration state (e.g. ensure antivirus is installed, firewall ports closed). | Continuous scheduled evaluation (drift remediation). | Compliance enforcement; ensures systems remain in audited state. |
---

## 15. AWS CodeDeploy & CodePipeline Enterprise CI/CD Deployment Orchestration (from SolsRef)

### 15.1 CodeDeploy Lifecycle Event Hooks (`appspec.yml`)
```
[ EC2 / On-Premises Lifecycle Flow ]
  ApplicationStop ──► DownloadBundle ──► BeforeInstall ──► Install ──► AfterInstall ──► ApplicationStart ──► ValidateService
                                                                                                               ▲
                                                                  (CloudWatch Alarms evaluate endpoint health) ┘
```
```
[ Amazon ECS Lifecycle Flow ]
  BeforeInstall ──► Install ──► AfterInstall ──► AfterAllowTestTraffic ──► BeforeAllowTraffic ──► AllowTraffic ──► AfterAllowTraffic
                                                        ▲
                                (Runs synthetic tests   │
                                 on test listener port) ┘
```
```
[ AWS Lambda Lifecycle Flow ]
  BeforeAllowTraffic ──► (Traffic Shifting: Linear / Canary) ──► AllowTraffic ──► AfterAllowTraffic
         ▲
  (Executes pre-traffic test Lambda)
```

### 15.2 Lambda Traffic Shifting Configurations
- **Canary Configurations**:
  - `Canary10Percent5Minutes`: Routes 10% of traffic to the new version, waits 5 minutes; if healthy, shifts remaining 90%.
  - `Canary10Percent15Minutes`: Routes 10% of traffic, waits 15 minutes, then shifts 90%.
  - `Canary10Percent30Minutes`: Routes 10% of traffic, waits 30 minutes, then shifts 90%.
- **Linear Configurations**:
  - `Linear10PercentEvery1Minute`: Shifts 10% every 1 minute until 100% (completes in 10 minutes).
  - `Linear10PercentEvery2Minutes`: Shifts 10% every 2 minutes (completes in 20 minutes).
  - `Linear10PercentEvery10Minutes`: Shifts 10% every 10 minutes (completes in 100 minutes).

---

## 16. AWS CloudFormation Drift Detection & Stack Policy Governance (from SolsRef)

### 16.1 CloudFormation Drift Detection Mechanics
- Detects unmanaged changes made directly to resources via the AWS Console, AWS CLI, or SDK out-of-band:
  - **Drift Statuses**:
    - `IN_SYNC`: Resource properties match the CloudFormation template specification exactly.
    - `DRIFTED`: One or more resource properties have been manually changed or deleted.
    - `NOT_CHECKED`: CloudFormation has not yet evaluated drift on this resource.
  - **Property-Level Diffing**: Identifies the exact property drifted (e.g. `SecurityGroupIngress` changed from `10.0.0.0/16` to `0.0.0.0/0`).

### 16.2 CloudFormation Stack Policies (Preventing Accidental Deletion)
- JSON policy document applied to a stack that defines explicit `Allow` or `Deny` actions on specific resources during stack updates:
```json
{
  "Statement": [
    {
      "Effect": "Deny",
      "Action": ["Update:Delete", "Update:Replace"],
      "Principal": "*",
      "Resource": "LogicalResourceId/ProductionDatabaseCluster"
    },
    {
      "Effect": "Allow",
      "Action": "Update:*",
      "Principal": "*",
      "Resource": "*"
    }
  ]
}
```
- **Rule**: Prevents catastrophic production database drops during CloudFormation updates, even if a developer updates the stack template with a new database engine or subnet configuration!
