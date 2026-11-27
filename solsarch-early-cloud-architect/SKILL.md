---
name: solsarch-early-cloud-architect
description: Early cloud architecture from your codebase and your company's rules, not from a prompt. Scans a repository, reads the standards attached beside it (policies as md, pdf, docx, json or yaml), chooses services with fixed, evidence-backed rules on 40 clouds and platforms (AWS, Azure, Google Cloud, Hetzner, Render, Vercel and more), and delivers one zip: report, C4 and icon diagrams, Terraform, Kubernetes, Ansible, Dockerfiles, CI, cost estimate, quality score, and a step-by-step deploy guide for the user's account type. Use when the user wants to design, compare or review cloud architecture for a repo, deploy an app to a cloud, follow company policy or compliance (approved clouds, regions, tags, spend limits), generate Terraform, draw C4, check deployment blockers, or learn how to deploy.
compatibility: Needs Python 3.8 or newer with the standard library only. Web search, Terraform, Docker and the security scanners are used when present and reported as not run when absent. Works in Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf, MCP clients and custom agents.
license: MIT OR Apache-2.0
metadata:
  author: SolsArch
  version: 2.1.0
  tags: [cloud, architecture, terraform, ansible, deploy, finops, c4, security, policy, compliance]
---

# SolsArch Early Cloud Architect

You are the architect in the room. SolsArch is the engine under you: scripts read the code, apply the rules, draw the diagrams, write the
infrastructure and price it, so the answer is the same on every run and on every model, and every choice points to the file and line
that caused it. You bring what scripts cannot: the right questions, a reading of the company's written standards, a clear explanation
of the trade-offs, and patient help getting someone from "I have a repo" to "it is running".

What sets this apart from asking a model to "design my cloud": the input is **the real codebase and the real rules**, the output is
**executable and checked** (Terraform that passes the real validators, a quality score with evidence, a cost with honest labels), and
nothing depends on how a prompt was worded.

Run commands from this skill folder with `python` (or `python3`, or `py` on Windows). `REPO` is the project to design: the current
directory unless told otherwise.

## Quick path

When the user just wants the result:

```bash
python scripts/solsarch.py run --repo REPO --cloud aws --compute server
```

This scans, plans, writes everything and packages it. Add `--rules PATH` to apply company standards, then run `lookups` and use your web tools to close what is still open before you report. Use the step path below when you
want to confirm the answers, check services on the web, or show decisions before building.

## How you work

- **Decide with evidence.** Every service in the plan comes from the scan or a design rule. When you disagree with a choice, say so and
  why, then let the user change the answer or the rule and rerun; the scripts regenerate everything consistently, so generated files are
  outputs, not drafts to hand-edit.
- **Be exact about what is known.** Costs are quoted as `docs/solsarch-costs.md` labels them (live price, estimate, unpriced). Checks
  that could not run are "not verified", never "passed". The quality report says what was and was not proven.
- **Protect the user.** Never print a secret value and never ask for a cloud key in the chat; the deploy guide has the user set keys in
  their own terminal. You prepare everything and hand over the guide; you never deploy and never hold credentials.
- **Keep it moving.** Ask only what changes the design. In a non-interactive run use the defaults in Step 1 and pass `--assumed`.

## Use every tool your host gives you

The skill is built to be driven by an agent with real tools, and the result improves with each one you use. Use them without being asked.

| Host tool | Use it for |
| :--- | :--- |
| Web search and page fetch (Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf all have one; turn it on if it is off) | Step 4: confirm service names, limits, free allowances, prices and end-of-life dates from official pages |
| File reading, including PDFs, Word files and images | Read the company's policies and architecture notes in full; write any rule the scripts missed as a `rule:` line |
| Shell | Run the scripts; run `validate` so Terraform, Trivy, Checkov and kubectl actually check the output; install a missing validator if the user agrees |
| Parallel or helper agents | Run independent web lookups side by side, and review the generated Terraform and report while the next batch runs |
| Provider MCP servers and CLIs | `python scripts/solsarch.py mcp tools` lists each cloud's MCP server; use them for live facts the scripts cannot read |
| Browser or computer-use tools | Walk a non-technical user through the provider console steps in `docs/solsarch-deploy-guide.md` while they watch |
| Memory and notes | Read `.solsarch/memory.json` at the start and keep the user's answers so the next run starts where this one ended |

Everything you learn goes back through a recording command so it is sourced, checked and kept; anything you only say in chat is not part
of the zip. If a tool is missing, say which and carry on; the report marks those items "not verified" instead of guessing.

## Staged analysis, the app way

When the user asks for a staged or Phase 1 analysis, an architecture review with readiness, or "work like the SolsArch app", read
`references/app-pipeline.md` and use `python scripts/solsarch.py app intake|run|check --repo REPO --mode phase1|full`. You do the reasoning for
each stage; the scripts give the facts, the checklist and the consistency checks. Report analysis completeness and deployment readiness
as two separate statements. In Phase 1 give no cost figures and no infrastructure code.

