---
name: solsarch-early-cloud-architect
description: Designs cloud architecture from a local codebase and delivers it ready to deploy. Scans the repo, picks services with fixed rules on 34 clouds and platforms (AWS, Azure, GCP, Hetzner, Render, Vercel, Railway and more), draws C4 and icon diagrams, writes Terraform, Ansible, Kubernetes, Helm, Dockerfiles, platform configs and a CI pipeline, estimates cost, scores ops maturity, runs a pre-deploy gate, exports to Backstage, Structurizr and draw.io, and writes a step-by-step deploy guide for non-technical people matched to their account type (free trial, free tier, pay as you go, paid). Use when the user asks to design cloud architecture, choose or compare clouds, deploy an app to AWS, Azure, GCP, Vercel, Render, Railway or any listed provider, generate Terraform or Ansible, draw a C4 diagram, review an architecture, check deployment blockers, or explain how to deploy step by step.
compatibility: Needs Python 3.8 or newer with the standard library only. Web search, Terraform, Docker and the security scanners are used when present and reported as not run when absent. Works in Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf, MCP clients and custom agents.
license: MIT OR Apache-2.0
metadata:
  author: SolsArch
  version: 2.0.0
  tags: [cloud, architecture, terraform, ansible, deploy, finops, c4, security]
---

# SolsArch Early Cloud Architect

Scripts make every decision, draw every diagram, write every file and the report, so the result is the same whichever
model runs this skill and the same on every run. Your job is to ask three questions, run the commands in order, do the web
searches the script asks for, and tell the user what came out. The model proposes nothing that the scripts do not check.

Run commands from this skill folder with `python` (or `python3`, or `py` on Windows).
`REPO` is the project the user wants designed: the current directory unless told otherwise.

## Rules

- Do not choose, rename, add or remove services yourself, and do not edit generated files. If you disagree with a choice, say so
  in your reply and let the user change the answers; then rerun Steps 3 and 5.
- Never print a secret value. Never ask the user to paste a cloud key into the chat; the deploy guide tells them to set it in their own terminal.
- State a cost only as `docs/solsarch-costs.md` gives it, with its label (live price, estimate, unpriced).
- Report only what the files say. Do not add claims about security, cost or availability.
- You never deploy for the user and never hold their credentials. You prepare everything and hand over the deploy guide.
- If the user cannot be asked (non-interactive run), use the defaults in Step 1 and pass `--assumed`.

## Step 1: Ask three questions

```bash
python scripts/solsarch.py questions --repo REPO
```

Ask exactly the questions in `ask`, in order, with the options given: the cloud or platform, server or serverless, and the
kind of cloud account (free trial, always-free tier, pay as you go, standard paid). Ask nothing else. Questions already
answered in earlier sessions are left out. Record the account type before planning:

```bash
python scripts/solsarch.py profile --repo REPO --set account_type=free_trial
```

A free trial or free tier gives a smaller, cheaper design. If `.solsarch/memory.json` exists, also run
`python scripts/memory_harness.py --path REPO read` and open your reply with its `message` line.

## Step 2: Scan

```bash
python scripts/solsarch.py scan --repo REPO
```

If `truncated` is true, say only part of the repo was scanned. If `components` is empty, ask the user to describe the system
and stop. `suggested_cloud` is only a default for Step 1.

## Step 3: Plan

```bash
python scripts/solsarch.py plan --repo REPO --cloud aws --compute server
```

`--cloud` is any id from `questions` (`aws`, `azure`, `gcp`, `hetzner`, `render`, `vercel` ...). `--compute` is `server` or
`serverless`. Add `--assumed` for any answer the user did not give. To mix providers (compute on one, data on another) add
`--also neon,cloudflare`. For "which cloud fits best" run `python scripts/solsarch.py compare --repo REPO` first. Show the user the
`decisions`. Each is tied to evidence from their code or to a design rule.

## Step 4: Check the services on the web

With a search API key set (`TAVILY_API_KEY`, `BRAVE_API_KEY`, `SERPER_API_KEY` or `GOOGLE_CSE_KEY`), let the script do it:

```bash
python scripts/solsarch.py facts auto --repo REPO          # add --llm to let a configured model choose among sentences
```

Pages that say a service was retired or renamed are listed under `needs_review`; do not record those yourself. Otherwise, with your own web search tool:

```bash
python scripts/solsarch.py facts queue --repo REPO
```

For each pending item: search for exactly its `query`; open an official page on one of its `domains`; copy one sentence (20+ characters)
that names the service; record it with
`python scripts/solsarch.py facts add --repo REPO --key KEY --url URL --quote "SENTENCE" --verdict confirmed`.
Verdicts: `confirmed`, `changed` (renamed or replaced; add `--note`), `unavailable` (add `--note`). If a record is rejected, fix the
record; do not look for a way around it. Each record stores a SHA-256 of the sentence; `facts recheck` later fetches the pages again and
reports any sentence that has disappeared. With no web search at all: `facts skip --repo REPO --reason "no web search tool available"`.

