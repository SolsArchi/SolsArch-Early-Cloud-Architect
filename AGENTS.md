# SolsArch Early Cloud Architect: instructions for AI agents

This repository is an Agent Skill that designs cloud architecture for a local codebase and delivers it ready to deploy:
40 clouds and platforms (AWS, Azure, Google Cloud, Hetzner, Render, Vercel, Railway and more), with Terraform, Ansible,
Kubernetes, platform config files, a CI pipeline, a cost estimate and a step-by-step deploy guide.
The skill lives in `solsarch-early-cloud-architect/`; `SKILL.md` there has the full rules. These instructions work
the same in Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf and custom agents.

## Use it on a project

Fast path, one command, no model judgement involved:

```bash
python solsarch-early-cloud-architect/scripts/solsarch.py run --repo PROJECT --cloud aws --compute server
```

Step by step (what the skill expects an agent to do):

1. `solsarch.py questions --repo PROJECT`: ask the user only the questions listed (cloud or platform; server or
   serverless; account type). Record the account type: `solsarch.py profile --repo PROJECT --set account_type=free_trial|free_tier|pay_as_you_go|standard`.
2. `solsarch.py scan --repo PROJECT`, then `plan --repo PROJECT --cloud ID --compute server|serverless` (add `--also neon,cloudflare` to mix providers).
3. Web check: with a search API key set run `facts auto`; otherwise `facts queue`, search each query, record one official
   sentence with `facts add`; without any web search `facts skip --reason "no web search tool"`.
4. `solsarch.py finish --repo PROJECT`: report, diagrams, cost estimate, infrastructure code, deploy guide, ops scorecard,
   exports, executive deck, verification gate, zip in `PROJECT/docs/`. Optional: `validate` and `gate`.
5. Tell the user the verdict, a short summary, blockers, and where the zip and `docs/solsarch-deploy-guide.md` are. Offer to
   walk them through the guide one step at a time.

`solsarch.py guide` prints this workflow as JSON. `solsarch.py compare --repo PROJECT` compares clouds.
Do not choose, rename or remove services yourself, do not edit generated files, never print secrets, never ask the user to
paste a cloud key into the chat, state costs only as `docs/solsarch-costs.md` gives them (with their label), and never deploy
for the user.

## Other ways in

- MCP server (any MCP-capable agent): `python solsarch-early-cloud-architect/scripts/mcp_server.py`
- Function-calling schemas for custom agents: `python solsarch-early-cloud-architect/scripts/agent_tools.py schema --format openai|anthropic|mcp`
- JSON command line: `python solsarch-early-cloud-architect/scripts/agent_tools.py call solsarch_run '{"repo": "."}'`
- Install into an agent: `python install.py --agent claude|codex|cursor|gemini|copilot|windsurf|generic`
- Self-check of the whole pipeline: `python solsarch-early-cloud-architect/scripts/eval_harness.py run`

## Working on this repository

```bash
python -m pytest tests          # about 5 minutes; add -k "not every_repo_shape" for a quick run
SOLSARCH_TEST_TERRAFORM=1 python -m pytest tests/test_iac.py   # also runs terraform validate on every cloud
python -m pytest tests/test_security.py   # hostile-repository and supply-chain checks; read SECURITY.md before changing how paths or names are used
python solsarch-early-cloud-architect/scripts/validate_skill.py solsarch-early-cloud-architect
```

Scripts use only the Python 3 standard library. Keep it that way. Generated output must be a pure function of the inputs: no
timestamps, no random values, fixed ordering. Never run `git add -A` at the repository root; stage the skill, tests and docs by
path (large local folders sit beside them). `assets/branding/` holds the full-size logos and is never installed; the skill
embeds a small mark in `scripts/brand.py`.
