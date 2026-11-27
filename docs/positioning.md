# Positioning

## One sentence
SolsArch turns a codebase and a company's own rules into a deployable cloud architecture, then keeps it honest: every claim carries its evidence, and every pull request shows what it does to the architecture.

## What ships today
| Capability | Where |
| :--- | :--- |
| Architecture from your code, with the file and line behind each choice | `scan`, `plan`, `docs/solsarch-report.md` |
| Your company rules (md, pdf, docx, json, yaml) decide what is allowed | `rules ingest`, quality gate 8, OPA policy, ADRs |
| Terraform, Kubernetes, Ansible, platform files and CI on 40 clouds and platforms | `iac`, validated with the real tools where installed |
| Evidence ledger: Observed, Verified, Estimated, Assumed, Unknown, plus the verification step reached | `ledger`, `docs/solsarch-evidence.md` |
| Architecture impact on a pull request: services, timeouts, cost, with the code that caused each | `impact baseline` and `impact check`, generated gate workflow |
| Cloud ranking by priority (cost, performance, reliability, portability) under your rules, weights shown | `rank`, `docs/solsarch-cloud-ranking.md` |
| A named approval that the gate voids when the plan or rules change | `approve`, `docs/solsarch-approvals.md` |
| Cloud-neutral workload specification | `workload`, `docs/solsarch-workload.md` |
| 20-repository benchmark with a compare step for other workflows | `benchmark.py`, [benchmark.md](benchmark.md) |
| Quality score out of 100 across eight gates, capped when evidence is missing | `docs/solsarch-quality.md` |
| Works inside Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf, MCP | `AGENTS.md`, `mcp_server.py` |

## Roadmap, not shipped
Reading live cloud accounts, deploying, watching real spend, learning from outcomes across customers, and a hosted interface are not in this skill. The skill never holds credentials. Each of these would need an account or a service and is listed here only so nobody assumes it exists.

## Why not just ask a frontier model?
A model is something you ask. This is a repeatable system: the same repository and rules give the same files on every agent and every run, each choice shows its evidence, nothing counts as verified without a check or a sourced page, and it runs in CI on every pull request. Use a model to talk to it; use this to be able to prove the answer.

## Who it is for
| Audience | What they get |
| :--- | :--- |
| Developers with no cloud background | A zip with the diagrams, the code and a step-by-step guide for their account type |
| Platform and DevOps teams | A PR check that shows architecture, timeout and cost impact; policy as code from their own documents |
| Solutions architects | A cited first design in minutes, side-by-side cloud comparison, ADRs, exports to Backstage, Structurizr and draw.io |
| Security and compliance | Company rules enforced in the generated code and gate; a ledger that separates proven from assumed |

## Claims to avoid
No speed-up, deploy-success or cost-accuracy figure is claimed. Those are targets to measure with real projects. The quality score is the tool's own; an outside reviewer may differ. A compliance name adds checks and is not a certification.
