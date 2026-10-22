# SolsBrain — Chapter 4: Enterprise VPC & Global Networking

**Domain:** Virtual Private Clouds, Subnet Hierarchies, Routing, Edge Distribution & Load Balancing  
**Target Clouds:** AWS, Azure, Google Cloud  
**Purpose:** Precise network topology rules, CIDR design, and private endpoint routing.

---

## 1. Virtual Private Cloud (VPC) Topology & Subnetting

### 1.1 The 3-Tier Enterprise Subnet Design
A resilient cloud architecture strictly isolates components into three distinct subnet tiers across at least two Availability Zones (AZ-A, AZ-B):

```
                        [ Internet ]
                             │
                  [ Internet Gateway (IGW) ]
                             │
 ┌───────────────────────────┴───────────────────────────┐
 │ PUBLIC SUBNET (AZ-A)          PUBLIC SUBNET (AZ-B)    │
 │ • Route to IGW (0.0.0.0/0)    • Route to IGW          │
 │ • Public ALB                  • Public ALB            │
 │ • NAT Gateway (AZ-A)          • NAT Gateway (AZ-B)    │
 └─────────────┬───────────────────────────┬─────────────┘
               │ (Private internal route)  │
 ┌─────────────┴───────────────────────────┴─────────────┐
 │ PRIVATE APP SUBNET (AZ-A)     PRIVATE APP SUBNET (B)  │
 │ • Route to NAT Gateway (AZ-A) • Route to NAT (AZ-B)   │
 │ • ECS Tasks / EKS Pods        • ECS Tasks / EKS Pods  │
 │ • Internal Microservices      • Internal Services     │
 └─────────────┬───────────────────────────┬─────────────┘
               │ (Isolated internal route) │
 ┌─────────────┴───────────────────────────┴─────────────┐
 │ ISOLATED DATA SUBNET (AZ-A)   ISOLATED DATA SUBNET (B)│
 │ • Zero Internet Routes        • Zero Internet Routes  │
 │ • Aurora / RDS Primary        • Aurora / RDS Standby  │
 │ • ElastiCache Redis Cluster   • Redis Replica         │
 └───────────────────────────────────────────────────────┘
```

### 1.2 NAT Gateway High-Availability Rules
- **Rule**: In production, deploy **one NAT Gateway per Availability Zone**.
- *Failure Mode*: If an entire AZ experiences a hypervisor or connectivity outage, instances in AZ-B routing through a single NAT Gateway in AZ-A lose all outbound internet connectivity.
- **Egress-Only Internet Gateway (EIGW)**:
  - Designed exclusively for IPv6 traffic (`::/0`).
  - Allows outbound IPv6 traffic while blocking all inbound connections from the internet (stateful).

---

## 2. Multi-VPC Interconnectivity & Endpoints

### 2.1 VPC Peering vs. AWS Transit Gateway
| Architectural Dimension | VPC Peering | AWS Transit Gateway (TGW) |
|---|---|---|
| **Topology** | Direct Point-to-Point (Mesh) | Centralized Hub-and-Spoke |
| **Transitive Routing** | **Unsupported**: VPC A <-> B and B <-> C does NOT allow A <-> C | **Fully Supported**: Routes across all VPCs, on-prem VPNs, and Direct Connect |
| **Scale Limit** | Complex at scale: `N*(N-1)/2` peerings required | Scales cleanly up to 5,000 VPC attachments |
| **Cost** | Free connection; pay only data transfer ($0.01/GB) | Hourly attachment fee ($0.05/hr) + data processing fee |
| **Best Used For** | 2–3 VPCs with high data throughput requirements | Enterprise multi-account architectures, shared services VPCs |

### 2.2 AWS PrivateLink: Gateway vs. Interface Endpoints
```
VPC Endpoints (Keep traffic on AWS private backbone; bypass NAT Gateway)
  │
  ├── Gateway Endpoints (FREE, Route Table Entry)
  │     ├── Amazon S3
  │     └── Amazon DynamoDB
  │
  └── Interface Endpoints (Powered by AWS PrivateLink, $0.01/hr + Data Transfer)
        ├── Allocates an Elastic Network Interface (ENI) with a private IP in your subnet
        ├── Supports: Secrets Manager, SQS, SNS, ECR, CloudWatch, API Gateway, 3rd-party SaaS
        └── Controlled via Security Groups
```

---

## 3. Global Edge Distribution & DNS

