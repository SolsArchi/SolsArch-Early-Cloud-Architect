<p align="center"><img src="assets/branding/solsarch-logo-wordmark.png" alt="SolsArch" width="220"></p>

# SolsArch Early Cloud Architect

An Agent Skill that turns a code repository into a cloud architecture. Ask your AI agent to design the cloud setup for a project and it will:

1. ask you two questions: **which cloud** (AWS, Azure or Google Cloud) and **servers or serverless**;
2. scan the code and choose services with fixed rules, each tied to the file and line that caused it;
3. check the chosen services against the cloud vendor's own documentation by web search;
4. draw the diagrams: a picture of your cloud layout with an **icon on every service**, a service sheet, and Mermaid diagrams (C4 levels, sign-in and request sequences, code-to-cloud map);
5. run five pre-deployment security checks;
6. **verify** the report against the scan (no invented evidence ids, no leaked secrets, no broken or unexplained diagrams);
7. write a report that explains every diagram and why each service was chosen, and put everything in **one zip** with an interactive `report.html`.

This is the merged edition: Folder 1's automated pipeline and deep scanner, plus an anti-hallucination verification gate, official-icon support, an interactive HTML report and ADR tracking. It reads one repository and does not touch your cloud account. See "Limits" below.

Works with agents that read the open Agent Skills format: Claude Code, Codex, and ChatGPT agents that can run local scripts. It needs Python 3.8 or newer and nothing else.

## Install

```bash
python install.py                 # Claude Code, all projects  (~/.claude/skills)
python install.py --target project   # Claude Code, this project only (./.claude/skills)
python install.py --dest PATH     # any other agent: PATH is its skills folder
python install.py --zip dist/solsarch-early-cloud-architect.zip   # for Claude.ai upload
```

An existing install is only replaced if you add `--force`. After installing, start a new agent session.
For Claude.ai, upload the zip under Settings, Capabilities, Skills.

## Use

Open your agent in a repository and ask in plain words:

- "Design the cloud architecture for this project."
- "Draw the C4 diagrams for this repo and explain the service choices."
- "Are there deployment blockers in this repo?"
- "Continue the architecture work from last time."

The agent asks the two questions, runs the steps, and tells you where the zip is.

## Same result with any model

The decisions do not come from the model. Scripts choose the services, draw the diagrams and write the report from the scan and your two answers, so Claude, Codex and ChatGPT agents produce the same plan, the same diagrams and the same tables. Each plan carries a fingerprint you can compare. The model asks the questions, runs the commands, does the web searches and relays the result.

Web searches are controlled the same way: the script supplies the exact query, accepts only https pages on the vendor's own domain, requires a quote that names the service, and sets the date itself. A source that fails these checks is rejected. Without a web search tool, the check is skipped and the report says so; services are then labelled "Not verified".

## What you get

All outputs are in `docs/solsarch-output.zip`, and also written into your repository:

| File | Contents |
| :--- | :--- |
| `docs/solsarch-output.zip` | Everything below, plus `report.html` (interactive viewer), `data/evidence.json`, `data/placements.json` and a README |
| `docs/solsarch-report.md` | Verdict, summary, your choices, what the scan found, why each service was chosen, every diagram with an explanation, security findings, web checks, cost drivers, open questions, coverage |
| `docs/solsarch-architecture.md` | The diagrams with their explanations |
| `docs/diagrams/infrastructure-icons.svg` | Cloud layout with a service icon on every box. Opens in any browser, document or slide tool, offline |
| `docs/diagrams/service-cards.svg` | One card per chosen service, with icon, product name, role and why it is there |
| `docs/diagrams/*.mmd` | Each Mermaid diagram as source |
| `.solsarch/memory.json` | Your choices and decision records for the next session |
| `.solsarch/scan.json`, `plan.json`, `facts.json`, `evidence.json`, `placements.json` | The scan, the plan and the web checks (add `scan.json` to `.gitignore`) |

The zip also contains `diagrams/index.html`, which shows everything in a browser. The icon pictures work offline; the Mermaid diagrams load the Mermaid library from the internet once.

The verdict is computed from the findings. Critical and high findings are blockers. Every claim is labelled confirmed or inferred, and answers you did not give stay marked as assumed.

## The five checks

SEC-001 CDN straight to private compute. SEC-002 database exposed to the internet. SEC-003 SQLite or LMDB on containers. SEC-004 proxy settings that cut streaming responses. SEC-005 hardcoded credentials or wildcard IAM. Secret values are never printed.

## Commands

```bash
python solsarch-early-cloud-architect/scripts/solsarch.py questions --repo REPO
python solsarch-early-cloud-architect/scripts/solsarch.py scan      --repo REPO
python solsarch-early-cloud-architect/scripts/solsarch.py plan      --repo REPO --cloud aws --compute server
python solsarch-early-cloud-architect/scripts/solsarch.py facts queue --repo REPO     # web verification
python solsarch-early-cloud-architect/scripts/solsarch.py evidence  --repo REPO [--icon-dir ICONS]
python solsarch-early-cloud-architect/scripts/solsarch.py verify    --repo REPO
python solsarch-early-cloud-architect/scripts/solsarch.py compare   --repo REPO --compute server   # AWS vs Azure vs GCP table
python solsarch-early-cloud-architect/scripts/solsarch.py finish    --repo REPO [--icon-dir ICONS] [--force]
```

