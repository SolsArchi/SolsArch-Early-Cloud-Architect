# SolsBrain — Chapter 1: Security, Identity & Governance (Deep-Dive Edition)

**Domain:** Identity, Access Management, Cryptography, Secrets Governance & Edge Security  
**Target Clouds:** AWS, Microsoft Azure, Google Cloud Platform (GCP)  
**Target Engine:** SolsArch Analyzer (Agent 11.5 Lite Security, Agent 6 Cloud Decider, Core Validator)  
**Version:** 2.0 (Exhaustive Technical Specification)

---

## 1. Identity & Access Management (IAM) Deep Dive

### 1.1 Deterministic Policy Evaluation Engine
The policy evaluation engine operates as an acyclic decision tree. Understanding this resolution order is critical for SolsArch's security analyzer to detect unintended privilege leakage:

```
[ Incoming Request: Principal + Action + Resource + Context ]
                            │
                            ▼
              ┌───────────────────────────┐
              │ 1. Is there an EXPLICIT   │ ──( YES )──> [ FINAL VERDICT: DENIED ]
              │    DENY in any policy?    │
              └─────────────┬─────────────┘
                            │ ( NO )
                            ▼
              ┌───────────────────────────┐
              │ 2. Organization SCPs:     │ ──( NO )───> [ FINAL VERDICT: DENIED ]
              │    Does parent SCP allow? │
              └─────────────┬─────────────┘
                            │ ( YES )
                            ▼
              ┌───────────────────────────┐
              │ 3. Permission Boundary:   │ ──( NO )───> [ FINAL VERDICT: DENIED ]
              │    Is boundary present?   │
              │    If so, does it allow?  │
              └─────────────┬─────────────┘
                            │ ( YES / NOT APPLIED )
                            ▼
              ┌───────────────────────────┐
              │ 4. Identity or Resource   │ ──( YES )──> [ FINAL VERDICT: ALLOWED ]
              │    Policy: Explicit ALLOW?│
              └─────────────┬─────────────┘
                            │ ( NO )
                            ▼
                [ FINAL VERDICT: DENIED ] (Implicit Deny)
```

#### Key Evaluation Rules:
1. **Explicit Deny Overrides Everything**: Even an Administrator policy with `Action: "*", Resource: "*"` is nullified if a single statement contains an explicit `Effect: "Deny"` matching the request context.
2. **Cross-Account Authorization Double-Check**: For access across distinct AWS accounts (Account A to Account B):
   - Account A must **explicitly allow** the principal to delegate/assume or call the external resource.
   - Account B must **explicitly allow** the principal via a resource-based policy or trust relationship.
   - If *either* side fails, the request is denied.

---

### 1.2 IAM Policy Grammar & Condition Key Operators

An IAM policy document contains strict semantic elements:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EnforceMFAAndOrgBoundaries",
      "Effect": "Deny",
      "Action": "*",
      "Resource": "*",
      "Condition": {
        "BoolIfExists": {
          "aws:MultiFactorAuthPresent": "false"
        },
        "StringNotEquals": {
          "aws:PrincipalOrgID": "o-xy12345678"
        }
      }
    }
  ]
}
```

#### Essential Global Condition Keys Evaluated by SolsArch:
| Condition Key | Type | Architectural Security Purpose |
|---|---|---|
| `aws:PrincipalOrgID` | String | Restricts resource access strictly to member accounts within the specified AWS Organization, blocking external accounts even if misconfigured. |
| `aws:PrincipalArn` | ARN | Verifies the exact cryptographic identity of the calling role or user. |
| `aws:SourceArn` | ARN | Prevents the **Confused Deputy Problem** during service-to-service calls (e.g., verifying an SNS topic triggers Lambda exclusively on behalf of a specific S3 bucket). |
| `aws:SourceAccount` | String | Verifies the owning account ID of the resource initiating a service call. |
| `aws:RequestedRegion` | String | Restricts operations to authorized sovereign geographical regions (e.g., `["eu-west-1", "eu-central-1"]`). |
| `aws:VpcSourceIp` | IP CIDR | Enforces that calls originate strictly from within a designated VPC IP range. |
| `aws:SecureTransport` | Boolean | Enforces TLS (`"aws:SecureTransport": "true"`); used in S3/SQS bucket policies to reject unencrypted plaintext HTTP. |

#### Condition Operator Evaluation Types:
- **String Matching**: `StringEquals`, `StringNotEquals`, `StringEqualsIgnoreCase`, `StringLike` (supports `*` and `?` wildcards).
- **Numeric Matching**: `NumericEquals`, `NumericLessThan`, `NumericGreaterThanEquals`.
- **Date Matching**: `DateEquals`, `DateLessThan` (temporary expiring access).
- **Boolean Matching**: `Bool` (`true` or `false`).
- **IP Address Matching**: `IpAddress`, `NotIpAddress` (e.g., `{"IpAddress": {"aws:SourceIp": "203.0.113.0/24"}}`).
- **Null Check**: `Null: {"aws:TokenIssueTime": "true"}` (verifies presence of condition keys).
- **Existence Modifiers**: `...IfExists` (evaluates condition only if key is present in request context; bypasses check if missing).

---

### 1.3 Quotas, Hard Limits & Architectural Anti-Patterns

| Resource Entity | Hard / Soft Quota | Architectural Consequence |
|---|---|---|
| **Managed Policy Size** | **6,144 characters** (Hard) | Complex policies must be decomposed into multiple modular managed policies. |
| **Inline Role Policy Size** | **10,240 characters** (Hard) | Roles exceeding 10 KB must attach customer-managed policies instead. |
| **Managed Policies per Role** | **10 default / 20 max** (Soft) | Over-attaching policies indicates a monolithic role anti-pattern; split into discrete microservice roles. |
| **Role Session Duration** | **1 hour default / 12 hours max** | Application compute must handle token expiration and automatic credential refresh. |
| **Permission Boundaries** | Max 1 per entity | Must be applied to all developer-created roles to prevent privilege escalation. |

#### The Confused Deputy Prevention Blueprint:
When delegating access to a multi-tenant 3rd-party SaaS (e.g., Datadog, SolsArch Collector):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:root"
      },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "solsarch-tenant-unique-uuid-98765"
        },
        "ArnEquals": {
          "aws:SourceArn": "arn:aws:iam::123456789012:role/SolsArchWorkerRole"
        }
      }
    }
  ]
}
```

---