### 3.1 Amazon Route 53 Routing Policies
1. **Simple Routing**: Direct mapping of domain to single/multiple IP addresses (randomized round-robin).
2. **Weighted Routing**: Distributes traffic based on assigned percentages (ideal for canary releases, A/B testing).
3. **Latency-Based Routing**: Directs global requests to the AWS region offering the lowest round-trip latency.
4. **Failover Routing**: Active-Passive disaster recovery; automatically points traffic to DR site when primary health check fails.
5. **Geolocation Routing**: Routes traffic based on the geographic origin IP of the client (compliance, localization).
6. **Multi-Value Answer**: Returns up to 8 healthy records with health-check filtering (client-side load balancing).

### 3.2 Amazon CloudFront Caching & Origin Groups
- **Origin Access Control (OAC)**: Cryptographically signs requests from CloudFront to S3 using AWS Signature Version 4 (SigV4).
- **Origin Shield**: Centralized caching layer between CloudFront edge locations and origin server; eliminates origin fetch spikes.
- **Origin Groups (Failover)**:
  - Automatically fails over from primary origin (e.g., ALB) to secondary origin (e.g., static S3 maintenance page) on HTTP 500/502/503/504 errors.

---

## 4. Elastic Load Balancing: ALB vs. NLB

| Capability | Application Load Balancer (ALB) | Network Load Balancer (NLB) |
|---|---|---|
| **OSI Layer** | **Layer 7** (Application) | **Layer 4** (Transport) |
| **Protocols** | HTTP, HTTPS, gRPC, WebSockets | TCP, UDP, TLS |
| **Latency** | ~10–18 milliseconds | < 100 microseconds (Extreme performance) |
| **Routing Logic** | Path-based (`/api/*`), Host-based (`api.domain.com`), HTTP headers, Query strings | Static IP per AZ, Port-based forwarding |
| **Static IPs** | No (resolves to dynamic DNS CNAME) | **Yes** (assigns Elastic IPs; firewall-friendly) |
| **WAF Integration** | Native direct attachment of AWS WAF | Requires ALB target or AWS Network Firewall |

---

## 5. Network Validation Rules for SolsArch

1. **RULE-NET-01**: A database subnet must NOT contain a route entry pointing to an Internet Gateway (`0.0.0.0/0 -> igw-xxx`).
2. **RULE-NET-02**: Production compute subnets must route outbound internet traffic through a NAT Gateway located in the matching Availability Zone.
3. **RULE-NET-03**: VPC peering connections between identical or overlapping CIDR blocks (e.g., `10.0.0.0/16` to `10.0.0.0/16`) must be flagged as an invalid topology.
4. **RULE-NET-04**: S3 or DynamoDB communication from private subnets must traverse a Gateway VPC Endpoint to avoid unnecessary NAT Gateway data processing fees.
---

## 4. Production Network Topology & Routing Best Cases (from SolsRef)

### Best Case 4.1: Enterprise Hub-and-Spoke Topology with AWS Transit Gateway & Centralized Egress Inspection
- **Context & Problem**: As enterprise AWS accounts scale from 5 to 100+ VPCs, full-mesh VPC peering becomes unmanageable ($N(N-1)/2$ connections). Furthermore, deploying NAT Gateways in every VPC incurs massive hourly and egress fees.
- **Architecture Blueprint**:
```
[ Spoke VPC 1: Production ] ──────┐
                                   │
[ Spoke VPC 2: Staging ]    ──────┼──► [ AWS Transit Gateway (TGW) ]
                                   │           │
[ Spoke VPC 3: Shared Svcs ] ─────┘           ▼
                                   [ Central Inspection / Egress VPC ]
                                   ┌───────────────────────────────────────┐
                                   │ ├── AWS Network Firewall / GWLB Fleet │
                                   │ │   (IDS/IPS, Domain Allow-Listing)   │
                                   │ └── Centralized NAT Gateways (Multi-AZ)│
                                   └──────────────────┬────────────────────┘
                                                      │
                                                      ▼
                                            [ Internet Gateway (IGW) ]
```
- **Cost & Security Benefits**: Eliminates 90% of redundant NAT Gateways across member VPCs while enforcing stateful deep packet inspection on all outbound internet traffic.

