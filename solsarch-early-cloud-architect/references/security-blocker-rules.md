# Security blocker rules

The pre-deployment gate. SEC-001 to SEC-005 are the five checks. A critical or high finding is a
blocker: the design is not ready for production. Medium and low findings are "should fix". `scripts/ast_scanner.py` detects them from files;
you also check the design you are proposing, because the scanner cannot see a diagram.

Scope of the scanner: application code, manifests, Dockerfiles, docker compose, Kubernetes
manifests, Terraform, nginx `.conf` files, JSON/YAML policies and `.env` files. It does not read
CloudFormation, CDK or Pulumi output, runtime cloud configuration, or git history. Say so when
reporting "no blockers".

## SEC-001 Direct ingress to private compute

- Looks like: a CDN (CloudFront, Front Door, Cloud CDN) routes straight to containers or functions, with no load balancer between.
- Scanner signal: Terraform defines `aws_cloudfront_distribution` and `aws_ecs_*` but no `aws_lb` or `aws_alb`.
- Why it matters: compute either needs public IPs or an unauthenticated origin, which bypasses WAF rules and health-based routing.
- Fix: public ALB with an HTTPS listener and a target group, tasks in private subnets, security group on tasks accepting only the ALB. Restrict the ALB to the CDN where possible.
- Severity: high.

## SEC-002 Public database exposure

- Looks like: a database or cache reachable from the internet.
- Scanner signals: a Terraform resource with `0.0.0.0/0` or `::/0` and a database port (5432, 3306, 27017, 6379, 1433, 1521); `publicly_accessible = true` on a database resource; docker compose publishing a database port on all interfaces.
- Why it matters: database credentials become the only barrier, and scanners find open ports in minutes.
- Fix: place the database in private data subnets; security group allows only the application's security group; use private endpoints for managed services.
- Severity: critical for Terraform findings; medium for local compose (do not copy that mapping to deployed environments; bind to 127.0.0.1).

## SEC-003 Ephemeral storage data loss trap

- Looks like: SQLite or LMDB (used by some search engines) inside containers, serverless functions or multi-replica services.
- Scanner signal: SQLite or LMDB evidence in code or config. High when a Dockerfile, compose file, Kubernetes manifest or container IaC resource is also present, medium otherwise.
- Why it matters: container disks vanish on redeploy; two replicas with separate files diverge; shared network file systems corrupt SQLite locks.
- Fix: managed PostgreSQL (Aurora, Cloud SQL, Azure Database for PostgreSQL, Neon). If the file store must stay, one writer with a persistent volume and backups, and say that scaling is capped at one replica.
- Severity: high or medium as above.

## SEC-004 Reverse-proxy streaming dropout

- Looks like: the app streams tokens or server-sent events but a proxy buffers or closes the connection.
- Scanner signal: streaming code (`text/event-stream`, `StreamingResponse`, `EventSourceResponse`, `streamText(`, `stream=True`) plus an nginx config with `proxy_pass` that lacks `proxy_buffering off;` or has `proxy_read_timeout` under 300 seconds (or unset, which means 60).
- Also check by hand: load balancer idle timeout (ALB default 60 s), API gateway integration timeout, CDN origin timeout.
- Fix: `proxy_buffering off;` and `proxy_read_timeout 300s;` (or higher) on streaming locations; raise the load balancer idle timeout to match; send periodic keep-alive events.
- Confidence: confirmed when streaming code is used; inferred when the scanner only saw a streaming import. Report inferred findings as needing confirmation.
- Severity: medium (should fix, not a blocker).

## SEC-005 Hardcoded credentials and permissive IAM

- Looks like: secrets committed to the repo, or policies that allow everything.
- Scanner signals: AWS access key ids, private key blocks, GitHub, Slack and `sk-` style tokens (critical); quoted literal values assigned to `password`, `secret`, `api_key`, `token`; database URLs with a real-looking password; secret-like values in a `.env` that is not gitignored; `"Action": "*"` in policies.
- The scanner never prints the value. Neither should you.
- Fix: rotate anything exposed first (assume it is compromised), then move values to Secrets Manager, Key Vault or Secret Manager and read them via the workload's identity; replace `Action: *` with the specific actions needed and scope `Resource`.
- Severity: critical for keys and private keys, otherwise high. Matches that appear only in test files are low; confirm they are fake.

## Advisories (not blockers)

- ADV-001 Container runs as root: add a non-root `USER` before `CMD`.

## Reporting format

| ID | Severity | Evidence | Fix |
| :--- | :--- | :--- | :--- |
| SEC-003 | high | `app/db.py:4` sqlite3.connect | Aurora PostgreSQL, see ADR-001 |

State blockers first. If there are none, say what was and was not covered.
