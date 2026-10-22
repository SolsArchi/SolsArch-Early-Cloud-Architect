#!/usr/bin/env python3
"""SolsArch as tools any AI agent can call: function-calling schemas, a JSON dispatcher and the MCP server's table.

Standard library only. The same tool table feeds three routes:
  * function calling (OpenAI-style or Anthropic-style schemas) for custom agents,
  * a JSON command line for any agent that can run a shell command,
  * mcp_server.py, a Model Context Protocol server over stdio.
Every tool runs solsarch.py as a separate process, so behaviour is identical on every route.

Usage:
  python agent_tools.py schema [--format openai|anthropic|mcp]     print the tool definitions as JSON
  python agent_tools.py call NAME ['{"repo": "."}' | -]            run one tool, print its JSON result
  python agent_tools.py list                                       tool names and one-line descriptions
Exit codes: 0 ok, 1 the tool reported an error, 2 bad usage or unknown tool.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOLSARCH = HERE / "solsarch.py"
TIMEOUT_SECONDS = 600
sys.path.insert(0, str(HERE))
import providers  # noqa: E402

CLOUDS = providers.hosting_ids()
COMPUTE = ["server", "serverless"]

REPO = {"type": "string", "description": "Path of the project folder (a repo, a monorepo or a folder of repos). Default: current directory."}


def _prop(kind, description, **extra):
    return dict({"type": kind, "description": description}, **extra)


TOOLS = [
    {"name": "solsarch_guide", "description": "Return the SolsArch workflow, rules and steps as JSON. Call this first if you do not know the workflow.",
     "properties": {}, "required": [], "argv": lambda a: ["guide"]},
    {"name": "solsarch_questions", "description": "List the two questions to ask the user (cloud, server or serverless), minus anything already answered.",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["questions", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_scan", "description": "Scan the project for frameworks, databases, queues, containers, routes, endpoints, schemas and security findings.",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["scan", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_plan",
     "description": "Choose cloud services with fixed rules for the chosen cloud and compute model. Run solsarch_scan first.",
     "properties": {"repo": REPO, "cloud": _prop("string", "Cloud to use.", enum=CLOUDS),
                    "compute": _prop("string", "server = always-on containers, serverless = scales to zero.", enum=COMPUTE),
                    "assumed": _prop("boolean", "True when the user did not choose these answers."),
                    "also": _prop("string", "Comma-separated overlay providers, for example neon,cloudflare.")},
     "required": ["cloud", "compute"],
     "argv": lambda a: ["plan", "--repo", a.get("repo", "."), "--cloud", a["cloud"], "--compute", a["compute"]]
                       + (["--also", a["also"]] if a.get("also") else []) + (["--assumed"] if a.get("assumed") else [])},
    {"name": "solsarch_compare", "description": "Place the same scan on AWS, Azure and Google Cloud and write a side-by-side table. Does not save a plan.",
     "properties": {"repo": REPO, "compute": _prop("string", "Compute model to compare.", enum=COMPUTE),
                    "clouds": _prop("string", "Comma-separated provider ids, or all. Default aws,azure,gcp.")}, "required": [],
     "argv": lambda a: ["compare", "--repo", a.get("repo", "."), "--compute", a.get("compute", "server")]
                       + (["--clouds", a["clouds"]] if a.get("clouds") else [])},
    {"name": "solsarch_facts",
     "description": "Web verification of the chosen services. action=queue lists searches to run; add records one official source sentence; skip records that no web search was possible; list shows the log.",
     "properties": {"repo": REPO, "action": _prop("string", "What to do.", enum=["queue", "add", "skip", "list"]),
                    "key": _prop("string", "Queue item key (add)."), "url": _prop("string", "https URL on the vendor's own domain (add)."),
                    "quote": _prop("string", "One sentence copied from that page that names the service (add)."),
                    "verdict": _prop("string", "What the page says (add).", enum=["confirmed", "changed", "unavailable"]),
                    "note": _prop("string", "What changed (add, for changed or unavailable)."),
                    "reason": _prop("string", "Why no search was done (skip).")},
     "required": ["action"],
     "argv": lambda a: ["facts", a["action"], "--repo", a.get("repo", ".")]
                       + (["--key", a["key"]] if a.get("key") else []) + (["--url", a["url"]] if a.get("url") else [])
                       + (["--quote", a["quote"]] if a.get("quote") else []) + (["--verdict", a["verdict"]] if a.get("verdict") else [])
                       + (["--note", a["note"]] if a.get("note") else []) + (["--reason", a["reason"]] if a.get("reason") else [])},
    {"name": "solsarch_finish", "description": "Write the evidence, report, diagrams, verification and the output zip. Needs scan and plan first.",
     "properties": {"repo": REPO, "narrative": _prop("string", "Path of an optional markdown file of notes to include."),
                    "icon_dir": _prop("string", "Folder of official vendor icon SVGs."),
                    "force": _prop("boolean", "Package even if verification failed. Only when the user asks.")},
     "required": [],
     "argv": lambda a: ["finish", "--repo", a.get("repo", ".")] + (["--narrative", a["narrative"]] if a.get("narrative") else [])
                       + (["--icon-dir", a["icon_dir"]] if a.get("icon_dir") else []) + (["--force"] if a.get("force") else [])},
    {"name": "solsarch_verify", "description": "Check the written report against the scan evidence: invented ids, leaked secrets, broken diagrams.",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["verify", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_run",
     "description": "The whole workflow in one call: scan, plan, skip the web check, write everything and verify. Omitted cloud or compute are assumed and marked so.",
     "properties": {"repo": REPO, "cloud": _prop("string", "Cloud to use.", enum=CLOUDS),
                    "compute": _prop("string", "Compute model.", enum=COMPUTE),
                    "also": _prop("string", "Comma-separated overlay providers, for example neon,cloudflare."),
                    "icon_dir": _prop("string", "Folder of official vendor icon SVGs.")},
     "required": [],
     "argv": lambda a: ["run", "--repo", a.get("repo", ".")] + (["--cloud", a["cloud"]] if a.get("cloud") else [])
                       + (["--compute", a["compute"]] if a.get("compute") else [])
                       + (["--also", a["also"]] if a.get("also") else [])
                       + (["--icon-dir", a["icon_dir"]] if a.get("icon_dir") else [])},
    {"name": "solsarch_profile",
     "description": "Record optional answers that shape the design and the deploy guide: account_type (standard, free_trial, free_tier, pay_as_you_go), traffic_tier, high_availability, compliance, region, monthly_budget. Run plan again after changing account_type.",
     "properties": {"repo": REPO, "set": _prop("string", "Comma-separated KEY=VALUE pairs, for example account_type=free_trial,region=us-east-1.")},
     "required": [],
     "argv": lambda a: ["profile", "--repo", a.get("repo", ".")] + [x for pair in (a.get("set") or "").split(",") if pair.strip() for x in ("--set", pair.strip())]},
    {"name": "solsarch_costs", "description": "Write the monthly cost estimate (live vendor prices where available, estimates labelled). Needs scan, plan and finish or evidence first.",
     "properties": {"repo": REPO, "offline": _prop("boolean", "Use the saved price book only; no network."), "refresh": _prop("boolean", "Read fresh vendor prices.")},
     "required": [],
     "argv": lambda a: ["costs", "--repo", a.get("repo", ".")] + (["--offline"] if a.get("offline") else []) + (["--refresh"] if a.get("refresh") else [])},
    {"name": "solsarch_iac", "description": "Write the infrastructure code for the chosen provider (Terraform, Ansible, Kubernetes, Helm, Dockerfiles, platform config, CI pipeline) and the capability report. Needs scan, plan and evidence.",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["iac", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_deploy_guide", "description": "Write the step-by-step deployment guide for a person who has never deployed (terminal and console paths, matched to the account type).",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["deploy-guide", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_validate", "description": "Check the generated infrastructure code with the real tools (terraform/tofu, TFLint, Trivy, Checkov, kubectl, ansible-lint); repairs formatting and reports the rest. Tools that are not installed are reported as not run.",
     "properties": {"repo": REPO, "engine": _prop("string", "Which engine to use.", enum=["auto", "terraform", "tofu"]),
                    "offline": _prop("boolean", "Do not download providers.")}, "required": [],
     "argv": lambda a: ["validate", "--repo", a.get("repo", ".")] + (["--engine", a["engine"]] if a.get("engine") else []) + (["--offline"] if a.get("offline") else [])},
    {"name": "solsarch_knowledge", "description": "Search the architecture reference library (16 chapters and 7 specialist playbooks) for guidance on a topic.",
     "properties": {"query": _prop("string", "What to look up, for example 'terraform state locking'."), "limit": _prop("string", "How many results (1 to 10).")},
     "required": ["query"], "argv": lambda a: ["knowledge", "search", a["query"], "--limit", str(a.get("limit") or 5)]},
    {"name": "solsarch_eol", "description": "List runtimes, frameworks and base images in the project that are past or near end of life.",
     "properties": {"repo": REPO}, "required": [], "argv": lambda a: ["eol", "--repo", a.get("repo", ".")]},
    {"name": "solsarch_mcp", "description": "List where each provider publishes an MCP server and the open-source infrastructure servers and tools that can be used.",
     "properties": {"action": _prop("string", "What to show.", enum=["list", "discover", "tools"]), "provider": _prop("string", "Provider id (discover or list).")},
     "required": [], "argv": lambda a: ["mcp", a.get("action", "list")] + (["--provider", a["provider"]] if a.get("provider") else [])},
]
BY_NAME = {t["name"]: t for t in TOOLS}


def schema(fmt="openai"):
    out = []
    for tool in TOOLS:
        params = {"type": "object", "properties": tool["properties"], "required": tool["required"],
                  "additionalProperties": False}
        if fmt == "anthropic":
            out.append({"name": tool["name"], "description": tool["description"], "input_schema": params})
        elif fmt == "mcp":
            out.append({"name": tool["name"], "description": tool["description"], "inputSchema": params})
        else:
            out.append({"type": "function", "function": {"name": tool["name"], "description": tool["description"],
                                                         "parameters": params}})
    return out


def validate(tool, args):
    """Return an error message, or None when args fit the tool's schema."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    for name in tool["required"]:
        if args.get(name) in (None, ""):
            return "missing required argument '%s'" % name
    for name, value in args.items():
        spec = tool["properties"].get(name)
        if spec is None:
            return "unknown argument '%s'" % name
        if spec["type"] == "boolean" and not isinstance(value, bool):
            return "argument '%s' must be true or false" % name
        if spec["type"] == "string":
            if not isinstance(value, str):
                return "argument '%s' must be a string" % name
            if "enum" in spec and value not in spec["enum"]:
                return "argument '%s' must be one of: %s" % (name, ", ".join(spec["enum"]))
            if name in ("repo", "narrative", "icon_dir", "key", "url", "also", "clouds", "query", "provider") and value.startswith("-"):
                return "argument '%s' must not start with '-'" % name
    return None