### Best Case 4.2: Resilient Hybrid Connectivity: AWS Direct Connect with Site-to-Site VPN Active/Passive BGP Failover
- **Context & Problem**: Mission-critical enterprise connectivity between on-premises corporate data centers and AWS requires dedicated 1G/10G bandwidth via AWS Direct Connect (DX). If the physical fiber cuts, traffic must automatically shift to a backup IPsec VPN without dropped sessions or asymmetric routing loops.
- **Architecture Blueprint**:
```
[ On-Premises Corporate Router ]
     │
     ├── 1. Primary Path: AWS Direct Connect (DX Private VIF)
     │      ├── BGP Session: Advertises On-Premises Routes (BGP Local Preference: 200)
     │      └── Advertises Normal AS-Path to AWS
     │
     └── 2. Backup Path: IPsec Site-to-Site VPN (over Public Internet)
            ├── BGP Session: Advertises On-Premises Routes (BGP Local Preference: 100)
            └── Advertises Prepend AS-Path (e.g., AS65000 AS65000 AS65000) to AWS
```
- **Routing Rules**:
  - AWS router selects Direct Connect for outbound traffic to on-premise because the VPN path has a longer AS-Path.
  - On-premises router selects Direct Connect for outbound traffic to AWS due to higher BGP Local Preference.
  - If DX link goes down, BGP withdraws the DX route; traffic automatically shifts to the VPN tunnel in under 3 seconds.

### Best Case 4.3: Private Multi-Account SaaS Sharing with AWS PrivateLink (No CIDR Overlap Risk)
- **Context & Problem**: A shared internal platform or external vendor needs to expose API endpoints to hundreds of client VPCs. VPC Peering cannot connect VPCs that share identical or overlapping CIDR blocks (e.g. `10.0.0.0/16` on both sides).
- **Architecture Blueprint**:
```
[ Service Provider VPC (CIDR: 10.0.0.0/16) ]
  ├── Backend Microservices / Containers
  └── Network Load Balancer (NLB)
        │
        ▼
  [ AWS PrivateLink VPC Endpoint Service ]
        │ (AWS Internal Private Backbone - No Internet, No Peering)
        ▼
[ Consumer VPC (CIDR: 10.0.0.0/16 - Overlapping CIDR Supported!) ]
  └── Interface VPC Endpoint (ENI placed inside Consumer Private Subnet)
        ▲
        │ (Sub-millisecond TCP/HTTPS calls to private ENI IP 10.0.1.50)
  [ Consumer Application Instances ]
```

### Best Case 4.4: Global Traffic Optimization: Route 53 Latency / Geolocation vs CloudFront vs Global Accelerator
- **Decision Matrix**:
  - **Static Web Assets & API Edge Caching**: Use **Amazon CloudFront**. Edge caches static assets, terminates TLS at 600+ PoPs, and uses Origin Access Control (OAC) to protect S3/ALB.
  - **Dynamic Non-HTTP Protocols (Gaming, VoIP, IoT, raw TCP/UDP)**: Use **AWS Global Accelerator**. Provides 2 static Anycast IP addresses, routes packets over the AWS global fiber backbone to the closest regional endpoint, eliminating public internet routing hops and packet loss.
  - **Multi-Region Disaster Recovery & DNS Routing**: Use **Route 53**. Latency-based routing directs users to the region with lowest network roundtrip time; Failover routing with Health Checks shifts traffic within 30 seconds of an outage.
---

## 7. Route 53 Complete Routing Policies, Hybrid DNS & VPC Endpoints (from SolsRef)

### 7.1 Route 53 Routing Policies Complete Engineering Matrix
| Routing Policy | Routing Mechanism | Health Check Support | Typical Production Fit |
|---|---|:---:|---|
| **Simple Routing** | Returns a single IP or multiple random IPs in a single DNS record. | No | Single-instance web servers, non-critical endpoints without failover. |
| **Weighted Routing** | Distributes traffic across resources based on relative percentage weights (e.g. 70/30). | Yes | Blue/Green deployments, Canary releases, progressive A/B testing. |
| **Latency-Based Routing** | Directs client to the AWS region that provides the lowest network latency. | Yes | Multi-region production applications serving global users. |
| **Failover Routing** | Active-Passive configuration: routes to primary unless primary health check fails, then routes to secondary. | **Mandatory** | Disaster recovery architectures with standby infrastructure. |
| **Geolocation Routing** | Routes traffic based on the geographic location of the DNS query origin (continent, country, US state). | Yes | Content localization, GDPR compliance, regional licensing restrictions. |
| **Geoproximity Routing** | Routes traffic based on geographic distance between user and resource, with adjustable **Bias** (+/- 1 to 99). | Yes | Dynamically shifting traffic boundaries between regional data centers using Route 53 Traffic Flow. |
| **Multivalue Answer** | Responds to DNS queries with up to 8 healthy records selected at random. | Yes | Basic client-side DNS load balancing without an Application Load Balancer. |