### 1.4 Attribute-Based Access Control (ABAC)
Rather than writing hundreds of individual RBAC policies per user, enterprise teams deploy **ABAC**, linking access dynamically to tags:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowAccessIfTagsMatch",
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances"
      ],
      "Resource": "arn:aws:ec2:*:*:instance/*",
      "Condition": {
        "StringEquals": {
          "aws:ResourceTag/Project": "${aws:PrincipalTag/Project}",
          "aws:ResourceTag/Environment": "${aws:PrincipalTag/Environment}"
        }
      }
    }
  ]
}
```
*Value*: Developers tagged with `Project: Billing` can automatically start/stop newly provisioned Billing instances without updating IAM policies.

---

### 1.5 Service Control Policies (SCPs) Governance
SCPs enforce non-bypassable guardrails at the AWS Organizations Root or Organizational Unit (OU) level. Even account root users cannot bypass an SCP.

#### Production Guardrail 1: Restrict Sovereign Regions
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnapprovedRegions",
      "Effect": "Deny",
      "NotAction": [
        "iam:*",
        "route53:*",
        "cloudfront:*",
        "wafv2:*",
        "support:*"
      ],
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:RequestedRegion": [
            "eu-west-1",
            "eu-central-1"
          ]
        }
      }
    }
  ]
}
```

