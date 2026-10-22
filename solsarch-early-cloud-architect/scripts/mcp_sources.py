#!/usr/bin/env python3
"""Where each cloud publishes its MCP server, and the rules for using one.

    mcp_sources.py list [--provider hetzner]       what is known (references/mcp-sources.json, dated)
    mcp_sources.py discover --provider NAME        the official-registry query to run for a provider whose source is an organisation page
    mcp_sources.py tools                           the open-source infrastructure MCP servers and tools the skill can use, with how to start them

A source is only a candidate. Nothing is installed or called from here; a server is called only after the person
agrees, only with read-only tools, and never with a production credential the skill was not given.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

REF = Path(__file__).resolve().parent.parent / "references" / "mcp-sources.json"
REGISTRY_SEARCH = "https://registry.modelcontextprotocol.io/v0.1/servers?search=%s&version=latest"

# Open-source servers and CLIs that help with infrastructure code. The skill never requires them.
TOOLS = [
    {"id": "terraform-mcp-server", "publisher": "HashiCorp", "purpose": "Terraform registry lookups: provider docs, module search, resource schemas",
     "run": ["docker", "run", "-i", "--rm", "hashicorp/terraform-mcp-server"], "repo": "https://github.com/hashicorp/terraform-mcp-server"},
    {"id": "awslabs-terraform-mcp-server", "publisher": "AWS Labs", "purpose": "Terraform and Checkov guidance for AWS",
     "run": ["uvx", "awslabs.terraform-mcp-server@latest"], "repo": "https://github.com/awslabs/mcp"},
    {"id": "opentofu-mcp-server", "publisher": "OpenTofu", "purpose": "OpenTofu registry search and documentation",
     "run": ["npx", "-y", "@opentofu/opentofu-mcp-server"], "repo": "https://github.com/opentofu/opentofu-mcp-server"},
    {"id": "azure-mcp", "publisher": "Microsoft", "purpose": "Azure resource and documentation tools",
     "run": ["npx", "-y", "@azure/mcp@latest", "server", "start"], "repo": "https://github.com/Azure/azure-mcp"},
    {"id": "cloudflare-mcp", "publisher": "Cloudflare", "purpose": "Cloudflare account, Workers and docs tools",
     "run": ["npx", "-y", "mcp-remote", "https://docs.mcp.cloudflare.com/mcp"], "repo": "https://github.com/cloudflare/mcp-server-cloudflare"},
]
CLI_TOOLS = [
    {"id": "terraform", "check": ["terraform", "version"], "use": "init -backend=false, validate, fmt -check"},
    {"id": "tofu", "check": ["tofu", "version"], "use": "the same checks with OpenTofu"},
    {"id": "tflint", "check": ["tflint", "--version"], "use": "provider-aware lint rules"},
    {"id": "checkov", "check": ["checkov", "--version"], "use": "security and compliance policies over infrastructure code"},
    {"id": "trivy", "check": ["trivy", "--version"], "use": "misconfiguration scan of Terraform, Kubernetes and Dockerfiles"},
    {"id": "terraform-docs", "check": ["terraform-docs", "--version"], "use": "inputs and outputs tables for the README"},
    {"id": "infracost", "check": ["infracost", "--version"], "use": "price estimates from a Terraform plan"},
    {"id": "kubeconform", "check": ["kubeconform", "-v"], "use": "validate Kubernetes manifests against the API schemas"},
    {"id": "helm", "check": ["helm", "version", "--short"], "use": "helm lint and template"},
    {"id": "ansible-lint", "check": ["ansible-lint", "--version"], "use": "playbook lint"},
    {"id": "opa", "check": ["opa", "version"], "use": "evaluate references/policies/baseline.rego over a plan"},
]


def load():
    return json.loads(REF.read_text(encoding="utf-8"))


def listing(provider=None):
    data = load()
    rows = [s for s in data["sources"] if not provider or s["provider"] == provider]
    return {"checked": data["checked"], "note": data["note"], "sources": rows}


def discover(provider):
    for s in load()["sources"]:
        if s["provider"] == provider:
            query = s["name"].split(" (")[0]
            return {"provider": provider, "kind": s["kind"], "registry_query": REGISTRY_SEARCH % quote(query), "page": s["url"],
                    "rule": "Keep a result only when its publisher namespace is the provider's own; review it before use."}
    return {"provider": provider, "error": "unknown provider"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="MCP sources and open-source infrastructure tooling")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--provider")
    p = sub.add_parser("discover")
    p.add_argument("--provider", required=True)
    sub.add_parser("tools")
    args = parser.parse_args(argv)
    out = {"list": lambda: listing(args.provider), "discover": lambda: discover(args.provider),
           "tools": lambda: {"mcp_servers": TOOLS, "command_line_tools": CLI_TOOLS}}[args.command]()
    sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