### 7.2 Hybrid Cloud DNS Resolution: Route 53 Resolver
- **Inbound Endpoints**: Allows on-premises DNS servers to resolve internal AWS Private Hosted Zones (`corp.aws.internal`) by forwarding DNS queries over Direct Connect or VPN to dedicated ENI resolver IPs in the VPC.
- **Outbound Endpoints & Resolver Rules**: Allows AWS EC2 instances and Lambda functions to resolve on-premises enterprise domain names (`datacenter.local`) by forwarding outbound DNS queries over Direct Connect or VPN to on-premises DNS servers.

### 7.3 Gateway VPC Endpoints vs Interface VPC Endpoints (PrivateLink)
```
┌───────────────────────────────────────────────┬───────────────────────────────────────────────┐
│ Gateway VPC Endpoints                         │ Interface VPC Endpoints (AWS PrivateLink)     │
├───────────────────────────────────────────────┼───────────────────────────────────────────────┤
│ • Supports ONLY: Amazon S3 & Amazon DynamoDB  │ • Supports 100+ AWS Services & Custom SaaS    │
│ • Free of charge (Zero hourly / GB costs)     │ • Hourly fee per ENI + Data processing fee    │
│ • Configured via Route Table prefix-list entry│ • Deploys an Elastic Network Interface (ENI) │
│ • Accessible ONLY from within the VPC         │ • Accessible from On-Premises via DX / VPN    │
│ • Does NOT require DNS changes                │ • Uses Private DNS to override public DNS     │
└───────────────────────────────────────────────┴───────────────────────────────────────────────┘
```
---

## 8. Enterprise CIDR Subnetting Math & VPC Reserved IP Rules (from SolsRef)

### 8.1 VPC CIDR Blocks & The 5 Reserved IPs per Subnet
- **Allowable VPC Block Sizes**: Subnet prefix sizes must range between `/16` (65,536 IP addresses) and `/28` (16 IP addresses).
- **The 5 AWS Reserved IPs**: In every VPC subnet, the first four IP addresses and the last IP address are strictly reserved by AWS and cannot be assigned to any host or network interface:
  - `x.x.x.0`: **Network Address** (Subnet identification).
  - `x.x.x.1`: **VPC Router** (Default gateway for the subnet).
  - `x.x.x.2`: **DNS Server** (Amazon-Provided DNS / Route 53 Resolver; always mapped to the base VPC CIDR $+ 2$).
  - `x.x.x.3`: **AWS Future Use** (Reserved for internal routing features).
  - `x.x.x.255`: **Network Broadcast Address** (AWS VPC networking does not support IP broadcast; address is reserved).
- **Usable Capacity Formula**:
  $$\text{Usable Host IPs} = 2^{(32 - \text{CIDR Prefix})} - 5$$
  - A `/24` subnet has $256 - 5 = \mathbf{251}$ usable IPs.
  - A `/28` subnet has $16 - 5 = \mathbf{11}$ usable IPs.

---

## 9. Network Security Layer: Stateful Security Groups vs Stateless NACLs (from SolsRef)

| Security Dimension | Security Groups (SG) | Network Access Control Lists (NACL) |
|---|---|---|
| **Operating Layer** | **Instance / ENI Level** | **Subnet Level** |
| **State Tracking** | **Stateful**: Return traffic is automatically allowed regardless of outbound rules. | **Stateless**: Return traffic must be explicitly permitted in both inbound and outbound rules. |
| **Rule Capabilities** | **Allow rules only**; cannot explicitly deny traffic. | Supports both **Allow and Deny rules**. |
| **Rule Evaluation Order** | **All rules evaluated simultaneously** (broadest match applies). | **Strict numerical order** (Rule 100 evaluated before Rule 200; first match decides verdict). |
| **Default Rules** | Denies all inbound traffic; allows all outbound traffic. | Default NACL allows all inbound/outbound; Custom NACL denies all until rules added. |
| **Ephemeral Port Requirement** | Automatically handled by stateful connection tracking table. | **Mandatory**: Outbound rules must explicitly allow Ephemeral Ports (`1024-65535`) to allow responses back to clients! |

---

## 10. NAT Gateways vs NAT Instances vs Egress-Only Internet Gateways (from SolsRef)