def call(name, args=None):
    """Run one tool. Returns (exit_code, payload_dict). Never raises for bad input."""
    tool = BY_NAME.get(name)
    if tool is None:
        return 2, {"status": "error", "message": "unknown tool '%s'" % name, "tools": sorted(BY_NAME)}
    args = {} if args is None else args
    problem = validate(tool, args)
    if problem:
        return 2, {"status": "error", "message": problem}
    try:
        done = subprocess.run([sys.executable, "-B", str(SOLSARCH)] + tool["argv"](args), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 1, {"status": "error", "message": "timed out after %d seconds" % TIMEOUT_SECONDS}
    except OSError as error:
        return 1, {"status": "error", "message": str(error)}
    try:
        payload = json.loads(done.stdout)
    except ValueError:
        payload = {"status": "error", "message": (done.stderr or done.stdout).strip()[-800:] or "no output"}
    return done.returncode, payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="SolsArch tools for AI agents")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("schema")
    p.add_argument("--format", choices=("openai", "anthropic", "mcp"), default="openai")
    sub.add_parser("list")
    p = sub.add_parser("call")
    p.add_argument("name")
    p.add_argument("arguments", nargs="?", default="{}", help="JSON object, or - to read it from stdin")
    args = parser.parse_args(argv)
    if args.command == "schema":
        sys.stdout.write(json.dumps(schema(args.format), indent=2) + chr(10))
        return 0
    if args.command == "list":
        for tool in TOOLS:
            sys.stdout.write("%s  %s%s" % (tool["name"], tool["description"], chr(10)))
        return 0
    raw = sys.stdin.read() if args.arguments == "-" else args.arguments
    try:
        arguments = json.loads(raw or "{}")
    except ValueError as error:
        sys.stdout.write(json.dumps({"status": "error", "message": "arguments are not valid JSON: %s" % error}) + chr(10))
        return 2
    code, payload = call(args.name, arguments)
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + chr(10))
    return code


if __name__ == "__main__":
    sys.exit(main())