#### Production Guardrail 2: Prevent Disabling Security Services
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ProtectSecurityServices",
      "Effect": "Deny",
      "Action": [
        "cloudtrail:DeleteTrail",
        "cloudtrail:StopLogging",
        "guardduty:DeleteDetector",
        "guardduty:DisassociateFromMasterAccount"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## 2. Cryptographic Key Management: AWS KMS & CloudHSM

### 2.1 Envelope Encryption Deep-Dive
Directly encrypting multi-gigabyte files using a central KMS API causes extreme network overhead and hits API rate limits. Cloud architectures utilize **Envelope Encryption**:

```
                       [ AWS KMS ]
                            │
               ( Request: GenerateDataKey )
                            │
            ┌───────────────┴───────────────┐
            │                               │
            ▼                               ▼
     [ Plaintext DEK ]              [ Ciphertext DEK ]
    (256-bit AES key in RAM)     (Encrypted under KMS CMK)
            │                               │
            ▼                               │
   [ Encrypt Data Locally ]                 │
            │                               │
            ▼                               ▼
    [ Encrypted Data ] ───────────> [ Storage: S3 / EBS ]
   (Plaintext DEK erased)       (Encrypted payload + Ciphertext DEK)
```

#### Decryption Flow:
1. Application reads the **Ciphertext DEK** and **Encrypted Data** from S3.
2. Application calls `kms:Decrypt` passing only the **Ciphertext DEK**.
3. KMS decrypts the DEK using the Customer Master Key (CMK) inside its hardware boundary and returns the **Plaintext DEK**.
4. Application decrypts the data in RAM and immediately zeroes out the Plaintext DEK in memory.

---

### 2.2 KMS Key Policy Architecture & Preventing Lockouts
A KMS key policy is the primary access control mechanism for a CMK. Unlike S3 buckets, an IAM policy alone cannot grant access to a KMS key unless the Key Policy explicitly delegates authority to the account root.

#### The Safe Key Policy Pattern:
```json
{
  "Version": "2012-10-17",
  "Id": "SafeKeyPolicy",
  "Statement": [
    {
      "Sid": "EnableRootAccountDelegation",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::111122223333:root"
      },
      "Action": "kms:*",
      "Resource": "*"
    },
    {
      "Sid": "AllowAppRoleToEncryptDecrypt",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::111122223333:role/PaymentsWorkerRole"
      },
      "Action": [
        "kms:GenerateDataKey*",
        "kms:Decrypt",
        "kms:DescribeKey"
      ],
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:ViaService": "s3.eu-west-1.amazonaws.com"
        }
      }
    }
  ]
}
```
*Critical Rule*: If the `root` delegation block is omitted and the specific IAM user/role in the policy is deleted, **the KMS key becomes permanently orphaned and unrecoverable**. AWS Support cannot restore an orphaned KMS key.

---

### 2.3 KMS Key Rotation & Multi-Region Keys

#### Automatic Key Rotation:
- Available for Customer Managed Symmetric Keys.
- When enabled, AWS KMS automatically rotates the backing cryptographic material every **365 days** (annual).
- **The Key ARN, Key ID, and permissions do NOT change**.
- KMS retains all historical backing keys; older ciphertexts can be decrypted seamlessly without manual re-encryption.

#### Multi-Region Keys:
- Allows primary keys in Region A (e.g., `us-east-1`) to be replicated to Region B (e.g., `eu-west-1`).
- Replicas share the **exact same Key ID and cryptographic key material**.
- Enables client applications to encrypt client data in `us-east-1` and decrypt it directly in `eu-west-1` without making cross-region latency-inducing KMS calls.

---

## 3. Secrets Management & Parameter Store Architecture

### 3.1 Secrets Manager Automated Rotation Architecture
```mermaid
sequenceDiagram
  autonumber
  participant SM as AWS Secrets Manager
  participant Lambda as Rotation Function
  participant DB as Amazon Aurora / RDS

  SM->>Lambda: 1. createSecret (Generate new password; stage as AWSPENDING)
  SM->>Lambda: 2. setSecret (Connect to DB using AWSCURRENT; ALTER USER with AWSPENDING)
  SM->>Lambda: 3. testSecret (Test new connection using AWSPENDING password)
  SM->>Lambda: 4. finishSecret (Promote AWSPENDING to AWSCURRENT; demote old to AWSPREVIOUS)
```

#### Secret Version Stages:
- `AWSCURRENT`: The active password currently in use by applications.
- `AWSPENDING`: The newly generated password undergoing rotation testing.
- `AWSPREVIOUS`: The previous password preserved for rollback safety.

---

## 4. Edge Protection, Firewalls & WAF

### 4.1 AWS WAF Architecture & Web ACL Capacity Units (WCU)
- **WCU Budget**: A single Web ACL has a baseline limit of **1,500 WCUs** (can be extended to 5,000 via quota increase).
- **Rule Processing Model**: Rules evaluate in strict numeric priority order.
  ```
  Priority 1: IP Whitelist / Blacklist (1 WCU)
       ↓
  Priority 2: AWS Managed Rules - Common Rule Set (700 WCU)
       ↓
  Priority 3: AWS Managed Rules - Known Bad Inputs (200 WCU)
       ↓
  Priority 4: AWS Managed Rules - SQLi Prevention (200 WCU)
       ↓
  Priority 5: Rate-based Rule (100 req/5min on /api/login) (2 WCU)
       ↓
  Default Action: ALLOW (or BLOCK)
  ```

---

## 5. Security Audit Logging & Compliance Monitoring

### 5.1 CloudTrail, GuardDuty & Security Hub Architecture
```
[ All AWS API Calls & Console Logins ]
                  │
                  ▼
         [ AWS CloudTrail ] ──( S3 Bucket with KMS + Object Lock )
                  │
                  ▼
         [ Amazon GuardDuty ] ──> Analyzes VPC Flow Logs, DNS queries, EKS Audit Logs
                  │              using Machine Learning anomaly detection
                  ▼
        [ AWS Security Hub ] ───> Scores against CIS Benchmarks & PCI-DSS
                  │
                  ▼
    [ Amazon EventBridge ] ────> Automated Remediation (Lambda isolation)
```

---

## 6. Formal SolsArch Security Engine Rules

SolsArch's security scanner evaluates architectures against these formal deterministic rules:

| Rule Code | Severity | Condition Trigger | Remediation Guidance |
|---|:---:|---|---|
| `SEC-IAM-001` | **CRITICAL** | IAM policy statement has `Effect: "Allow"` and `Action: "*"` on `Resource: "*"` | Scope actions to specific APIs and restrict resource ARNs |
| `SEC-IAM-002` | **HIGH** | External role trust policy lacks `sts:ExternalId` or `aws:SourceArn` | Add unique tenant ExternalId condition to prevent Confused Deputy attacks |
| `SEC-NET-001` | **CRITICAL** | Database node (`relational_db`, `document_db`) has incoming edge from `client_browser` or `0.0.0.0/0` | Place database in an isolated subnet; route traffic via container/API tier |
| `SEC-NET-002` | **HIGH** | Public load balancer does not attach an edge WAF Web ACL | Attach AWS WAF / Cloudflare WAF to inspect incoming HTTP/HTTPS traffic |
| `SEC-STR-001` | **HIGH** | `object_storage` node lacks default encryption configuration | Configure default AWS KMS key (`aws/s3` or custom CMK) on the bucket |
| `SEC-STR-002` | **CRITICAL** | S3 bucket has `BlockPublicAccess: false` and lacks OAC CloudFront restriction | Enable S3 Block Public Access; attach CloudFront Origin Access Control |
| `SEC-SEC-001` | **CRITICAL** | Plaintext credential patterns (e.g., `AKIA...`, `password=`, `BEGIN RSA`) found in committed code | Remove secret immediately; revoke key; migrate to AWS Secrets Manager |
| `SEC-SEC-002` | **MEDIUM** | CloudWatch Log Group has retention period set to `Never Expire` | Configure retention policy between 30 and 365 days with archival to S3 |
---

## 4. Production Architecture Best Cases & Scenarios (from SolsRef)

### Best Case 1.1: CloudFront Origin Cloaking & Secret Rotation with AWS WAF & Secrets Manager
- **Context & Problem**: When using CloudFront as a CDN/WAF in front of an Application Load Balancer (ALB), attackers who discover the public DNS name or IP of the ALB can bypass CloudFront caching, rate limits, and AWS Shield/WAF defenses, attacking the backend directly.
- **Architecture Blueprint**:
```
[ Client Browser ]
        │
        ▼ (HTTPS)
[ Amazon CloudFront Distribution ]
   ├── AWS WAF WebACL (Rate Limiting, IP Reputation)
   └── Custom Origin Header Injection:
       Header: "X-Origin-Verify: <secret_token>"
        │
        ▼ (HTTPS)
[ Application Load Balancer (ALB) ]
   └── ALB AWS WAF WebACL Rule:
       IF Header "X-Origin-Verify" == "<secret_token>" ──► ALLOW
       ELSE ──────────────────────────────────────────► BLOCK (403 Forbidden)
        │
        ▼
[ Auto Scaling Group (EC2 / ECS / Containers) ]
```
- **Automated Secret Rotation Mechanics**:
  1. AWS Secrets Manager stores `<secret_token>`.
  2. Scheduled rotation Lambda function triggers every 30 days:
     - Step 1: Generates new token version `V2`.
     - Step 2: Updates ALB WAF rule to allow *both* `V1` and `V2` (grace period).
     - Step 3: Updates CloudFront distribution origin headers to send `V2`.
     - Step 4: After propagation delay (5 min), updates ALB WAF rule to strictly allow only `V2`, revoking `V1`.
- **SolsArch Evaluator Rule**: `SEC-WAF-003` — Any public ALB configured as an origin for CloudFront must enforce origin validation via CloudFront OAC or rotated custom verification headers in AWS WAF.

### Best Case 1.2: Multi-Account Centralized CloudTrail & Security Ingestion Engine
- **Context & Problem**: Enterprise compliance (SOC2, ISO 27001, HIPAA) requires that audit trails cannot be tampered with or deleted by compromised root or administrator credentials in any individual AWS account.
- **Architecture Blueprint**:
```
AWS Organizations (Management Account / Delegated Security Admin)
├── Member Account A (Prod)  ──► Organizational CloudTrail (All Regions) ──┐
├── Member Account B (Dev)   ──► Organizational CloudTrail (All Regions) ──┤
└── Member Account C (Sec)   ──► Organizational CloudTrail (All Regions) ──┤
                                                                           │
                                                                           ▼
                    [ Dedicated Log Archive AWS Account ]
                    ┌───────────────────────────────────────────────────────┐
                    │ S3 Central Audit Bucket                               │
                    │ ├── SSE-KMS with Customer Managed Key (CMK)           │
                    │ ├── Bucket Policy: Deny s3:DeleteObject to ALL        │
                    │ ├── S3 Object Lock (Compliance Mode, Retention: 365d) │
                    │ ├── S3 MFA Delete Enabled                             │
                    │ └── Lifecycle Rule: Day 90 -> Glacier Flexible        │
                    └───────────────────────────────────────────────────────┘
```
- **Key Policy Rules**:
  - The KMS CMK Key Policy explicitly allows `cloudtrail.amazonaws.com` to `GenerateDataKey` and restricts decryption to the dedicated Security Incident Response Team IAM role.
  - Member accounts have zero IAM permissions to stop logging or alter the bucket configuration.

### Best Case 1.3: Delegated Cross-Account IAM Role with `sts:ExternalId` (Confused Deputy Prevention)
- **Context & Problem**: In multi-tenant SaaS architectures, the SaaS provider assumes IAM roles in customer AWS accounts to monitor or manage resources. Without an `ExternalId`, a malicious customer could trick the SaaS vendor into assuming another customer's role (The Confused Deputy Attack).
- **IAM Policy Implementation**:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::111122223333:root" },
      "Action": "sts:AssumeRole",
      "Condition": {
        "StringEquals": {
          "sts:ExternalId": "tenant-uuid-a8f9-4b21-8842-99014abef321"
        }
      }
    }
  ]
}
```
- **SolsArch Evaluator Rule**: `SEC-IAM-004` — Cross-account trust policies without `sts:ExternalId` or `aws:SourceArn` conditions must be flagged as Critical Severity.

### Best Case 1.4: Hardware Security Module (CloudHSM) SSL Offloading for FIPS 140-2 Level 3
- **Context & Problem**: Financial trading platforms, banking, and government workloads require SSL private key isolation complying with FIPS 140-2 Level 3 (where cryptographic keys can never exist in host RAM).
- **Architecture Blueprint**:
```
[ Incoming HTTPS Client ]
        │
        ▼ (Port 443)
