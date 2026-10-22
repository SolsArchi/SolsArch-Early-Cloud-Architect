#!/usr/bin/env python3
"""Record web-search verification of the services in the plan.

The plan names each service and gives a fixed search query. The agent runs the query with
its web search tool and records what an official vendor page says. This script checks the
record before saving it, so the result is the same whatever model did the search:

- the page must be https and on the cloud vendor's own domain;
- the quote must be at least 20 characters and mention the service by name;
- a changed or unavailable verdict needs a note saying what changed;
- the date is set here, not by the model.

Standard library only. Usage:
    python facts.py queue --repo REPO [--limit 12]
    python facts.py add --repo REPO --key compute_api --url URL --quote TEXT --verdict confirmed
    python facts.py auto --repo REPO [--llm]       (with a search API key set; see web_tools.py)
    python facts.py recheck --repo REPO            (fetch each page again; report any recorded sentence that is gone)
    python facts.py skip --repo REPO --reason "no web search tool available"
    python facts.py list --repo REPO

Exit codes: 0 ok, 1 record rejected, 2 plan missing.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

VERDICTS = ("confirmed", "changed", "unavailable")
GENERIC = {"the", "and", "with", "for", "amazon", "azure", "google", "cloud", "aws", "service", "services",
           "managed", "minimum", "instance", "instances", "replica", "replicas", "microsoft", "extension"}
MIN_QUOTE = 20
POINTER_RE = re.compile(r"^\W*(?:for\b.{0,60}\bsee\b|see\b|go to\b|learn more\b|refer to\b)|\bsee the\b.{0,40}\b(?:documentation|docs|page)\b",
                        re.I)


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def plan_path(repo):
    return Path(repo) / ".solsarch" / "plan.json"


def facts_path(repo):
    return Path(repo) / ".solsarch" / "facts.json"


def load_plan(repo):
    try:
        plan = json.loads(plan_path(repo).read_text(encoding="utf-8"))
        if "verify_queue" not in plan:
            raise ValueError("not a SolsArch plan")
        return plan
    except (OSError, ValueError):
        return None


def load_facts(repo):
    try:
        data = json.loads(facts_path(repo).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"facts": [], "skipped": None}


def save_facts(repo, data):
    path = facts_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".facts-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def significant_tokens(service):
    words = re.findall(r"[A-Za-z0-9]+", service)
    return [w.lower() for w in words if len(w) >= 3 and w.lower() not in GENERIC]


def host_allowed(url, domains):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def validate(item, url, quote, verdict, note):
    """Return an error string, or None when the record is acceptable."""
    if verdict not in VERDICTS:
        return "verdict must be one of: " + ", ".join(VERDICTS)
    if not host_allowed(url, item["domains"]):
        return "url must be an https page on the vendor's own site (%s)" % ", ".join(item["domains"])
    quote = " ".join(quote.split())
    if len(quote) < MIN_QUOTE:
        return "quote must be at least %d characters copied from the page" % MIN_QUOTE
    tokens = significant_tokens(item["service"])
    if tokens and not any(t in quote.lower() for t in tokens):
        return "quote must mention the service (one of: %s)" % ", ".join(tokens)
    if POINTER_RE.search(quote):
        return ("quote is a pointer to other documentation, not a statement about the service. Copy a sentence that "
                "describes the service or says it is available")
    if verdict != "confirmed" and not (note or "").strip():
        return "a %s verdict needs --note saying what changed" % verdict
    return None


def cmd_queue(args):
    plan = load_plan(args.repo)
    if plan is None:
        return 2, {"status": "error", "message": "No plan found. Run the plan step first."}
    done = {f["key"] for f in load_facts(args.repo)["facts"]}
    pending = [q for q in plan["verify_queue"] if q["key"] not in done][:args.limit]
    return 0, {"status": "ok", "recorded": len(done), "pending": pending,
               "how": "For each item run a web search with exactly the query, open an official page on the domains "
                      "(their subdomains count, for example docs.cloud.google.com), then record it with: "
                      "solsarch.py facts add --repo REPO --key KEY --url URL --quote TEXT --verdict VERDICT"}


def cmd_add(args):
    plan = load_plan(args.repo)
    if plan is None:
        return 2, {"status": "error", "message": "No plan found. Run the plan step first."}
    item = next((q for q in plan["verify_queue"] if args.key in q.get("keys", [q["key"]])), None)
    if item is None:
        return 1, {"status": "rejected", "message": "Unknown key '%s'. Keys: %s" % (
            args.key, ", ".join(q["key"] for q in plan["verify_queue"]))}
    covered = item.get("keys", [item["key"]])
    error = validate(item, args.url, args.quote, args.verdict, args.note)
    if error:
        return 1, {"status": "rejected", "message": error}
    data = load_facts(args.repo)
    data["facts"] = [f for f in data["facts"] if f["key"] not in covered]
    for key in covered:
        record = {"key": key, "service": item["service"], "url": args.url,
                  "quote": " ".join(args.quote.split()), "verdict": args.verdict,
                  "note": (args.note or "").strip(), "date": today(),
                  "quote_sha256": hashlib.sha256(" ".join(args.quote.split()).encode("utf-8")).hexdigest()}
        if getattr(args, "page_sha256", None):
            record["page_sha256"] = args.page_sha256
        data["facts"].append(record)
    data["facts"].sort(key=lambda f: f["key"])
    data["skipped"] = None
    save_facts(args.repo, data)
    return 0, {"status": "recorded", "key": args.key, "covers": covered, "verdict": args.verdict, "date": today()}


def cmd_auto(args, search_fn=None, fetch_fn=None, llm=None):
    """Record the official sentence for pending items using a configured search API (and optionally a model). Anything it cannot
    record stays pending, and a page that says a service was retired or renamed is left for a person to review."""
    import web_tools
    plan = load_plan(args.repo)
    if plan is None:
        return 2, {"status": "error", "message": "No plan found. Run the plan step first."}
    if not (search_fn or web_tools.search_provider()):
        return 1, {"status": "no_search", "message": "No search API is configured. " + web_tools.status()["note"] + " Or run the searches yourself with 'facts queue' and 'facts add'."}
    use_llm = llm if llm is not None else (web_tools.llm_complete if getattr(args, "llm", False) else None)
    done = {f["key"] for f in load_facts(args.repo)["facts"]}
    pending = [q for q in plan["verify_queue"] if q["key"] not in done][:args.limit]
    recorded, review, missing = [], [], []
    for item in pending:
        kwargs = {"validate": validate, "llm": use_llm}
        if search_fn:
            kwargs["search_fn"] = search_fn
        if fetch_fn:
            kwargs["fetch_fn"] = fetch_fn
        found = web_tools.auto_verify(item, **kwargs)
        if found["status"] == "found":
            ns = argparse.Namespace(repo=args.repo, key=item["key"], url=found["url"], quote=found["quote"], verdict="confirmed", note="",
                                    page_sha256=found.get("page_sha256"))
            code, out = cmd_add(ns)
            (recorded if code == 0 else missing).append(item["key"])
        elif found["status"] == "needs_review":
            review.append({"key": item["key"], "url": found["url"], "quote": found["quote"], "why": found["why"]})
        else:
            missing.append(item["key"])
    return 0, {"status": "ok", "recorded": recorded, "needs_review": review, "still_pending": missing,
               "next": "Record the rest by hand with 'facts queue' and 'facts add', or skip with 'facts skip'." if (review or missing) else "All pending items are recorded."}


def cmd_recheck(args, fetch_fn=None):
    """Fetch each recorded page again and check the recorded sentence is still on it. Reports drift; changes nothing."""
    import web_tools
    fetch_fn = fetch_fn or web_tools.fetch_text
    data = load_facts(args.repo)
    out = {"still_present": [], "changed": [], "unreachable": []}
    seen = {}
    for rec in data["facts"]:
        if rec["url"] not in seen:
            seen[rec["url"]] = " ".join(fetch_fn(rec["url"]).split())
        text = seen[rec["url"]]
        if not text:
            out["unreachable"].append(rec["key"])
        elif " ".join(rec["quote"].split()) in text:
            out["still_present"].append(rec["key"])
        else:
            out["changed"].append({"key": rec["key"], "url": rec["url"], "recorded": rec["date"]})
    out["status"] = "ok" if not out["changed"] else "drift"
    out["checked"] = len(data["facts"])
    return (0 if not out["changed"] else 1), out


def cmd_skip(args):
    data = load_facts(args.repo)
    data["skipped"] = {"reason": args.reason.strip(), "date": today()}
    save_facts(args.repo, data)
    return 0, {"status": "skipped", "reason": args.reason.strip()}


def cmd_list(args):
    return 0, dict(load_facts(args.repo), status="ok")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record web verification of planned services")
    sub = parser.add_subparsers(dest="command", required=True)
    queue = sub.add_parser("queue")
    queue.add_argument("--repo", default=".")
    queue.add_argument("--limit", type=int, default=12)
    add = sub.add_parser("add")
    add.add_argument("--repo", default=".")
    add.add_argument("--key", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--quote", required=True)
    add.add_argument("--verdict", required=True)
    add.add_argument("--note")
    auto = sub.add_parser("auto")
    auto.add_argument("--repo", default=".")
    auto.add_argument("--limit", type=int, default=40)
    auto.add_argument("--llm", action="store_true", help="Let a configured language model choose among sentences when the plain rules cannot")
    recheck = sub.add_parser("recheck")
    recheck.add_argument("--repo", default=".")
    skip = sub.add_parser("skip")
    skip.add_argument("--repo", default=".")
    skip.add_argument("--reason", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    code, payload = {"queue": cmd_queue, "add": cmd_add, "auto": cmd_auto, "recheck": cmd_recheck, "skip": cmd_skip, "list": cmd_list}[args.command](args)
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