`finish` runs `evidence`, writes the report and diagrams, runs `verify`, and only then writes the zip. A failed
verification stops it unless you pass `--force`. Up to eight Mermaid diagrams are drawn (infrastructure, C4 context,
container and component, sign-in sequence, core request sequence, background-job sequence, code-to-cloud map); a
diagram is skipped when the repository has nothing to show for it, for example no queue means no job sequence.

`--icon-dir` takes a folder of official vendor icon SVGs (AWS, Azure, Google Cloud). They are not bundled because of
their licence terms. Name files after the service (`amazon-aurora-postgresql.svg`), its category (`relational-database.svg`) or the glyph (`database.svg`); unmatched services keep the built-in glyph.

## Monorepos, several repos, several clouds

Point `--repo` at a monorepo or at a folder holding several repositories; every service folder is scanned and planned
together. `compare` places the same scan on AWS, Azure and Google Cloud and writes `docs/solsarch-cloud-comparison.md`;
`plan` then commits to one cloud. A deployment that spans clouds is run as one plan per cloud.

## Branding

Every SVG diagram (infrastructure, service cards, icon picture, and Mermaid SVGs exported with `render_diagram.py`)
carries the SolsArch mark in its bottom-right corner, as do the HTML viewers and each diagram in `solsarch-architecture.md`.
The skill embeds a 7 KB version of the mark in `scripts/brand.py`; the full-size logos in `assets/branding/` are for
this repository only and are not part of the installed skill or the upload zip. Mermaid `.mmd` source files are plain
text and carry no image.

## What is inside

```
solsarch-early-cloud-architect/
  SKILL.md                         the instructions the agent follows
  references/
    service-catalog.json           fixed service choice per need, cloud and compute model (used by plan)
    placement-rules.json           condition rules with confidence and caveats (used by place_services.py)
    capacity-sizing.md             baseline sizing heuristics
    security-blocker-rules.md, c4-diagram-standards.md, sequence-flow-patterns.md, cloud-catalog-matrix.md
  scripts/
    solsarch.py                    the one command the agent uses
    ast_scanner.py                 repository evidence and findings (deep catalogue, generated/gitignored dirs skipped)
    evidence_bridge.py             numbers the evidence E001, E002, ... for the verifier and placement engine
    recommend.py                   scan + cloud + compute model -> plan
    place_services.py              rule-based placement with confidence and caveats (modular route)
    research.py, facts.py          web-check query planner and the strict source logger
    build_diagrams.py, render_svg.py, icon_diagram.py, render_diagram.py   diagrams, icon pictures, optional mmdc export
    diagram_sanitizer.py, mermaid_lint.py   repair and C4-aware lint of Mermaid
    verify_report.py               anti-hallucination gate
    build_report.py                writes the report
    brand.py                       the SolsArch mark and stamp_svg() for every diagram
    package_outputs.py             builds the zip (used by finish)
    package_report.py              verified zip for a hand-written report (modular route)
    hermes_memory.py, memory_harness.py     project memory, ADRs with --supersedes, scan diffing
    validate_skill.py              checks the folder against the packaging rules
  assets/, evals/
assets/branding/                   full-size logos (repository only, never installed)
tests/                             unit, accuracy, determinism and end-to-end tests with fixture repositories
examples/                          two real outputs (RAG on Google Cloud, a full-stack repo on AWS)
tools/                             install_skill.py, package_skill.py
install.py                         installer and zip builder
```

## Develop

```bash
python -m pytest tests          # or: python -m unittest discover -s tests (pytest-style tests need pytest)
python solsarch-early-cloud-architect/scripts/validate_skill.py solsarch-early-cloud-architect
```

## Limits

- Lite means one repository, static analysis only. It does not inspect a live cloud account, estimate prices, cover enterprise platforms such as SAP or Salesforce, map controls to compliance frameworks, or generate Terraform. The report lists cost drivers, not dollar figures.
- The scanner reads application code, manifests, Dockerfiles, compose files, Kubernetes manifests, Terraform, nginx configs, JSON and YAML policies and `.env` files. It does not read CloudFormation, CDK or Pulumi output, or git history. A clean result means nothing was found in those files, not that the system is secure.
- Diagrams show the standard pattern for the chosen services. They are derived from the libraries and files found, not from tracing calls, and say so under each diagram.
- The sanitizer checks diagram structure. It is not Mermaid's own parser. If `mmdc` is installed, `diagram_sanitizer.py --render-check` also renders a diagram, and `render_diagram.py` exports PNG/SVG.
- The service catalog was written on 2026-10-17. Service names change; the web check exists to catch that, and it reports what it finds without changing the plan.
- Region is not asked or chosen. The report lists it as an open question.
- The icons are simple glyphs for the kind of service (database, queue, load balancer and so on) on a tile in the chosen cloud's brand colour. They are not the cloud vendors' official icon packs, which have their own licence terms and are not bundled. Mermaid diagrams cannot carry icons, so the icons appear in the two SVG pictures.

## License

Dual-licensed under MIT or Apache-2.0 at your option; see `LICENSE`, `LICENSE-MIT` and `LICENSE-APACHE`.