[ NGINX / Apache Web Tier on EC2 ]
   └── OpenSSL Dynamic Engine (libcloudhsm_openssl.so)
        │ (Private ENI / VPC Peering)
        ▼
[ AWS CloudHSM Cluster (Multi-AZ) ]
   └── Cryptographic Asymmetric RSA/ECDSA Private Key Operations performed in Hardware
```
- **Benefits**: Relieves web server CPUs of expensive asymmetric math while guaranteeing that even root compromise of the EC2 instance cannot expose the private SSL keys.
---

## 8. Advanced Cryptography, Secrets Governance & Perimeter Defense (from SolsRef)

### 8.1 AWS KMS Cryptographic Architecture & Envelope Encryption
- **Key Taxonomy & Ownership**:
  - **AWS Owned Keys**: Free, internal to AWS services (e.g. DynamoDB default encryption). Cannot be viewed, audited, or tracked in CloudTrail.
  - **AWS Managed Keys**: Default service keys (e.g. `aws/s3`, `aws/ebs`, `aws/rds`). Key policies cannot be modified; annual automated rotation.
  - **Customer Managed Keys (CMK)**: Created by user ($1/month per key). Full control over key policy, grants, annual or on-demand rotation, cryptographic deletion scheduling (7 to 30 days wait period), and CloudTrail auditability.
- **Envelope Encryption Mechanics**:
```
[ Application Client ]
       │
       ├── 1. Requests Data Key: kms:GenerateDataKey(KeyId="arn:aws:kms:...")
       │      │
       │      ▼
       │   [ AWS KMS Service Engine ]
       │      ├── Encrypts Plaintext Data Key with Customer Master Key (CMK)
       │      └── Returns TWO keys:
       │          - Plaintext Data Key (e.g., 256-bit AES key)
       │          - Ciphertext (Encrypted) Data Key
       │
       ├── 2. Client encrypts payload locally using Plaintext Data Key (AES-256-GCM)
       ├── 3. Client securely overwrites & zeros Plaintext Data Key from memory (RAM)
       │
       ▼
[ Stored Data Object ]
  ├── Ciphertext Encrypted Data Payload
  └── Ciphertext Encrypted Data Key (stored in metadata header)