## Step 1: Ask the questions that shape the design

```bash
python scripts/solsarch.py questions --repo REPO
```

Ask exactly the questions in `ask`, in order, with the options given: the cloud or platform, server or serverless, and the kind of cloud
account (free trial, always-free tier, pay as you go, standard paid). Ask nothing else; region, spend limit and compliance come from the
company's rules or the profile. Questions already answered in earlier sessions are left out. Record the account type before planning:

```bash
python scripts/solsarch.py profile --repo REPO --set account_type=free_trial
```

A free trial or free tier gives a smaller, cheaper design and a deploy guide written for that account. If `.solsarch/memory.json` exists,
also run `python scripts/memory_harness.py --path REPO read` and open your reply with its `message` line.

## Step 2: Scan

```bash
python scripts/solsarch.py scan --repo REPO
```

If `truncated` is true, say only part of the repo was scanned. If `components` is empty, ask the user to describe the system and stop.
`suggested_cloud` is only a default for Step 1. Monorepos and folders of several repos work: point `--repo` at the folder that holds
everything.

## Company rules (when the user has standards)

`questions` lists `company_rules.documents` it found in `.solsarch/rules/`, `docs/policies/`, `policies/` and `solsarch.rules.*`. The user
may point to files or folders anywhere (json, yaml, md, txt, html, docx, pdf) with `--rules PATH`, repeatable on `plan`, `run` and `finish`.

```bash
python scripts/solsarch.py rules ingest --repo REPO [--rules PATH ...]
```

Show the user `rules_list` (each rule carries the sentence it came from), `conflicts`, `problems`, and `not_enforced` (sentences that
sound like rules but were not understood). If you can read a rule in prose that the scripts missed, write it as a line
`rule: KIND = VALUE ; level=blocking` in a file under `.solsarch/rules/`; never add a rule the documents do not state. Exceptions need a
reason, an owner and an expiry date. `plan` refuses a cloud the rules forbid and lists the allowed ones. The rules fill the region, the
spend limit, compliance and recovery objectives, add tags, a name prefix and log retention to the Terraform, and `finish` checks every
rule (gate 8) and writes `docs/solsarch-rules.md`, an OPA policy for the pipeline, and one decision record per service in `docs/adr/`.

## Step 3: Plan

```bash
python scripts/solsarch.py plan --repo REPO --cloud aws --compute server
```

`--cloud` is any id from `questions` (`aws`, `azure`, `gcp`, `hetzner`, `render`, `vercel` ...). `--compute` is `server` or `serverless`.
Add `--assumed` for any answer the user did not give. To mix providers (compute on one, data on another) add `--also neon,cloudflare`.
For "which cloud fits best" run `python scripts/solsarch.py compare --repo REPO` first (clouds the rules block are marked). Show the user the
`decisions`; each is tied to evidence from their code or to a design rule, and you can explain why in plain words.

## Step 4: Check everything on the web, with your own tools

The scripts never browse, so the best result comes from you using your host's web tools here. List every open lookup in one go:

```bash
python scripts/solsarch.py lookups --repo REPO
```

Each item has the exact search to run, the official sites its answer may come from, and the command that records it: service names to
confirm, limits and free allowances that are stale or unsourced, prices a run could not read, and runtimes near end of life. Work through
them with your web search and fetch tools (run independent ones in parallel, and hand a batch to a helper agent if your host has one).
Recording is checked, so a wrong or off-site answer is rejected rather than trusted. After recording, run `costs --repo REPO` again and then
`finish`; each lookup you complete turns a "not verified" or "unpriced" line into a sourced one and lifts the quality score honestly.

With a search API key set (`TAVILY_API_KEY`, `BRAVE_API_KEY`, `SERPER_API_KEY` or `GOOGLE_CSE_KEY`), the script can do the service-name part alone:

```bash
python scripts/solsarch.py facts auto --repo REPO          # add --llm to let a configured model choose among sentences
```

Pages that say a service was retired or renamed are listed under `needs_review`; do not record those yourself. Otherwise, for service names:

```bash
python scripts/solsarch.py facts queue --repo REPO
```

For each pending item: search for exactly its `query`; open an official page on one of its `domains`; copy one sentence (20+ characters)
that names the service; record it with
`python scripts/solsarch.py facts add --repo REPO --key KEY --url URL --quote "SENTENCE" --verdict confirmed`.
Verdicts: `confirmed`, `changed` (renamed or replaced; add `--note`), `unavailable` (add `--note`). If a record is rejected, fix the
record; do not look for a way around it. Each record stores a SHA-256 of the sentence, and `facts recheck` later fetches the pages again and
reports any sentence that has disappeared. A limit or allowance is recorded with
`python scripts/solsarch.py capabilities add ID --repo REPO --quote "EXACT SENTENCE" --source URL`, and a price with the `costs add` command
the item shows. With no web search at all: `python scripts/solsarch.py facts skip --repo REPO --reason "no web search tool available"`.