| Architecture Dimension | AWS Managed NAT Gateway | Self-Managed NAT Instance | Egress-Only Internet Gateway |
|---|---|---|---|
| **IP Protocol** | **IPv4 Only** | IPv4 Only | **IPv6 Only** |
| **High Availability** | Managed Multi-AZ architecture; scales from 5 Gbps up to **45 Gbps** automatically. | Single EC2 instance; single point of failure; requires custom failover scripts. | High availability built into AWS network fabric. |
| **Maintenance** | Zero maintenance; fully managed by AWS software-defined network. | User patches OS, manages AMIs, disables `Source/Destination Check`. | Zero maintenance. |
| **Security Group** | Cannot attach a Security Group to a NAT Gateway (controlled by subnet NACL). | Managed via Security Group attached to EC2 instance ENI. | Controlled via VPC Route Tables and Security Groups. |
| **Optimal Production Fit** | Enterprise production VPCs with private subnets requiring outbound internet for patching/APIs. | Legacy architectures or cost-conscious test environments. | Modern dual-stack IPv6 VPCs requiring outbound internet for private IPv6 services. |

---

## 11. AWS Direct Connect (DX) Routing Architectures & Redundancy (from SolsRef)

### 11.1 Virtual Interface (VIF) Types Deep Dive
- **Private VIF**: Connects directly to a Virtual Private Gateway (VGW) in a single VPC or to a **Direct Connect Gateway (DXGW)** to access multiple VPCs across different AWS regions. Exposes only private subnet CIDRs.
- **Public VIF**: Provides dedicated, private fiber connectivity to **public AWS service endpoints** (Amazon S3, Amazon DynamoDB, AWS KMS, SQS) without traversing the public internet. BGP session exchanges public AWS IP prefixes.
- **Transit VIF**: Connects to an **AWS Transit Gateway (TGW)** via a Direct Connect Gateway. Enables on-premises connectivity to thousands of VPCs attached to the TGW over a single BGP routing session.

### 11.2 Link Aggregation Groups (LAG) & MACsec Encryption
- **Link Aggregation Group (LAG)**: Combines multiple physical Direct Connect connections (e.g. four 10 Gbps ports into a single 40 Gbps logical connection) using Link Aggregation Control Protocol (LACP - IEEE 802.3ad). All links in a LAG must operate at the same speed and terminate at the same AWS Direct Connect endpoint device.
- **MACsec (IEEE 802.1AE)**: Provides point-to-point hardware-based Layer 2 encryption at line rate (10 Gbps and 100 Gbps). Ensures that data traveling between on-premises edge routers and AWS colocation facilities is encrypted at the physical link layer without CPU encryption overhead.
---

## 12. Amazon API Gateway Deep Dive & CloudFront Advanced Caching (from SolsRef)

### 12.1 Amazon API Gateway: REST vs HTTP vs WebSocket APIs
| Architecture Dimension | REST APIs | HTTP APIs | WebSocket APIs |
|---|---|---|---|
| **Primary Protocol** | HTTP/1.1, RESTful endpoints | HTTP/2, RESTful endpoints | **Bidirectional WebSocket (WSS)** |
| **Latency & Cost** | Standard latency; $3.50/M requests | **Sub-10ms latency; $1.00/M requests (70% cheaper)** | Stateful connection charges + message pricing |
| **Feature Set** | API Keys, Usage Plans, Request Validation, Caching, WAF, Client SSL Certificates. | OIDC/OAuth 2.0 authorization, CORS, minimal feature set. | Real-time chat, live score updates, financial tickers. |
| **Endpoint Types** | **Edge-Optimized**, **Regional**, **Private** (VPC Endpoint). | Regional only. | Regional only. |

### 12.2 API Gateway Throttling, Usage Plans & Monetization
- **Throttling Algorithm**: Implements the **Token Bucket Algorithm**.
  - **Account-Level Rate Limit**: Default 10,000 requests per second with a burst capacity of 5,000 requests.
  - When limits are exceeded, API Gateway returns **HTTP 429 (Too Many Requests)**.
- **Usage Plans & API Keys**:
  - Enables monetizing APIs or metering SaaS tiers (Bronze, Silver, Gold).
  - Associates API Keys with a Usage Plan that defines:
    - **Throttle Limits**: Rate (req/sec) and Burst (capacity).
    - **Quota Limits**: Total requests permitted per Day, Week, or Month (e.g. 100,000 requests/month).