```
- **KMS Multi-Region Keys**: Primary key in one region replicated to secondary regions with identical key ID, key ARN, and key material. Enables client to decrypt encrypted data in secondary regions without making cross-region network calls to KMS.

### 8.2 Amazon Cognito: User Pools vs Identity Pools (Federated Identities)
| Dimension | Amazon Cognito User Pools (CUP) | Amazon Cognito Identity Pools (Federated Identities) |
|---|---|---|
| **Primary Purpose** | **Authentication ("Who you are")** | **Authorization ("What you can access")** |
| **User Directory** | Built-in directory for web/mobile users; manages signup, login, password resets. | No user directory; exchanges tokens from identity providers for AWS credentials. |
| **Authentication Sources** | Username/password, Social (Google, Apple, Meta), Enterprise (SAML 2.0, OIDC). | Cognito User Pools, OpenID Connect, SAML, Google, Apple, Guest/Unauthenticated users. |
| **Output Token** | OIDC-standard JWT tokens: **ID Token**, **Access Token**, **Refresh Token**. | Temporary, scoped **AWS IAM Credentials** (`AccessKeyId`, `SecretAccessKey`, `SessionToken`). |
| **Integration** | Used with API Gateway Lambda Authorizers, ALB, and AppSync GraphQL. | Used to grant mobile/web clients direct access to AWS resources (S3 uploads, DynamoDB reads). |

### 8.3 Enterprise Secrets Architecture: AWS Secrets Manager vs SSM Parameter Store
| Feature | AWS Secrets Manager | AWS Systems Manager (SSM) Parameter Store |
|---|---|---|
| **Cost Model** | $0.40 per secret/month + $0.05 per 10,000 API calls | **Standard**: Free (up to 10,000 parameters). **Advanced**: $0.05 per parameter/month. |
| **Automatic Rotation** | **Native Built-in Support**: Out-of-the-box Lambda templates for RDS, Aurora, DocumentDB, Redshift. | Manual: Must configure custom EventBridge rules and Lambda functions. |
| **Cross-Account Sharing** | Native resource-based policies allow direct cross-account access without role assumption. | Requires assuming an IAM role in the target account. |
| **Multi-Region Replication** | Automated cross-region secret replication with synchronized primary-replica rotation. | Manual replication via CI/CD or custom scripts. |
| **Recommended Fit** | Database credentials, third-party API keys requiring automated 30-day rotation. | Application configuration parameters, feature flags, license keys, AMI IDs. |

### 8.4 AWS Security Services Comparison Matrix
| Service | Primary Inspection Target | Underlying Detection Mechanism | Automated Remediation Trigger |
|---|---|---|---|
| **Amazon GuardDuty** | CloudTrail Management & S3 Data Events, VPC Flow Logs, DNS Logs, EKS Audit Logs. | Machine Learning anomaly detection, threat intelligence feeds (Proofpoint, CrowdStrike). | EventBridge -> Lambda / SSM Automation Runbook (isolate compromised EC2 instance, revoke IAM session). |
| **Amazon Macie** | Amazon S3 Buckets and Object payloads. | Pattern matching, regular expressions, and ML to identify PII, credit cards, credentials, financial data. | EventBridge -> S3 Bucket Policy update (revoke public access, enable encryption). |
| **Amazon Inspector** | EC2 Instances (SSM agent), Amazon ECR container images, AWS Lambda function code. | Automated vulnerability scanning against Common Vulnerabilities and Exposures (CVE) databases. | EventBridge -> Quarantine unpatched container image in ECR, trigger CI/CD build block. |
| **AWS Security Hub** | Aggregates findings from GuardDuty, Macie, Inspector, IAM Access Analyzer, Firewall Manager. | Evaluates infrastructure against security standards (CIS AWS Foundations Benchmark, PCI-DSS). | Centralized SecOps dashboard with 1-click Automated Response and Remediation actions. |

### 8.5 Perimeter DDoS Defense: AWS Shield Standard vs AWS Shield Advanced
- **AWS Shield Standard**: Automatically enabled for all AWS customers at no cost. Defends against 96% of common Layer 3 and Layer 4 infrastructure attacks (SYN/UDP floods, reflection attacks).
- **AWS Shield Advanced**: $3,000/month per organization (1-year commitment).
  - Specialized 24/7 access to the **AWS Shield Response Team (SRT)** for manual rule customization during attacks.
  - Comprehensive Layer 7 DDoS mitigation when paired with AWS WAF.
  - **DDoS Cost Protection**: Financial credit reimbursement for scaling charges (EC2, CloudFront, ALB, Route 53) incurred due to traffic spikes during verified DDoS attacks.
  - Proactive engagement: SRT contacts the enterprise directly if Route 53 health checks degrade during an active attack.
---

## 9. Enterprise Identity Federation, SAML 2.0 & Cross-Account Access (from SolsRef)

### 9.1 SAML 2.0 Enterprise Identity Federation Architecture
- **Context & Problem**: Enterprises with 5,000+ employees using Active Directory (ADFS, Azure AD / Microsoft Entra ID, Okta, PingFederate) must not create individual IAM users in AWS. Users must log in via existing corporate SSO with Single Sign-On and Multi-Factor Authentication (MFA).
- **Architectural Flow**:
```
[ User Browser ]
       │
       ├── 1. Accesses Corporate SSO Portal (e.g. https://sso.enterprise.com)
       │      │
       │      ▼
       │   [ Corporate Identity Provider (IdP) / Active Directory ]
       │      ├── Authenticates user credentials & verifies hardware MFA
       │      └── Generates cryptographically signed SAML 2.0 Assertion:
       │          - Claim: https://aws.amazon.com/SAML/Attributes/Role:
       │                   arn:aws:iam::123456789012:role/DeveloperRole,
       │                   arn:aws:iam::123456789012:saml-provider/CorporateAD
       │          - Claim: https://aws.amazon.com/SAML/Attributes/RoleSessionName: user@enterprise.com
       │
       ├── 2. Browser redirects & POSTs SAML Assertion to AWS Sign-In Endpoint
       │      │
       │      ▼
       │   [ AWS Security Token Service (STS) ]
       │      ├── Verifies signature against uploaded IdP SAML Metadata Document
       │      └── Invokes AssumeRoleWithSAML API
       │      │
       │      ▼ Returns Temporary AWS Credentials (1h - 12h session duration)
       │
       └── 3. Browser accesses AWS Management Console / CLI with temporary session
```
- **AWS IAM Identity Center (formerly AWS SSO)**: Provides multi-account centralized access management. Assigns **Permission Sets** (IAM policies mapped to job functions) to users and groups synchronized from corporate directories via SCIM (System for Cross-domain Identity Management).

---

## 10. AWS Organizations SCP Master Rulebook & Preventive Guardrails (from SolsRef)

### 10.1 Service Control Policy (SCP) Evaluation Logic & Inheritance
- **Evaluation Rules**:
  - SCPs specify the **maximum available permissions** for accounts in an Organization or Organizational Unit (OU).
  - An SCP **never grants permissions** by itself; permissions must still be granted by IAM policies within the account.
  - If a permission is NOT allowed by an SCP at ANY level in the hierarchy (Root -> OU -> Account), it is implicitly denied.
  - An explicit `Deny` in any SCP overrides all allows across the entire organization tree.
- **Whitelist vs Blacklist Governance Strategies**:
  - **Blacklist Strategy (Default)**: The AWS default `FullAWSAccess` SCP remains attached to all roots and OUs. Custom SCPs attach explicit `Deny` statements for prohibited actions (e.g. disabling CloudTrail, using unapproved regions).
  - **Whitelist Strategy (Strict Zero-Trust)**: `FullAWSAccess` is detached. Explicit `Allow` SCPs permit only approved services (e.g. EC2, S3, RDS) for specific OUs. Any unlisted service is strictly forbidden.

### 10.2 Production Enterprise SCP Policy Library
#### Rule 1: Deny Leaving Organization & Tampering with Security Controls
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyLeavingOrgAndAlteringSecurity",
      "Effect": "Deny",
      "Action": [
        "organizations:LeaveOrganization",
        "guardduty:DeleteDetector",
        "guardduty:DisassociateFromMasterAccount",
        "cloudtrail:DeleteTrail",
        "cloudtrail:StopLogging",
        "cloudtrail:UpdateTrail",
        "config:DeleteConfigRule",
        "config:StopConfigurationRecorder"
      ],
      "Resource": "*"
    }
  ]
}
```

#### Rule 2: Enforce Geographic Regional Boundaries (Data Sovereignty & Cost Defense)
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DenyUnapprovedRegions",
      "Effect": "Deny",
      "NotAction": [
        "iam:*",
        "organizations:*",
        "route53:*",
        "budgets:*",
        "waf:*",
        "cloudfront:*",
        "globalaccelerator:*",
        "support:*"
      ],
      "Resource": "*",
      "Condition": {
        "StringNotEquals": {
          "aws:RequestedRegion": ["us-east-1", "eu-west-1"]
        }
      }
    }
  ]
}
```

#### Rule 3: Enforce TLS 1.2+ and In-Transit Encryption on Amazon S3
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EnforceSSLRequestsOnly",
      "Effect": "Deny",
      "Action": "s3:*",
      "Resource": "*",
      "Condition": {
        "Bool": {
          "aws:SecureTransport": "false"
        }
      }
    }
  ]
}
```

---

## 11. Automated Security Incident Response & Remediation Pipeline (from SolsRef)

```
[ Compromised EC2 Instance / Leaked Key ]
                   │
                   ▼ (Emits suspicious telemetry: Crypto-mining, Port Scanning, Tor Exit Node)
      [ Amazon GuardDuty / Amazon Inspector ]
                   │
                   ▼ (Generates High Severity Finding: UnauthorizedAccess:EC2/TorIPCaller)
       [ Amazon EventBridge Default Bus ]
                   │
                   ▼ (Event Pattern: { "source": ["aws.guardduty"], "detail": { "severity": [7, 8.9] } })
         [ AWS Lambda Auto-Remediation Function ]
                   │
   ┌───────────────┼───────────────────────────────┐
   ▼               ▼                               ▼
[ Step 1: Isolate ] [ Step 2: Forensic Snapshot ] [ Step 3: Revoke Credentials ]
Attaches Quarantine Snapshot attached EBS         Calls sts:RevokeUserSessions
Security Group      volumes for digital           to invalidate compromised
(No In/Out traffic) evidence analysis             IAM temporary tokens
```
---

## 12. Enterprise Directory Services, Resource Access Manager (RAM) & Firewall Manager (from SolsRef)

### 12.1 AWS Directory Services Architecture & Selection Matrix
| Directory Service Type | Architectural Underlying Engine | Trust Relationships | High Availability | Recommended Production Fit |
|---|---|---|---|---|
| **AWS Managed Microsoft AD** | Actual Microsoft Windows Server Active Directory running in AWS. | Supports **Two-Way Forest Trusts** to on-premises Active Directory. | Deployed as High-Availability domain controllers across 2+ Availability Zones. | Enterprise Windows workloads, SharePoint, SQL Server Always-On, seamless AD domain joins. |
| **AD Connector** | **Directory Gateway Proxy** (does not store or cache directory data in AWS). | N/A (Proxies LDAP/Kerberos calls directly to on-premise AD over DX/VPN). | Multi-AZ proxy instances. | Authenticating AWS Console, Amazon WorkSpaces, and QuickSight directly against on-premises AD without syncing. |
| **Simple AD** | Standalone Samba 4-compatible Active Directory engine. | **No trust relationships** supported. | Multi-AZ deployment. | Lightweight Linux/Samba environments requiring basic LDAP directory service up to 5,000 users. |