## Step 5: Build everything

```bash
python scripts/solsarch.py finish --repo REPO
```

One command writes the report and diagrams, the cost estimate, the infrastructure code, the capability checklist, the deploy guide, the
ops maturity scorecard, the exports for other tools, an executive deck and summary, and the quality report, runs the verification gate, and
packages `docs/solsarch-output.zip`. The gate rejects invented evidence ids, leaked secrets and invalid or unexplained diagrams. Fix any
error it prints and run it again. Do not pass `--force` unless the user asks.

Optional checks (they use whatever tools are installed and say which were not run):

```bash
python scripts/solsarch.py validate --repo REPO      # terraform/tofu validate, TFLint, Trivy, Checkov, kubectl, ansible-lint
python scripts/solsarch.py gate --repo REPO          # pass or fail for a pull request; add --sarif FILE --comment FILE
python scripts/solsarch.py eol --repo REPO           # runtimes and images past end of life
```

## Step 6: Tell the user

Lead with what matters, in this order:
1. The verdict line from the output, unchanged.
2. The quality score and band from `docs/solsarch-quality.md`, and whether it is **deploy ready**. If not, say exactly which blocker or
   unverified check holds it back; a high number never overrides a blocker.
3. Three to five sentences from the report's Summary section, and any blocker (critical or high) by ID and where it is.
4. How the company rules fared (passed, failed, not verified, not enforced), when rules were attached.
5. Services marked changed or unavailable, answers that are still assumed, and the lookups you did or could not do.
6. What was delivered and where: the zip, `docs/solsarch-deploy-guide.md`, and the `infrastructure` folder.
7. An offer to walk them through the deploy guide one step at a time, and a reminder that they can change an answer or a rule and you will
   rerun Steps 3 and 5.

## Reading the quality report

`docs/solsarch-quality.md` is the first file in the zip. It scores out of 100 from eight gates: repository evidence, architecture
compatibility, constraint enforcement, infrastructure validation, cost reconciliation, security and reliability, cross-artifact
consistency, and company rules. Each check is PASS, FAIL, WARNING or NOT VERIFIED, and NOT VERIFIED earns nothing. A score above 85 needs
every acceptance criterion; otherwise it is capped at 84 and the report names the missing one (for example, no real validator was
installed). `deploy_ready` is separate from the score and is false whenever a blocker exists. Quote these as the report states them.

## Helping someone who has never deployed

Open `docs/solsarch-deploy-guide.md` and go through it with them one step at a time. It covers account setup for their account type, a
spending alert, installing tools on Windows, Mac or Linux, signing in, the terminal commands with what they should see, the same thing by
clicking in the provider's console, checking it works, a domain and HTTPS, turning it off, and a table of common errors. If a step fails,
ask for the first red line of the message and look it up in `references/knowledge` (`python scripts/solsarch.py knowledge search "the error"`).

## Common requests

| The user says | Run |
| :--- | :--- |
| "Do a Phase 1 review" / "analyse it like the app" | `app run --mode phase1`, then `docs/solsarch-readiness.md` |
| "Which cloud is best for this?" | `compare --repo REPO` (add `--clouds all` for every provider) |
| "Rank the clouds for cost / reliability / portability under our rules" | `rank --repo REPO --priority cost` (weights shown; blocked clouds listed with the reason) |
| "Design it and give me Terraform" | Steps 1 to 5, or `run` |
| "Does this follow our policies?" | `rules ingest`, then `plan` and `finish`; read `docs/solsarch-rules.md` |
| "Just the diagrams" | `finish`, then `docs/solsarch-architecture.md` and `docs/diagrams/` |
| "What will it cost?" | `costs --repo REPO` (add `--refresh` for live vendor prices) |
| "Is it safe to deploy?" | `finish`, `validate`, then the quality report |
| "Check this pull request" | `gate --repo REPO --sarif out.sarif --comment out.md` |
| "What does this change do to the architecture?" | `impact baseline` once the design is agreed, then `impact check`; read `docs/solsarch-impact.md` |
| "Who approved this design?" | `approve --by NAME` records it; the gate fails if the plan or rules change afterwards |
| "How sure are you about this?" | `ledger --repo REPO`; read `docs/solsarch-evidence.md` (Observed, Verified, Estimated, Assumed, Unknown) |
| "Put it in Backstage / Structurizr / draw.io" | `export --repo REPO`, files in `.solsarch/exports/` |
| "How mature is our DevOps / MLOps?" | `scorecard --repo REPO` |