## Step 5: Build everything

```bash
python scripts/solsarch.py finish --repo REPO
```

One command writes: the report and diagrams (`docs/`), the cost estimate (`docs/solsarch-costs.md`), the infrastructure code
(`.solsarch/iac`), the capability checklist (`docs/solsarch-iac.md`), the step-by-step deploy guide (`docs/solsarch-deploy-guide.md`),
the ops maturity scorecard (`docs/solsarch-ops-scorecard.md`), the exports for other tools, an executive deck and summary, then runs
the verification gate and writes `docs/solsarch-output.zip` with all of it. The gate rejects invented evidence ids, leaked secrets and
invalid or unexplained diagrams. Fix any error it prints and run it again. Do not pass `--force` unless the user asks.

Optional checks (they use whatever tools are installed and say which were not run):

```bash
python scripts/solsarch.py validate --repo REPO      # terraform/tofu validate, TFLint, Trivy, Checkov, kubectl, ansible-lint
python scripts/solsarch.py gate --repo REPO          # pass or fail for a pull request; add --sarif FILE --comment FILE
python scripts/solsarch.py eol --repo REPO           # runtimes and images past end of life
```

## Step 6: Tell the user

Reply with:
1. The verdict line from the output, unchanged.
2. Three to five sentences from the report's Summary section.
3. Any blocker (critical or high) by ID and where it is.
4. Services marked changed or unavailable, and answers that are still assumed.
5. What was delivered and where: the zip, `docs/solsarch-deploy-guide.md`, and the `infrastructure` folder.
6. That you will walk them through the deploy guide step by step if they want, and that they can change an answer and you rerun Steps 3 and 5.

## Helping someone who has never deployed

Open `docs/solsarch-deploy-guide.md` and go through it with them one step at a time. It covers account setup for their account type,
a spending alert, installing tools on Windows, Mac or Linux, signing in, the terminal commands with what they should see, the same
thing by clicking in the provider's console, checking it works, a domain and HTTPS, turning it off, and a table of common errors. Never ask
them to share a key with you. If a step fails, ask for the first red line of the message and look it up in `references/knowledge`
(`python scripts/solsarch.py knowledge search "the error"`).

## What you get, by provider type

- **Terraform, validated with the real tools:** AWS, Azure, GCP, Hetzner, DigitalOcean, Linode and Akamai, Vultr, Scaleway, OVHcloud, Oracle,
  Alibaba, Tencent, Huawei, IBM. A site with no running service gets a static-site layout (storage, CDN, headers) instead of a network and cluster.
- **Kubernetes manifests, Helm chart and Ansible add-ons:** Linode, Akamai, Vultr, OVHcloud, Tencent, CoreWeave. **Ansible server setup:** Hetzner.
- **The platform's own config file and a deploy script:** Render, Fly.io, Railway, Heroku, Vercel, Netlify, Cloudflare, Deno Deploy, Fastly,
  Firebase, Supabase, Databricks, Snowflake, RunPod, Lambda, Together AI. **Terraform data modules:** Neon, MongoDB Atlas.
- **For every provider:** Dockerfiles for units without one, a GitHub Actions deploy pipeline, a pull-request gate with SARIF annotations,
  a weekly drift check (Terraform providers), Backstage, Structurizr, PlantUML, LikeC4, draw.io, Score and Port exports.

The same 17-point capability checklist runs over every provider (`docs/solsarch-iac.md`), so gaps show up the same way for all of them.

## Monorepos, several repos, and comparing clouds

Point `--repo` at the folder that holds everything. The scanner reads every service folder and the plan covers all of them together.
Each SVG diagram carries the SolsArch mark in its bottom-right corner; do not remove it.

## If you cannot follow the steps

- `python scripts/solsarch.py run --repo REPO --cloud aws --compute server` does Steps 2, 3 and 5 in one call (the web check runs
  if a search API key is set, otherwise it is skipped and the report says so). Exit code 3 means the scan found nothing.
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

Does: static analysis of a repository, a cited service plan on 34 clouds and platforms, validated diagrams, infrastructure code and a
deploy guide, cost estimates with honest labels, ops maturity, a pre-deploy gate, exports, web-checked service names, one zip.
Does not: deploy, hold credentials, inspect a live cloud account (the drift workflow compares a plan against state in the user's own
pipeline), or map controls to compliance frameworks. Enterprise platforms (SAP, Salesforce and similar) are read-only structure sources.
If the user needs something else, say it is outside this skill.

## Troubleshooting

- `python` not found: try `python3`, or `py` on Windows.
- "No scan found" or "No plan found": run Steps 2 and 3 first.
- `facts add` rejected: the URL must be https on the vendor's own domain, and the quote must mention the service by name.
- A tool is reported "not run": it is not installed. The result says so and does not count it as passed.
- Memory file reported as unreadable: it was moved to `memory.json.corrupt-*` and a new one started.