### 12.3 CloudFront Cache Policies vs Origin Request Policies
- **Cache Policy**: Defines which request attributes (Query Strings, Headers, Cookies) are included in the **CloudFront Cache Key**. Attributes *not* in the cache key do not influence cache hits, maximizing edge caching efficiency.
- **Origin Request Policy**: Controls which headers, cookies, and query strings are forwarded to the *backend origin* when a cache miss occurs, without affecting the cache key.
- **CloudFront Origin Shield**: A centralized caching tier deployed in a specific regional edge cache (e.g. `us-east-1`). All other regional caches fetch misses from Origin Shield, collapsing origin request volumes by up to 99% and protecting backend application servers from traffic spikes.
---

## 13. Enterprise VPN Architectures: Site-to-Site VPN vs Client VPN (from SolsRef)

### 13.1 AWS Site-to-Site VPN & BGP ECMP Architecture
- **Architecture**: Connects corporate on-premises data centers to AWS VPCs or AWS Transit Gateway.
- **High-Availability Tunnel Topology**:
  - Every AWS Site-to-Site VPN connection provisions **TWO redundant IPsec tunnels** terminating at distinct AWS endpoints across different Availability Zones.
  - Best practice requires on-premises customer gateways (CGW) to establish both tunnels with BGP dynamic routing.
- **Equal-Cost Multi-Path (ECMP) Throughput Scaling**:
  - A single IPsec tunnel has a maximum throughput of **1.25 Gbps**.
  - By terminating VPN connections on an **AWS Transit Gateway (TGW)** with ECMP enabled, up to 40 VPN tunnels can be aggregated simultaneously, scaling aggregate encrypted hybrid throughput up to **50 Gbps**.

### 13.2 AWS Client VPN (Remote Workforce Access)
- Fully managed OpenVPN-compatible client VPN service for remote employees.
- Authenticates users via Active Directory, SAML 2.0 (Okta, Azure AD), or mutual certificate authentication.
- **Split-Tunnel Architecture**: Directs only corporate AWS CIDR traffic across the encrypted VPN tunnel, while public internet traffic routes directly out the employee's local ISP, reducing AWS data transfer costs by up to 80%.

---

## 14. Route 53 DNSSEC & Resolver DNS Firewall (from SolsRef)

### 14.1 Route 53 DNSSEC (Domain Name System Security Extensions)
- Protects public and private DNS routing against DNS spoofing, man-in-the-middle attacks, and cache poisoning.
- Cryptographically signs DNS records using public-key cryptography. When a client resolver requests an IP, Route 53 returns the DNS record alongside a digital cryptographic signature (RRSIG) verified against a Chain of Trust up to the root domain.

### 14.2 Route 53 Resolver DNS Firewall
- **Architecture**: Stateful firewall that inspects and filters outbound DNS queries initiated from VPC workloads (EC2, containers, Lambda).
- **Threat Protection**:
  - Enforces domain **Allow-Lists** (e.g. workloads can resolve only `*.amazonaws.com` and `api.partner.com`).
  - Enforces domain **Block-Lists** (blocks known malware domains, command-and-control servers, and dynamic DNS providers).
  - Stops **DNS Exfiltration**: Prevents malicious actors or malware from exfiltrating sensitive data by encoding stolen credentials into DNS subdomains (`stolen_data.attacker.com`).
---

## 15. VPC Flow Logs Deep Dive & Global Traffic Architecture (from SolsRef)

### 15.1 VPC Flow Logs Schema & Security Diagnostics
- **Flow Log String Format**:
  `version account-id interface-id srcaddr dstaddr srcport dstport protocol packets bytes start end action log-status`
- **Diagnosing Security Group vs NACL Rejections**:
```
Scenario A (Inbound Security Group Block):
Flow Log: 2 123456789012 eni-01234567 203.0.113.5 10.0.1.50 49152 443 6 1 40 1600000000 1600000060 REJECT OK
Diagnostic: Inbound packet rejected; missing Security Group rule allowing port 443 from source IP.

Scenario B (Stateless NACL Return Block):
Flow Log 1: 2 123456789012 eni-01234567 203.0.113.5 10.0.1.50 49152 443 6 1 40 1600000000 1600000060 ACCEPT OK
Flow Log 2: 2 123456789012 eni-01234567 10.0.1.50 203.0.113.5 443 49152 6 1 40 1600000000 1600000060 REJECT OK
Diagnostic: Inbound accepted by SG/NACL, but outbound return response to ephemeral port 49152 blocked by stateless NACL!
```

