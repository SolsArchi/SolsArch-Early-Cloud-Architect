# The problems this skill exists to solve

Each problem below is something teams run into when they take code to a cloud. Next to each is what SolsArch does about it today, and where the limit is. Nothing here is a claim about speed or savings; those need measuring on real projects.

## 1. "I have code. I don't know what cloud services it needs."
**What happens:** a developer guesses (a container service, a database, maybe a cache), deploys, and finds out about the missing queue, secret store or timeout in production.
**What SolsArch does:** reads the repository (dependencies, Dockerfiles, compose files, environment variables, streaming and scheduled-work code) and chooses services by fixed rules. Every choice points to the file and line that caused it, so you can disagree with a specific line.
**Limit:** it reads files, not running traffic. Sizing starts from stated assumptions.

## 2. "Our company has cloud standards, and nobody reads them."
**What happens:** policies sit in PDFs and wikis (approved clouds, regions, tags, naming, log retention, spend limits). Designs ignore them, and review catches it late.
**What SolsArch does:** reads the documents you attach (md, pdf, docx, json, yaml), shows every rule with the sentence it came from, refuses a cloud the rules forbid, writes the rules into the generated code, and checks them in a quality gate. Sentences it could not turn into a rule are listed as "not enforced" instead of silently dropped.
**Limit:** prose rules are matched by fixed patterns, so some come back "not understood". A compliance name adds checks; it is not a certification.

## 3. "Which cloud should we use?"
**What happens:** the choice is made by habit, the vendor a founder knows, or a sales call.
**What SolsArch does:** places the same code on every provider with the same rules (`compare`), then ranks them by your priority (`rank`: balanced, cost, performance, reliability, portability). The weights are shown, clouds your rules block are excluded with the reason, and a cloud that covers under half of the app's needs is not offered. It also says when not to split an app across clouds.
**Limit:** the score orders options; it does not predict speed or the bill.

## 4. "We need Terraform, Kubernetes and a pipeline, and nobody has time to write them."
**What happens:** infrastructure code is written once, by hand, differently for each cloud, and drifts from the design.
**What SolsArch does:** generates Terraform, Kubernetes, Helm, Ansible, platform config files and a CI pipeline for 40 clouds and platforms from the same plan, so the diagram, the code and the cost estimate agree. Where terraform, Trivy or Checkov are installed, the real tools check the output.
**Limit:** it never deploys and never holds credentials.

## 5. "A small code change quietly changed our architecture and our bill."
**What happens:** someone adds a streaming endpoint or a job queue. Proxy timeouts, always-on compute, a cache and a worker are now needed, and nobody noticed until an outage or an invoice.
**What SolsArch does:** `impact check` compares the code against the agreed baseline and comments on the pull request: services added or removed, streaming or scheduled work turning on, the timeout that means, and the cost and score change, each with the file and line behind it.
**Limit:** it compares two readings of files; it does not watch live infrastructure.

## 6. "AI answers sound confident, and we can't tell what is true."
**What happens:** a model says a service has a free tier or a limit; nobody knows if the page was ever read.
**What SolsArch does:** keeps an evidence ledger. Every decision, price, limit, rule and assumption is labelled Observed, Verified, Estimated, Assumed or Unknown, with its source. A "verified" claim needs a sourced vendor page; a check that could not run earns nothing and is shown as not verified. Lines that say "verified" without proof are flagged.
**Limit:** a vendor page can change after it was read; the date is shown.

## 7. "I'm not a cloud person. I just want to get this online."
**What happens:** console screens, jargon and a free-tier trap that turns into a surprise bill.
**What SolsArch does:** asks only the few questions that change the design (cloud, server or serverless, account type) and writes a step-by-step deploy guide matched to the account type, with the free-tier limits and what happens when they run out.
**Limit:** it explains; the person still clicks the buttons and runs the deploy.

## 8. "Security review comes after the design and blocks the launch."
**What happens:** secrets in code, open databases, public buckets and missing logging are found in the last week.
**What SolsArch does:** scans for these at the start (including secrets in git history), places every finding in the report with its file and line, generates private-by-default networking, and runs an eight-gate pre-deploy check where a blocker always overrides the score. The tool itself treats the repository as hostile: names and paths are quoted, and tests prove it.
**Limit:** it is a static check, not a penetration test.

## 9. "Architecture documents are out of date the day after we write them."
**What happens:** diagrams and decision records are made once for a review and then forgotten.
**What SolsArch does:** regenerates diagrams (C4 and icon diagrams), decision records (ADRs), exports for Backstage, Structurizr and draw.io, and the report from the code, so refreshing is a command, and the baseline comparison shows exactly what moved.

## 10. "Every team and every AI tool gives a different answer."
**What happens:** the same repository gets a different design depending on who or what was asked.
**What SolsArch does:** the output is a pure function of the code, the rules and the answers. The same inputs give the same files, whatever agent runs it (Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf, or the MCP server). The agent's own web search then confirms names, limits and prices through recorded, checked commands.

## Who feels this most
| Person | Their problem | Where to start |
| :--- | :--- | :--- |
| Developer with no cloud background | Does not know what to build or click | `run`, then the deploy guide |
| Startup founder or small team | Needs a sensible first architecture and cost, cheaply | `compare`, `rank`, `costs` |
| Platform or DevOps team | Standards ignored, drift between design and code | `rules ingest`, `impact`, the generated gate |
| Solutions architect | Slow first designs, no evidence trail | the report, ADRs, the evidence ledger |
| Security and compliance | Cannot tell proven from assumed | the evidence ledger and the quality gates |

## What it deliberately does not do
It does not log in to a cloud account, deploy, watch real spend or learn from other customers' outcomes. Those need credentials or a hosted service, which this skill refuses to hold.