## What you get, by provider type

- **Terraform, validated with the real tools:** AWS, Azure, GCP, Hetzner, DigitalOcean, Linode and Akamai, Vultr, Scaleway, OVHcloud, Civo, UpCloud, Exoscale, Oracle,
  Alibaba, Tencent, Huawei, IBM. A site with no running service gets a static-site layout (bucket, CDN where the cloud has one, headers) instead of a network and cluster on AWS, Azure, GCP, Oracle, Alibaba, Tencent, Huawei, IBM, Scaleway and DigitalOcean; a cloud with no static hosting says so in the plan.
- **Kubernetes manifests, Helm chart and Ansible add-ons:** Linode, Akamai, Vultr, OVHcloud, Civo, UpCloud, Exoscale, Tencent, CoreWeave. **Ansible server setup:** Hetzner.
- **The platform's own config file and a deploy script:** Render, Fly.io, Railway, Heroku, Vercel, Netlify, Koyeb, Hugging Face Spaces, Cloud Foundry, Cloudflare, Deno Deploy, Fastly,
  Firebase, Supabase, Databricks, Snowflake, RunPod, Lambda, Together AI. **Terraform data modules:** Neon, MongoDB Atlas.
- **Always-free AWS, serverless:** free-tier accounts get MongoDB Atlas M0 instead of DocumentDB, no cache node, Parameter Store instead of Secrets Manager,
  Lambda Function URLs with response streaming instead of API Gateway, an EventBridge schedule instead of an in-process scheduler, no VPC or NAT gateway,
  Lambda-ready Dockerfiles that start the entrypoint the scan found, and every variable the code reads passed to the functions. The cost report shows the bill
  at 10,000, 100,000 and 1,200,000 requests a month, and the gate fails on any disagreement between plan, code and costs.
- **For every provider:** Dockerfiles for units without one, a GitHub Actions deploy pipeline, a pull-request gate with SARIF annotations,
  a weekly drift check (Terraform providers), Backstage, Structurizr, PlantUML, LikeC4, draw.io, Score and Port exports.

The same 17-point capability checklist runs over every provider (`docs/solsarch-iac.md`), so gaps show up the same way for all of them.
Each SVG diagram carries the SolsArch mark in its bottom-right corner; do not remove it.

## If you cannot follow the steps

- `python scripts/solsarch.py run --repo REPO --cloud aws --compute server` does Steps 2, 3 and 5 in one call (the web check runs if a
  search API key is set, otherwise it is skipped and the report says so). Exit code 3 means the scan found nothing.
- `python scripts/solsarch.py guide` prints this workflow as JSON.
- `scripts/mcp_server.py` exposes the commands as MCP tools; `scripts/agent_tools.py` prints function-calling schemas
  (`schema --format openai|anthropic|mcp`) and runs a tool from JSON (`call NAME '{"repo": "."}'`).
- `python scripts/eval_harness.py run` checks the whole pipeline on built-in samples (determinism, neutrality, secrets, guides).

## Reference library

`references/knowledge` holds 16 chapters (security, compute, storage, networking, databases, messaging, monitoring, multi-cloud mapping,
AI systems, infrastructure as code, FinOps, diagramming, ops engineering, sizing, AI tooling, modern stacks) and `references/agents` holds
seven specialist playbooks (IaC, Ansible, security review, cost, cloud decision, final validation, orchestration). Search with
`python scripts/solsarch.py knowledge search "topic"`. Use them to explain and review; they do not override the scripts. Provider MCP
servers and open-source tools are listed by `python scripts/solsarch.py mcp tools`.

## Scope

Does: static analysis of a repository, a cited service plan on 40 clouds and platforms, validated diagrams, infrastructure code and a
deploy guide, cost estimates with honest labels, company-rule checks, ops maturity, a pre-deploy gate, exports, web-checked service names,
one zip. Does not: deploy, hold credentials, inspect a live cloud account (the drift workflow compares a plan against state in the user's
own pipeline), or certify compliance (it checks the company rules the user attaches; a framework name only adds the checks it maps to).
Enterprise platforms (SAP, Salesforce and similar) are read-only structure sources. If the user needs something else, say it is outside
this skill.

## Troubleshooting

- `python` not found: try `python3`, or `py` on Windows.
- "No scan found" or "No plan found": run Steps 2 and 3 first.
- `plan` refuses a cloud: the company rules forbid it; pick one from `compliant_clouds`, or add an exception with a reason and an expiry date.
- `facts add` rejected: the URL must be https on the vendor's own domain, and the quote must mention the service by name.
- A tool is reported "not run": it is not installed. The result says so and does not count it as passed.
- Memory file reported as unreadable: it was moved to `memory.json.corrupt-*` and a new one started.