### 12.2 AWS Resource Access Manager (RAM)
- **Architecture**: Allows securely sharing AWS resources across AWS accounts within an AWS Organization or with specific external accounts without duplicating infrastructure.
- **Sharable Resource Types**:
  - **VPC Subnets**: Enables multiple accounts to deploy EC2 instances into a central VPC subnet owned by a dedicated Network Account (Participants cannot view or modify resources owned by other participants).
  - **AWS Transit Gateway attachments**: Share TGW hub with spoke accounts.
  - **Route 53 Resolver Rules**: Centralized DNS forwarding rules shared with all member accounts.
  - **AWS License Manager**: Share software licenses (Microsoft, Oracle) across organizational boundaries.

### 12.3 AWS Firewall Manager
- **Centralized Security Management**: Cross-account security management tool integrated with AWS Organizations.
- Automatically rolls out, enforces, and updates security rules across all accounts:
  - **AWS WAF Rules**: Centrally deploy WebACLs to all ALBs, API Gateways, and CloudFront distributions.
  - **AWS Shield Advanced Protections**: Automatically protect all public Elastic IPs and ALBs across accounts.
  - **VPC Security Groups**: Enforce organization-wide security group baselines (e.g. audit and close port 22 on all public EC2 instances).
  - **AWS Network Firewall Policies**: Centralized stateful packet filtering.
---

## 13. Instance Metadata Service v2 (IMDSv2) & SSRF Defense Architecture (from SolsRef)

### 13.1 IMDSv1 Vulnerabilities vs IMDSv2 Defense Mechanics
- **The SSRF Threat Vector**: In modern web architectures, Server-Side Request Forgery (SSRF) vulnerabilities in web applications allow attackers to trick backend servers into issuing HTTP requests to internal IP addresses. Under **IMDSv1**, an attacker submitting `http://169.254.169.254/latest/meta-data/iam/security-credentials/` causes the web server to fetch and expose its temporary IAM role credentials in plaintext.
- **IMDSv2 Defense Protocol**:
```
[ Attacker / Client ] ──► [ Vulnerable Web Server ]
                                 │
                                 ├── 1. Attacker attempts simple GET: http://169.254.169.254/latest/meta-data/...
                                 │      │
                                 │      ▼
                                 │   [ IMDSv2 Engine (Local Hypervisor) ]
                                 │      └── BLOCKS REQUEST (HTTP 401 Unauthorized: Missing Session Token)
                                 │
                                 └── 2. Legitimate Application Token Handshake:
                                        │
                                        ▼ (Step A: Acquire Session Token via HTTP PUT)
                                     PUT http://169.254.169.254/latest/api/token
                                     Header: "X-aws-ec2-metadata-token-ttl-seconds: 21600"
                                        │
                                        ▼ (Returns Secret Session Token: "AQAEAA...")
                                        │
                                        ▼ (Step B: Query Metadata via HTTP GET with Token)
                                     GET http://169.254.169.254/latest/meta-data/iam/security-credentials/
                                     Header: "X-aws-ec2-metadata-token: AQAEAA..."
                                        │
                                        ▼
                                     [ Metadata Successfully Returned ]
```
- **Architectural Rules for SolsArch**:
  - `SEC-EC2-IMDS2`: All EC2 launch templates and running instances must strictly enforce `HttpTokens: required` and disable IMDSv1.
  - `HttpPutResponseHopLimit`: For containerized workloads (ECS/Docker), set hop limit to `1` so packets cannot escape container network namespaces to query host metadata.

---

## 14. IAM Permission Boundaries, Session Policies & Access Analyzer (from SolsRef)

### 14.1 IAM Permission Boundaries (Privilege Escalation Defense)
- **Problem**: Senior administrators want to allow developers to create IAM roles and policies for their Lambda functions and ECS tasks, but without the risk of developers granting themselves full `AdministratorAccess` (Privilege Escalation).
- **Architecture**:
  - Administrator creates an **IAM Permission Boundary** policy that defines the *maximum ceiling* of permissions an entity can possess.
  - Developer's IAM policy requires that whenever they create a role or user, they *must* attach this Permission Boundary:
    ```json
    {
      "Condition": {
        "StringEquals": {
          "iam:PermissionsBoundary": "arn:aws:iam::123456789012:policy/DeveloperBoundary"
        }
      }
    }
    ```
  - An entity can only perform actions that are allowed by **BOTH** its identity-based policy **AND** its Permission Boundary.

### 14.2 IAM Access Analyzer
- Evaluates resource-based policies (S3 buckets, KMS keys, SQS queues, IAM roles) using automated mathematical reasoning and logic proving.
- Flags any resource that allows access to external AWS accounts or public/unauthenticated internet principals, identifying unintended exposure before security audits occur.
---

## 15. AWS KMS Deep Dive: Key Policies, Grants, Asymmetric Keys & Rotation (from SolsRef)

### 15.1 KMS Key Policy vs IAM Policy Mechanics
- **The Golden Rule of KMS Access**:
  - In AWS IAM, an IAM policy alone can never grant access to a Customer Managed Key (CMK) unless the **KMS Key Policy explicitly delegates authority to the account root principal** (`Principal: {"AWS": "arn:aws:iam::123456789012:root"}`).
  - If a key policy does NOT include the root delegation statement, ONLY the principals explicitly listed in the key policy can use or administer the key, bypassing account administrators!
- **KMS Grants vs Key Policies**:
  - **Key Policy**: Declarative, permanent JSON document attached to the key defining primary administrative and usage permissions.
  - **KMS Grants**: Programmatic, temporary, and scoped permissions granted by an authorized principal using the `kms:CreateGrant` API.
  - *Internal AWS Usage*: When an EC2 instance mounts an encrypted EBS volume, the EC2 service principal calls `CreateGrant` to acquire temporary cryptographic rights to decrypt the EBS volume key, automatically retiring the grant when the instance terminates.

### 15.2 Asymmetric KMS Keys & Digital Signatures
| Key Spec | Cryptographic Algorithms | Primary Use Case |
|---|---|---|
| **SYMMETRIC_DEFAULT** | **AES-256-GCM** (Same key encrypts & decrypts) | 99% of cloud storage encryption: Amazon S3, EBS volumes, RDS databases, DynamoDB. |
| **RSA_2048 / RSA_4096** | **RSAES_OAEP_SHA_256**, **RSASSA_PSS_SHA_512** | Digital code signing, SSL certificate generation, payload encryption by third-party systems outside AWS. |
| **ECC_NIST_P256 / P384** | **ECDSA (Elliptic Curve Digital Signature Algorithm)** | High-speed, compact digital signatures for Bitcoin/Ethereum blockchain nodes, IoT microcontrollers. |