### 15.2 Architectural Comparison: AWS Global Accelerator vs Amazon CloudFront
| Dimension | AWS Global Accelerator | Amazon CloudFront |
|---|---|---|
| **OSI Layer** | **Layer 4 (Transport: TCP / UDP)** | **Layer 7 (Application: HTTP / HTTPS / WebSockets)** |
| **Edge Caching** | **No Caching**; purely network path acceleration. | **Deep Edge Caching** for static assets, videos, API responses. |
| **IP Addresses** | **2 Static Anycast IPv4 Addresses** (Fixed endpoints for firewalls). | Dynamic DNS CNAME mapping to 600+ edge PoPs. |
| **Protocol Support** | **Any TCP / UDP protocol** (Gaming, VoIP, IoT, MQTT, DNS). | HTTP, HTTPS, WebSockets only. |
| **Failover Mechanism** | Instant automated health-check failover to healthy regional endpoint in **< 1 minute** (no DNS TTL caching delays!). | Relies on DNS TTL propagation or CloudFront Origin Groups failover. |
| **Client IP Preservation** | Preserves client IP address to backend EC2/ALB targets natively. | Forwards client IP in `X-Forwarded-For` HTTP header. |
---

## 16. AWS Transit Gateway Advanced Routing: Segmentation & Appliance Mode (from SolsRef)

### 16.1 Multi-Tenant Network Segmentation via TGW Route Tables
```
[ Production Spoke VPCs ] ──► Associated with: [ Prod TGW Route Table ]
                                                   ├── Propagates: Shared Services VPC
                                                   └── CANNOT see Dev Spoke VPCs (Isolated!)

[ Development Spoke VPCs ] ──► Associated with: [ Dev TGW Route Table ]
                                                   ├── Propagates: Shared Services VPC
                                                   └── CANNOT see Prod Spoke VPCs (Isolated!)

[ Shared Services VPC ]    ──► Associated with: [ Shared TGW Route Table ]
                                                   └── Propagates: Prod VPCs + Dev VPCs
```
- **TGW Blackhole Routes**: Explicit route entries where the target is set to `blackhole`. Any traffic matching the CIDR is silently dropped at the gateway, preventing data leakage or accidental routing to unauthorized subnets.

### 16.2 Transit Gateway Appliance Mode (Stateful Firewall Symmetry)
- **Problem**: In centralized inspection architectures, east-west traffic between VPCs routes through a central Security VPC containing stateful inspection firewalls (Palo Alto Networks, Fortinet, Check Point). Without Appliance Mode, AWS Transit Gateway hashes return traffic across different Availability Zones, sending the response packet to a different firewall instance that has no record of the TCP connection handshake, causing the firewall to drop the connection!
- **Appliance Mode Solution**:
  - When enabled on the VPC attachment (`ApplianceModeSupport: enable`), TGW guarantees that all traffic for a specific bidirectional TCP flow is routed to the **exact same Network Interface (ENI) in the same Availability Zone** for the entire lifespan of the connection, maintaining stateful firewall inspection symmetry.
---

## 17. Amazon Route 53 Resolver, CloudFront Signed Assets & PrivateLink Topology (from SolsRef)

### 17.1 Amazon Route 53 Resolver (Hybrid Cloud DNS Routing)
```
[ On-Premises Corporate DC ]                                   [ AWS Virtual Private Cloud (VPC) ]
   (10.0.0.0/16)                                                      (172.31.0.0/16)

 [ Corporate DNS Server ] ──── Forward 'aws.corp' queries ────► [ Route 53 Inbound Endpoint ]
   (10.0.4.10)                    over Direct Connect / VPN             (172.31.10.5, 172.31.20.5)
                                                                                  │ Resolves against:
                                                                                  ▼
                                                                    [ Route 53 Private Hosted Zone ]
                                                                            (aws.corp)

 [ Corporate Server ]     ◄─── Query 'corp.internal' ─────────── [ Route 53 Outbound Endpoint ]
   (Resolves on-prem)              over Direct Connect / VPN             (172.31.10.99, 172.31.20.99)
                                                                                  ▲
                                                                                  │ Resolver Rules
                                                                                  │ 'corp.internal' -> 10.0.4.10
                                                                    [ EC2 / Containers in VPC ]
```
- **Inbound Endpoints**: Elastic Network Interfaces (ENIs) with private VPC IPs that receive DNS queries from on-premises resolvers to resolve AWS Private Hosted Zones.
- **Outbound Endpoints**: ENIs that forward DNS queries originating in the VPC to on-premises DNS servers based on conditional **Route 53 Resolver Rules**.
- **Route 53 Resolver DNS Firewall**: Domain-level security filtering across VPCs; inspects outbound DNS queries against domain allowlists (e.g. `*.amazon.com`) and blocklists (known malware/C2 domains).

