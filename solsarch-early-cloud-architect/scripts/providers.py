#!/usr/bin/env python3
"""The provider registry: every cloud, platform, edge, AI, data and enterprise provider SolsArch knows.

Standard library only. Provider files live in references/providers/*.json and are merged into the service
catalogue at load time, so adding a provider is a data change. Entries are compact: a service is
[service, short name, why] plus an optional object with caveat, partner, builtin or selfmanaged.

Kinds:
  cloud       infrastructure cloud (AWS, Azure, Google Cloud, DigitalOcean, ...)
  paas        application platform (Vercel, Fly.io, Render, ...)
  edge        CDN and edge platform (Cloudflare, Fastly, ...)
  gpu         AI and GPU cloud (CoreWeave, RunPod, ...)
  data        data platform (Neon, Supabase, Snowflake, ...)
  enterprise  enterprise platform read for structure only (SAP, Salesforce, ...); never a hosting target

Usage:
  python providers.py list [--kind K]      one line per provider
  python providers.py show ID              one provider as JSON
  python providers.py validate             check every provider file; exit 1 on a problem
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROVIDER_DIR = HERE.parent / "references" / "providers"
KINDS = ("cloud", "paas", "edge", "gpu", "data", "enterprise")
HOSTING_KINDS = ("cloud", "paas", "edge", "gpu", "data")
NEEDS = ("compute", "ingress", "edge", "network", "egress", "db_postgres", "db_mysql", "db_mongo", "cache",
         "queue_kafka", "queue_rabbitmq", "vector_service", "search", "gpu", "storage", "static_site", "identity",
         "secrets", "registry", "observability", "managed_ai", "warehouse")
MODES = ("server", "serverless", "any")
GROUP_ORDER = ("global-clouds", "app-platforms", "edge", "other-clouds", "ai-gpu", "data", "enterprise")
_cache = {}


class ProviderError(ValueError):
    pass


def _entry(raw, where):
    """[service, short, why, {extras}] or a dict -> a catalogue entry."""
    if isinstance(raw, dict):
        entry = dict(raw)
    elif isinstance(raw, list) and 3 <= len(raw) <= 4:
        entry = {"service": raw[0], "short": raw[1], "why": raw[2]}
        if len(raw) == 4:
            if not isinstance(raw[3], dict):
                raise ProviderError("%s: the fourth item must be an object" % where)
            entry.update(raw[3])
    else:
        raise ProviderError("%s: a service is [service, short, why] or an object" % where)
    for field in ("service", "short", "why"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ProviderError("%s: missing %s" % (where, field))
    entry.setdefault("alternatives", [])
    extras = {"partner", "builtin", "selfmanaged"} & set(entry)
    for name in extras:
        if not isinstance(entry[name], bool):
            raise ProviderError("%s: %s must be true or false" % (where, name))
    if entry.get("selfmanaged") and not entry.get("caveat"):
        entry["caveat"] = "You run, patch and back this up yourself."
    return entry


def _load_file(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProviderError("%s: %s" % (path.name, error))
    group = data.get("group")
    if group not in GROUP_ORDER:
        raise ProviderError("%s: unknown group '%s'" % (path.name, group))
    providers = data.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise ProviderError("%s: no providers" % path.name)
    out = {}
    for pid, raw in providers.items():
        where = "%s/%s" % (path.name, pid)
        if not pid.isalnum() or pid != pid.lower():
            raise ProviderError("%s: the id must be lowercase letters and digits" % where)
        if raw.get("kind") not in KINDS:
            raise ProviderError("%s: kind must be one of %s" % (where, ", ".join(KINDS)))
        brand = raw.get("brand") or {}
        for key in ("fill", "stroke", "text"):
            if not str(brand.get(key, "")).startswith("#"):
                raise ProviderError("%s: brand.%s must be a #colour" % (where, key))
        for key in ("name", "domains", "pricing_page"):
            if not raw.get(key):
                raise ProviderError("%s: missing %s" % (where, key))
        services = {}
        for need, options in (raw.get("services") or {}).items():
            if need not in NEEDS:
                raise ProviderError("%s: unknown need '%s'" % (where, need))
            if not isinstance(options, dict) or not options or not set(options) <= set(MODES):
                raise ProviderError("%s/%s: expected server, serverless or any" % (where, need))
            services[need] = {mode: _entry(item, "%s/%s/%s" % (where, need, mode)) for mode, item in options.items()}
        overlay = raw.get("overlay", [])
        if not set(overlay) <= set(NEEDS):
            raise ProviderError("%s: overlay lists an unknown need" % where)
        gaps = raw.get("gaps", {})
        if not set(gaps) <= set(NEEDS) | {"serverless"}:
            raise ProviderError("%s: gaps lists an unknown need" % where)
        out[pid] = {"id": pid, "group": group, "kind": raw["kind"], "name": raw["name"], "brand": brand,
                    "domains": raw["domains"], "pricing_page": raw["pricing_page"], "services": services,
                    "overlay": overlay, "gaps": gaps, "description": raw.get("description", ""),
                    "markers": raw.get("markers"), "artifacts": raw.get("artifacts", []),
                    "structure": raw.get("structure", {}), "integration": raw.get("integration", [])}
        if raw["kind"] == "enterprise" and not raw.get("markers"):
            raise ProviderError("%s: an enterprise platform needs markers" % where)
    return out


def registry(directory=None):
    """{id: provider} for every provider file, in a fixed order."""
    key = str(directory or PROVIDER_DIR)
    if key in _cache:
        return _cache[key]
    folder = Path(directory or PROVIDER_DIR)
    files = sorted(folder.glob("*.json"), key=lambda p: (GROUP_ORDER.index(json.loads(p.read_text(encoding="utf-8")).get("group", "")) if json.loads(p.read_text(encoding="utf-8")).get("group") in GROUP_ORDER else 99, p.name))
    merged = {}
    for path in files:
        for pid, provider in _load_file(path).items():
            if pid in merged:
                raise ProviderError("provider '%s' is defined twice" % pid)
            merged[pid] = provider
    _cache[key] = merged
    return merged


def hosting_ids():
    return [pid for pid, p in registry().items() if p["kind"] in HOSTING_KINDS]


def enterprise_ids():
    return [pid for pid, p in registry().items() if p["kind"] == "enterprise"]


def overlay_ids():
    return [pid for pid, p in registry().items() if p["kind"] != "enterprise" and p["overlay"]]


def palette():
    """{id: {fill, stroke, text}} for diagram colours."""
    return {pid: dict(p["brand"]) for pid, p in registry().items()}


def mermaid_classdefs():
    return ["classDef %s fill:%s,stroke:%s,color:%s" % (pid, p["brand"]["fill"], p["brand"]["stroke"], p["brand"]["text"])
            for pid, p in registry().items()]


def merged_catalog(base):
    """The base catalogue (AWS, Azure, Google Cloud services) plus every other provider."""
    catalog = json.loads(json.dumps(base))
    reg = registry()
    for pid, provider in reg.items():
        if provider["kind"] == "enterprise":
            continue
        existing = catalog["clouds"].get(pid, {})
        catalog["clouds"][pid] = {"name": provider["name"], "css": pid, "domains": provider["domains"],
                                  "group": provider["group"], "kind": provider["kind"],
                                  "pricing_page": provider["pricing_page"]}
        if existing:
            catalog["clouds"][pid]["domains"] = existing.get("domains", provider["domains"])
        for need, options in provider["services"].items():
            catalog["services"].setdefault(need, {})[pid] = options
    catalog["providers"] = reg
    return catalog


def validate():
    """Return a list of problems across every provider file (empty when sound)."""
    problems = []
    try:
        reg = registry(PROVIDER_DIR)
    except ProviderError as error:
        return [str(error)]
    for pid, provider in reg.items():
        if provider["kind"] == "enterprise":
            continue
        if pid not in ("aws", "azure", "gcp") and not provider["services"]:
            problems.append("%s has no services" % pid)
        for need in provider["services"]:
            if need in provider["gaps"]:
                problems.append("%s lists %s both as a service and a gap" % (pid, need))
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="SolsArch provider registry")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("list")
    p.add_argument("--kind", choices=KINDS)
    p = sub.add_parser("show")
    p.add_argument("id")
    sub.add_parser("validate")
    args = parser.parse_args(argv)
    try:
        reg = registry()
    except ProviderError as error:
        sys.stderr.write("error: %s" % error + chr(10))
        return 1
    if args.command == "list":
        for pid, provider in reg.items():
            if args.kind and provider["kind"] != args.kind:
                continue
            sys.stdout.write("%-14s %-11s %-12s %s%s" % (pid, provider["kind"], provider["group"], provider["name"], chr(10)))
        return 0
    if args.command == "show":
        if args.id not in reg:
            sys.stderr.write("error: unknown provider '%s'" % args.id + chr(10))
            return 2
        sys.stdout.write(json.dumps(reg[args.id], indent=2) + chr(10))
        return 0
    problems = validate()
    for problem in problems:
        sys.stderr.write(problem + chr(10))
    sys.stdout.write("%d providers, %d problems%s" % (len(reg), len(problems), chr(10)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
