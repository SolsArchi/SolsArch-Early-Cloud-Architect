#!/usr/bin/env python3
"""Every web lookup the host agent should do for a project, in one ordered list.

    web_tasks.py --repo REPO            print the lookups as JSON

SolsArch never browses. This script gathers the open questions the other steps leave (service names to confirm, limits and free
allowances that are stale or unsourced, prices a run could not read, runtimes close to end of life) and, for each one, gives the exact
search to run, the official sites an answer may come from, and the command that records it. The agent runs the searches with its own web
search and fetch tools (Claude Code, Codex, Cursor, Gemini CLI, Copilot and others all have them) and, where it can, runs independent
lookups in parallel. A recorded answer is checked like any other: an https page on an official domain, with a quote that names the thing.
The result is identical for the same project state, so every agent starts from the same list. Standard library only.
"""
import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capabilities  # noqa: E402
import eol_check  # noqa: E402
import facts as facts_store  # noqa: E402

# Official sites for the clouds with the most pages. Other providers' domains come from their own plan entries or the record's source.
PRICING_DOMAINS = {
    "aws": ["aws.amazon.com", "docs.aws.amazon.com"],
    "azure": ["azure.microsoft.com", "learn.microsoft.com"],
    "gcp": ["cloud.google.com", "docs.cloud.google.com"],
    "hetzner": ["hetzner.com", "docs.hetzner.com"],
    "digitalocean": ["digitalocean.com", "docs.digitalocean.com"],
    "oci": ["oracle.com", "docs.oracle.com"],
    "scaleway": ["scaleway.com"],
    "ovhcloud": ["ovhcloud.com"],
    "vultr": ["vultr.com"],
    "linode": ["linode.com", "akamai.com"],
    "render": ["render.com"],
    "vercel": ["vercel.com"],
    "railway": ["railway.com", "railway.app"],
    "flyio": ["fly.io"],
}


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def host(url):
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def service_checks(repo):
    plan = facts_store.load_plan(repo)
    if not plan:
        return []
    done = {f["key"] for f in facts_store.load_facts(repo)["facts"]}
    skipped = bool(facts_store.load_facts(repo).get("skipped"))
    out = []
    for q in plan.get("verify_queue", []):
        if q["key"] in done:
            continue
        out.append({
            "kind": "service_check", "priority": 1, "subject": q["service"],
            "why": "Confirm the service still exists under this name and is offered as described.",
            "search": [q["query"]], "official_domains": q["domains"],
            "record": "python scripts/solsarch.py facts add --repo REPO --key %s --url URL --quote \"ONE SENTENCE\" --verdict confirmed|changed|unavailable" % q["key"],
            "note": "A page that says retired or renamed means verdict changed or unavailable, with --note." + (" (A skip was recorded earlier; recording now replaces it.)" if skipped else ""),
        })
    return out


def capability_checks(repo):
    plan = facts_store.load_plan(repo)
    if not plan:
        return []
    signals = (read(Path(repo) / ".solsarch" / "scan.json") or {}).get("signals")
    records = capabilities.relevant(capabilities.load(repo), plan, signals)
    out = []
    for rid in sorted(records):
        rec = records[rid]
        st = capabilities.status(rec)
        if st == "verified":
            continue
        domain = host(rec.get("source") or "")
        must = rec.get("must_contain") or []
        out.append({
            "kind": "limit_or_allowance", "priority": 2, "subject": rid,
            "why": "This limit or free allowance is %s, so it earns no quality-score credit until it is re-read." % st,
            "search": ["%s %s" % (rid.replace(".", " ").replace("_", " "), "official documentation")] + ([rec["source"]] if rec.get("source") else []),
            "official_domains": [domain] if domain else PRICING_DOMAINS.get(rid.split(".")[0], []),
            "record": "python scripts/solsarch.py capabilities add %s --repo REPO --quote \"EXACT SENTENCE\" --source URL" % rid,
            "note": ("The quote must contain: %s." % ", ".join(must)) if must else "Copy the sentence exactly as the page prints it.",
        })
    return out


def price_checks(repo):
    costs = read(Path(repo) / ".solsarch" / "costs.json")
    if not costs:
        return []
    out = []
    for u in costs.get("unpriced", []):
        provider = u.get("provider", "")
        out.append({
            "kind": "price", "priority": 3, "subject": u.get("service", u.get("meter", "")),
            "why": "No live or price-book price was available (%s), so this line is unpriced in the cost report." % u.get("reason", "unknown"),
            "search": [("%s price per %s %s %s" % (u.get("service", ""), u.get("unit", "unit"), provider, u.get("region") or "")).strip()],
            "official_domains": PRICING_DOMAINS.get(provider, []),
            "record": u.get("how", "python scripts/solsarch.py costs add --provider P --meter M --unit-price PRICE --unit UNIT --url URL --quote QUOTE"),
            "note": "Use the price for the region in the plan and the unit shown; copy the price sentence as the quote. Then run costs again.",
        })
    return out


def eol_checks(repo):
    try:
        eol = eol_check.check(repo)
    except (OSError, ValueError, KeyError):
        eol = {}
    out = []
    for item in (eol.get("end_of_life") or []) + (eol.get("ending_soon") or []):
        out.append({
            "kind": "end_of_life", "priority": 2, "subject": "%s %s" % (item.get("name", item.get("product")), item.get("version", "")),
            "why": "This runtime or base image is past or near end of life (%s)." % item.get("eol", "date unknown"),
            "search": ["%s %s end of life support policy" % (item.get("name", item.get("product", "")), item.get("version", ""))],
            "official_domains": ["endoflife.date"],
            "record": "No command: tell the user the supported replacement and the date, citing the page.",
            "note": "Name the file and line from the scan (%s:%s)." % (item.get("file"), item.get("line")),
        })
    return out


def collect(repo):
    tasks = service_checks(repo) + capability_checks(repo) + eol_checks(repo) + price_checks(repo)
    tasks.sort(key=lambda t: (t["priority"], t["kind"], t["subject"]))
    for number, task in enumerate(tasks, 1):
        task["id"] = "L%03d" % number
    return tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description="Every web lookup still open for a project")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    if facts_store.load_plan(args.repo) is None:
        print(json.dumps({"status": "error", "message": "No plan found. Run the scan and plan steps first."}))
        return 2
    tasks = collect(args.repo)
    kinds = {}
    for t in tasks:
        kinds[t["kind"]] = kinds.get(t["kind"], 0) + 1
    print(json.dumps({
        "status": "ok", "open": len(tasks), "by_kind": kinds, "tasks": tasks,
        "how": ["Run each search with your own web search tool and open the page with your fetch tool; take answers only from the official domains listed.",
                "Independent lookups can run in parallel, and a batch can go to a helper agent if your tool supports it.",
                "Record each answer with the command shown, then run: python scripts/solsarch.py finish --repo REPO",
                "If you have no web tools, say so: facts skip --reason \"no web search tool available\". Nothing is invented; the report marks these as not verified."],
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