### 15.3 Key Rotation & Bring Your Own Key (BYOK)
- **Automatic Key Rotation**:
  - When enabled, AWS automatically generates new cryptographic backing material for the CMK every **1 Year** (or on-demand).
  - The Key ARN, Key ID, and Key Policy remain completely unchanged.
  - AWS KMS permanently retains all previous key versions, allowing transparent decryption of older ciphertext while all newly encrypted payloads utilize the newest key version.
- **Imported Key Material (BYOK)**:
  - Enterprise generates key material locally inside an on-premises Hardware Security Module (HSM).
  - Imports key material into an empty AWS KMS CMK wrapped with RSA-OAEP.
  - *Operational Rule*: Automatic rotation is **not supported** for imported key material; rotation must be managed manually by creating new keys and updating application aliases.
---

## 16. The 6-Stage AWS IAM Policy Evaluation Logic & Permission Boundaries (from SolsRef)

### 16.1 Deterministic IAM Policy Evaluation Logic Flow
```
                      [ Incoming API Request (Principal, Action, Resource, Context) ]
                                                    │
                                                    ▼
                                  ┌───────────────────────────────────┐
                                  │ 1. Is there an EXPLICIT DENY?    │
                                  └───────────────────────────────────┘
                                         │ YES                   │ NO
                                         ▼                       ▼
                                  [ DENY ACCESS ]   ┌───────────────────────────────────┐
                                                    │ 2. Is there an Organization SCP?  │
                                                    └───────────────────────────────────┘
                                                           │ NO ALLOW              │ ALLOW
                                                           ▼                       ▼
                                                    [ DENY ACCESS ]   ┌───────────────────────────────────┐
                                                                      │ 3. Is Resource-Based Policy OK?  │
                                                                      └───────────────────────────────────┘
                                                                             │ YES                   │ NO
                                                                             ├──► [ ALLOW ACCESS* ]   ▼
                                                                             │    (*unless boundary) ┌───────────────────────────────────┐
                                                                             │                       │ 4. Identity-Based Policy ALLOW?   │
                                                                             │                       └───────────────────────────────────┘
                                                                             │                              │ NO                    │ YES
                                                                             │                              ▼                       ▼
                                                                             │                       [ DENY ACCESS ]   ┌───────────────────────────────────┐
                                                                             │                                         │ 5. Permissions Boundary ALLOW?    │
                                                                             │                                         └───────────────────────────────────┘
                                                                             │                                                │ NO                    │ YES
                                                                             │                                                ▼                       ▼
                                                                             │                                         [ DENY ACCESS ]   ┌───────────────────────────────────┐
                                                                             │                                                           │ 6. Session Policy ALLOW?          │
                                                                             │                                                           └───────────────────────────────────┘
                                                                             │                                                                  │ NO                    │ YES
                                                                             │                                                                  ▼                       ▼
                                                                             │                                                           [ DENY ACCESS ]          [ ALLOW ACCESS ]
                                                                             └──────────────────────────────────────────────────────────────────────────────────────────┘
```
- **Evaluation Precedence**: An explicit `Deny` in any policy layer instantly terminates evaluation with access denied. If no explicit deny exists, every applicable layer must evaluate to `Allow`.

### 16.2 IAM Permission Boundaries vs Service Control Policies (SCPs)
| Architectural Control | Enforced By | Maximum Scope | Delegation Use Case |
|---|---|---|---|
| **Service Control Policy (SCP)** | AWS Organizations Management Root | Entire AWS Accounts, OUs (Root downwards). | Defines the maximum allowable permissions for all IAM users and roles across member accounts. Does not grant permissions alone. |
| **Permissions Boundary** | IAM Administrator on IAM Principals | Specific IAM User or IAM Role. | Limits the maximum permissions that an identity-based policy can grant to an IAM entity. Enables safe delegation: allows developers to create IAM roles for Lambda/EC2 while guaranteeing they cannot escalate privileges beyond the boundary. |
| **Session Policy** | Programmatic AssumeRole API caller | Temporary STS Credentials session. | Passed when assuming a role via `sts:AssumeRole` to further restrict the permissions of that specific session. |

### 16.3 AWS IAM Identity Center (Successor to AWS Single Sign-On)
- **Centralized Multi-Account Federation**: Integrates with external Identity Providers (Okta, Microsoft Entra ID, Ping, Google Workspace) via SAML 2.0 / SCIM (System for Cross-domain Identity Management) for automated user provisioning and deprovisioning.
- **Permission Sets**: Stored centrally in the Identity Center management account; defines IAM policies assigned to users or groups across target AWS accounts.

---

## 17. AWS Network Security & Automated Threat Detection Suite: Network Firewall, GuardDuty, Macie & Security Hub (from SolsRef)

### 17.1 AWS Network Firewall Architecture
- Managed Layer 3 to Layer 7 stateful inspection firewall deployed inside VPC subnets:
  - Inspects VPC-to-Internet, Internet-to-VPC, VPC-to-VPC (east-west), and Direct Connect / VPN traffic.
  - Stateful engine supports thousands of **Suricata-compatible intrusion detection/prevention (IDS/IPS) rules**.
  - Provides TLS/SSL inspection, domain name allowlists/blocklists (e.g. block `.xyz` or `badsite.com`), and application layer protocol matching (HTTP, HTTPS, SSH).
- **Deployment Models**:
  - **Distributed Model**: Network Firewall endpoints provisioned inside dedicated firewall subnets in every individual spoke VPC.
  - **Centralized Model**: Transit Gateway routes all traffic through a dedicated Central Inspection VPC containing Network Firewall endpoints.

### 17.2 Amazon GuardDuty Threat Detection Architecture
```
[ AWS Telemetry Sources (No Agent Required!) ]
├── VPC Flow Logs
├── CloudTrail Management Events & API Calls
├── CloudTrail S3 Data Events
├── Route 53 DNS Logs (detects malware beacons / DNS tunneling)
└── Optional: EKS Audit Logs, RDS Login Activity, Lambda Execution Logs
                             │
                             ▼
              ┌───────────────────────────────┐
              │      Amazon GuardDuty         │
              │  ├── Machine Learning Models  │
              │  ├── Anomaly Detection       │
              │  └── Threat Intelligence Feeds│
              └───────────────────────────────┘
                             │
                             ▼ Emits Findings (Severity: Low, Med, High)
              ┌───────────────────────────────┐
              │    Amazon EventBridge         │
              └───────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
   [ Automated Remediation ]       [ Security Alerting ]
   AWS Lambda / Systems Manager    Amazon SNS / PagerDuty / SOC
   (Isolate compromised EC2,
    revoke leaked IAM keys)
```
- **Capabilities**: Detects compromised EC2 instances, unauthorized cryptocurrency mining, compromised IAM credentials (API calls from Tor exit nodes), S3 bucket exfiltration, and EKS container escapes.

