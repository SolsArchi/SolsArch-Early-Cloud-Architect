#!/usr/bin/env python3
"""Plan and record web verification of the chosen cloud services.

Standard library only. This script never browses; the agent runs the searches with its own web
search tool and logs what it read. Queries are generated from fixed templates in a fixed order,
so every model runs the same searches.

Commands:
  plan   PLACEMENTS.json                         print the fixed query list
  add    --file research.json --service S --url U --title T --finding F [--query Q] [--accessed DATE]
  list   --file research.json
  status PLACEMENTS.json --file research.json    which services still have no source
"""
import argparse
import datetime
import json
import os
import re
import sys
from urllib.parse import urlparse

CLOUD_NAMES = {"aws": "AWS", "azure": "Microsoft Azure", "gcp": "Google Cloud"}
QUERY_TEMPLATES = (
    "{service} {cloud} features limits and quotas site:{domain}",
    "{service} {cloud} deprecations and recent announcements site:{domain}",
)
DOCS_DOMAIN = {"aws": "docs.aws.amazon.com", "azure": "learn.microsoft.com", "gcp": "cloud.google.com"}
MAX_FINDING = 400
OFFICIAL_HINTS = ("aws.amazon.com", "docs.aws.amazon.com", "learn.microsoft.com", "azure.microsoft.com",
                  "cloud.google.com", "docs.cloud.google.com")


def load_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def search_name(service):
    """Drop parenthetical notes so the search is about the product, not our wording."""
    return re.sub(r"\s*\([^)]*\)", "", service).strip()


def plan(placements_doc):
    cloud = CLOUD_NAMES[placements_doc["cloud"]]
    domain = DOCS_DOMAIN[placements_doc["cloud"]]
    seen, queries = set(), []
    for p in placements_doc["placements"]:
        if p["service"] in seen:
            continue
        seen.add(p["service"])
        for tpl in QUERY_TEMPLATES:
            queries.append({
                "id": "Q%02d" % (len(queries) + 1),
                "service": p["service"],
                "placement": p["id"],
                "query": tpl.format(service=search_name(p["service"]), cloud=cloud, domain=domain),
            })
    return {"cloud": placements_doc["cloud"], "queries": queries,
            "instructions": "Run each query with your web search tool. Open the top official vendor result "
                            "(prefer docs.aws.amazon.com, learn.microsoft.com, cloud.google.com), then log one "
                            "finding per source with `research.py add`. If you have no web tool, log nothing and "
                            "write 'Web verification: not performed' in the report."}


def load_log(path):
    if not os.path.isfile(path):
        return {"entries": []}
    data = load_json(path)
    if not isinstance(data.get("entries"), list):
        raise ValueError("research file has no entries list")
    return data


def cmd_plan(args):
    json.dump(plan(load_json(args.placements)), sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


def cmd_add(args):
    url = urlparse(args.url)
    if url.scheme not in ("http", "https") or not url.netloc:
        print("error: --url must be an http(s) URL", file=sys.stderr)
        return 2
    finding = " ".join(args.finding.split())
    if not finding:
        print("error: --finding cannot be empty", file=sys.stderr)
        return 2
    if len(finding) > MAX_FINDING:
        print("error: --finding is %d characters; keep it under %d and paraphrase" % (len(finding), MAX_FINDING),
              file=sys.stderr)
        return 2
    if args.accessed and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.accessed):
        print("error: --accessed must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        data = load_log(args.file)
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    entry = {
        "id": "R%03d" % (len(data["entries"]) + 1),
        "service": args.service.strip(),
        "query": (args.query or "").strip(),
        "url": args.url,
        "title": args.title.strip(),
        "finding": finding,
        "accessed": args.accessed or datetime.date.today().isoformat(),
        "official": any(url.netloc.lower().endswith(h) for h in OFFICIAL_HINTS),
    }
    data["entries"].append(entry)
    os.makedirs(os.path.dirname(os.path.abspath(args.file)), exist_ok=True)
    with open(args.file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=True)
        fh.write("\n")
    print(json.dumps(entry, ensure_ascii=True))
    return 0


def cmd_list(args):
    try:
        data = load_log(args.file)
    except (ValueError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    for e in data["entries"]:
        print("%s [%s] %s - %s" % (e["id"], e["service"], e["title"], e["url"]))
    return 0


def status(placements_doc, log):
    covered = {e["service"] for e in log["entries"]}
    services = []
    for p in placements_doc["placements"]:
        if p["service"] not in services:
            services.append(p["service"])
    return {"covered": [s for s in services if s in covered], "missing": [s for s in services if s not in covered]}


def cmd_status(args):
    try:
        out = status(load_json(args.placements), load_log(args.file))
    except (ValueError, OSError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    json.dump(out, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0 if not out["missing"] else 1


def build_parser():
    ap = argparse.ArgumentParser(description="Plan and record web verification.")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("placements")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("add")
    p.add_argument("--file", required=True)
    p.add_argument("--service", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--finding", required=True)
    p.add_argument("--query", default="")
    p.add_argument("--accessed", default=None)
    p.set_defaults(func=cmd_add)
    p = sub.add_parser("list")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("status")
    p.add_argument("placements")
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_status)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