### 17.2 CloudFront Signed URLs vs Signed Cookies & Origin Groups
| Feature / Characteristic | CloudFront Signed URLs | CloudFront Signed Cookies |
|---|---|---|
| **Resource Granularity** | Individual file / distinct URL path. | Multiple files / entire content tree / HLS video streams. |
| **Client Requirement** | Client does not need cookie support; URL query string contains signature. | Client must support HTTP cookies (e.g. standard web browsers). |
| **Caching Behavior** | Unique URL per customer disables shared edge caching. | Clean URL structure preserves high CloudFront edge cache hit ratios! |
| **Primary Use Case** | Single digital software download, secure PDF receipt. | Subscription streaming platform, protected member portal. |

- **CloudFront Origin Groups (Automatic Failover)**:
  - Configures an origin group with a **Primary Origin** and a **Secondary Origin** (cross-region S3 buckets, or S3 primary + EC2/ALB secondary).
  - Automatically fails over to the secondary origin when the primary returns configured HTTP status codes (`500`, `502`, `503`, `504`, `403`, `404`).

### 17.3 AWS PrivateLink (VPC Endpoint Services) vs Transit Gateway vs Peering
| Connectivity Option | IP Overlap Tolerance | Network Isolation Level | Scale & Transitivity | Production Best Fit |
|---|---|---|---|---|
| **VPC Peering** | **Zero tolerance** (CIDRs must not overlap). | Direct routing between VPCs; shared network layer. | Non-transitive; scales up to tens of VPCs before mesh complexity. | High-bandwidth, low-latency, point-to-point VPC data replication. |
| **Transit Gateway (TGW)** | **Zero tolerance** within the same route table. | Hub-and-spoke routed network; Layer 3 packet routing. | Highly scalable (thousands of VPCs); transitive routing. | Enterprise multi-account hub-and-spoke corporate backbone. |
| **AWS PrivateLink** | **Full tolerance!** (Consumer and Producer can share identical `10.0.0.0/16` CIDR!). | Unidirectional Layer 4 TCP proxy via NLB and Endpoint ENI; no route table modification. | Point-to-point service publishing; massive multi-tenant scale. | Exposing SaaS applications to external AWS customer accounts securely without network exposure. |
---

## 18. Amazon API Gateway Advanced Throttling, Usage Plans & VTL Mapping Templates (from SolsRef)

### 18.1 Token Bucket Algorithm & Multi-Tier Throttling
```
                                 [ Incoming API Client Requests ]
                                                │
                                                ▼
       ┌────────────────────────────────────────────────────────────────────────┐
       │               API Gateway Multi-Tier Throttling Hierarchy              │
       │                                                                        │
       │  ├── Tier 1: Client API Key / Usage Plan (e.g. 50 rps, 1,000 burst)   │
       │  ├── Tier 2: Method / Stage Level Limit (e.g. GET /orders: 500 rps)    │
       │  └── Tier 3: Regional Account Limit (10,000 rps steady-state, 5,000)   │
       └────────────────────────────────────────────────────────────────────────┘
                                                │
                                                ├── Breached: Returns HTTP 429 Too Many Requests
                                                └── Within Quota: Passes to Backend Integration
```
- **Token Bucket Algorithm**:
  - The token bucket holds up to the **Burst limit** tokens.
  - Tokens are continuously added to the bucket at the **Steady-State Rate** (requests per second).
  - Each request consumes 1 token. If the bucket is empty, requests receive HTTP 429 responses until tokens regenerate.

### 18.2 Velocity Template Language (VTL) Mapping Templates
- **Data Transformation without Lambda**:
  - Transforms incoming JSON requests into XML/SOAP for legacy backend enterprise systems.
  - Filters out sensitive parameters or strips unwanted fields before invoking compute services.
  - Injects caller identity context: `$context.identity.sourceIp`, `$context.authorizer.claims.email`.
```velocity
#set($inputRoot = $input.path('$'))
{
    "customerId": "$inputRoot.id",
    "callerIp": "$context.identity.sourceIp",
    "requestTimestamp": "$context.requestTimeEpoch",
    "sanitizedPayload": {
        "action": "$inputRoot.action",
        "parameters": "$inputRoot.details"
    }
}
```