### 17.3 Amazon Macie & AWS Security Hub
- **Amazon Macie**:
  - Uses machine learning and pattern matching (regex, keywords) to discover and classify sensitive data (Personally Identifiable Information - PII, credit card numbers, AWS secret keys, medical records) stored in Amazon S3 buckets.
  - Generates continuous data security posture evaluations and alerts on unencrypted or publicly accessible buckets containing sensitive data.
- **AWS Security Hub**:
  - Central security posture management service that aggregates, normalizes, and prioritizes security findings from GuardDuty, Macie, Inspector, AWS Config, and third-party firewalls.
  - Automates continuous compliance checks against CIS AWS Foundations Benchmark, PCI DSS, and NIST SP 800-53 Rev. 5.
---

## 18. AWS WAF, AWS Shield & DDoS Resiliency Architectures (from SolsRef)

### 18.1 AWS WAF (Web Application Firewall) Layer 7 Protection
```
                              [ Incoming Internet Traffic (HTTP / HTTPS) ]
                                                   │
                                                   ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                       AWS WAF Web ACL Evaluation                       │
       │                                                                        │
       │  ├── 1. IP Set Matching (Allowlist corporate VPN / Blocklist bad IPs) │
       │  ├── 2. Geo-Blocking (Block traffic from non-service countries)        │
       │  ├── 3. AWS Managed Rule Groups:                                       │
       │  │      ├── Core Rule Set (CRS / Common Vulnerabilities)               │
       │  │      ├── SQL Database Rule Set (SQL Injection - SQLi)               │
       │  │      ├── Known Bad Inputs (Log4j, Shellshock, local file inclusion)  │
       │  │      └── Amazon IP Reputation list                                  │
       │  ├── 4. Rate-Based Rules (Throttle IPs exceeding 100 reqs / 5 mins)   │
       │  └── 5. Bot Control & Account Takeover Prevention (ATP / ACFP)         │
       └────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ├── BLOCKED: Returns HTTP 403 Forbidden / Custom JSON error
                                    └── ALLOWED: Forwards sanitized HTTP traffic
                                                   │
                                                   ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │             Protected Application Integration Targets:                 │
       │  ├── Amazon CloudFront CDN (Global Edge Defense)                       │
       │  ├── Application Load Balancer (ALB)                                   │
       │  ├── Amazon API Gateway (REST & HTTP APIs)                             │
       │  ├── AWS AppSync (GraphQL APIs)                                        │
       │  └── Amazon Cognito User Pools                                         │
       └────────────────────────────────────────────────────────────────────────┘
```
- **Rule Actions**:
  - `Allow`: Permitted through the inspection pipeline.
  - `Block`: Returns HTTP 403 with customizable response headers and JSON body.
  - `Count`: Non-blocking observation mode for evaluating new rule false-positive rates in production.
  - `CAPTCHA / Challenge`: Silently issues silent browser JavaScript challenges or interactive puzzles to verify non-automated human clients.

### 18.2 AWS Shield Standard vs AWS Shield Advanced
| Feature & Capability | AWS Shield Standard | AWS Shield Advanced |
|---|---|---|
| **Cost** | **Free** (enabled by default for all AWS customers). | **$3,000 / month** + data transfer usage fees. |
| **Protected Layers** | Layer 3 (Network) & Layer 4 (Transport). | Layer 3, Layer 4, and **Layer 7** (Application). |
| **Attack Mitigation** | Automatic SYN floods, UDP reflection attacks, ACK floods. | Complex sophisticated multi-vector volumetric & application DDoS attacks. |
| **Protected Resources** | Amazon Route 53, CloudFront edge network. | Elastic IPs, CloudFront, ALB, NLB, and Route 53. |
| **DDoS Cost Protection** | None (customer pays for Auto Scaling compute spike). | **100% Financial Reimbursement** for scaling costs incurred during a validated DDoS attack! |
| **Human Expert Support** | Standard AWS Support channels. | **24/7 Direct Access to the AWS DDoS Response Team (DRT)** to craft custom mitigation rules during an active attack. |
| **AWS WAF Bundling** | AWS WAF rules billed separately. | **Free AWS WAF usage** included on all Shield Advanced protected resources! |
---

## 19. AWS Cognito Deep Dive: User Pools (AuthN) vs Identity Pools (AuthZ) & JWT Lifecycle (from SolsRef)

### 19.1 User Pools (Authentication) vs Identity Pools (Authorization)
```
                                     [ Web / Mobile User Client ]
                                                   │
                                                   ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                   Amazon Cognito User Pool (AuthN)                     │
       │  ├── User Directory (Email, Username, Custom Attributes)               │
       │  ├── Sign-up, Sign-in, Self-Service Password Reset, MFA (SMS/TOTP)    │
       │  └── Social / SAML / OIDC IdPs (Google, Apple, Facebook, Okta, Entra)  │
       └────────────────────────────────────────────────────────────────────────┘
                                                   │
                               (Returns JWT Tokens: ID, Access, Refresh)
                                                   ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                 Amazon Cognito Identity Pool (AuthZ)                   │
       │  ├── Exchanges JWT Token / IdP Assertion for AWS Credentials           │
       │  ├── Evaluates IAM Role Mapping (e.g. AdminRole vs FreeUserRole)        │
       │  └── Supports Unauthenticated Guest Users                              │
       └────────────────────────────────────────────────────────────────────────┘
                                                   │
                               (Returns Temporary Scoped AWS STS Credentials)
                                                   ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │                    Direct Access to AWS Services                       │
       │  ├── Amazon S3 (Upload user avatar to 's3://bucket/{cognito-identity}/')│
       │  ├── Amazon DynamoDB (Query user-partitioned records)                  │
       │  └── AWS AppSync / API Gateway                                         │
       └────────────────────────────────────────────────────────────────────────┘
```

### 19.2 The Three Cognito JWT Tokens
| Token Type | Primary Purpose | Standard Expiration | Key Payload Contents |
|---|---|---|---|
| **ID Token** | Proves user identity to client application. | 1 Hour (configurable 5m – 24h) | User profile attributes: `sub`, `email`, `cognito:groups`, `custom:tier`. |
| **Access Token** | Authorizes API calls to backend endpoints (API Gateway, ALB). | 1 Hour (configurable 5m – 24h) | Scopes, client ID, `username`, user groups (does NOT contain user email/profile). |
| **Refresh Token** | Retrieves fresh ID & Access tokens without re-authenticating. | 30 Days (configurable 60m – 10 years) | Encrypted cryptographically signed long-lived token. |
