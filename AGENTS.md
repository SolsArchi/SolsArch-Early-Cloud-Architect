# SolsArch Early Cloud Architect: instructions for AI agents

This repository is an Agent Skill that designs cloud architecture (AWS, Azure, Google Cloud) for a local codebase.
The skill lives in `solsarch-early-cloud-architect/`; `SKILL.md` there has the full rules. These instructions work
the same in Claude Code, Codex, Cursor, Gemini CLI, Copilot, Windsurf and custom agents.

## Use it on a project

Fast path, one command, no model judgement involved:

```bash
python solsarch-early-cloud-architect/scripts/solsarch.py run --repo PROJECT --cloud aws --compute server
```

Step by step (what the skill expects an agent to do):

1. `solsarch.py questions --repo PROJECT`: ask the user only the questions listed (cloud; server or serverless).
2. `solsarch.py scan --repo PROJECT`, then `plan --repo PROJECT --cloud aws|azure|gcp --compute server|serverless`.
3. With a web search tool: `facts queue`, search each query, record one official sentence with `facts add`.
   Without one: `facts skip --reason "no web search tool"`.
4. `solsarch.py finish --repo PROJECT`: report, up to 11 diagrams, verification gate, zip in `PROJECT/docs/`.
5. Tell the user the verdict, a short summary, blockers, and where the zip is.

`solsarch.py guide` prints this workflow as JSON. `solsarch.py compare --repo PROJECT` compares the three clouds.
Do not choose, rename or remove services yourself, do not edit the generated report or diagrams, never print secrets,
never state prices.

## Other ways in

- MCP server (any MCP-capable agent): `python solsarch-early-cloud-architect/scripts/mcp_server.py`
- Function-calling schemas for custom agents: `python solsarch-early-cloud-architect/scripts/agent_tools.py schema --format openai|anthropic|mcp`
- JSON command line: `python solsarch-early-cloud-architect/scripts/agent_tools.py call solsarch_run '{"repo": "."}'`
- Install into an agent: `python install.py --agent claude|codex|cursor|gemini|copilot|windsurf|generic`

## Working on this repository

```bash
python -m pytest tests          # about 4 minutes; add -k "not every_repo_shape" for a quick run
python solsarch-early-cloud-architect/scripts/validate_skill.py solsarch-early-cloud-architect
```

Scripts use only the Python 3 standard library. Keep it that way. `assets/branding/` holds the full-size logos
and is never installed; the skill embeds a small mark in `scripts/brand.py`.
