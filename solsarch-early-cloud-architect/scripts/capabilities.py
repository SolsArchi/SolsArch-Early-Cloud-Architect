#!/usr/bin/env python3
"""Sourced facts about cloud services: limits, free allowances and timeouts the gates and the cost scenarios rely on.

    capabilities.py check   [--repo REPO]
    capabilities.py refresh [--repo REPO] [--only ID] [--write-reference]
    capabilities.py show ID [--repo REPO]

A fact is not trusted because it is in a file. Each record holds the value, the official page it comes from, the one sentence on
that page that states it (the quote), the day the page was read and a hash of the quote. `refresh` fetches each page again and
looks for a sentence that matches the record's pattern and contains every required phrase:

    verified   the quote was found on the page today (or on the recorded day) and holds every required phrase
    changed    the page now says something else; the old value must not be used until someone confirms it
    unreadable the page could not be read (offline, blocked, or built by scripts); the old quote stays but is not re-confirmed
    stale      verified once but older than 30 days

A value is used by the gates only while it is verified and not stale. Otherwise the caller gets None and says "not verified".
The recorded set ships in references/capabilities.json; a refresh in a project writes .solsarch/capabilities.json, which wins
when it is newer. Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import web_tools  # noqa: E402

REFERENCE = Path(__file__).resolve().parent.parent / "references" / "capabilities.json"
STALE_DAYS = 30


def today():
    return os.environ.get("SOLSARCH_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def digest(quote):
    return hashlib.sha256((quote or "").encode("utf-8")).hexdigest()[:16]


def load_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {r["id"]: r for r in data.get("records", [])}


def load(repo=None):
    """All records: the shipped set, replaced per id by the project's copy when that copy was read later."""
    records = load_file(REFERENCE)
    if repo:
        for rid, rec in load_file(Path(repo) / ".solsarch" / "capabilities.json").items():
            if rid not in records or (rec.get("retrieved") or "") >= (records[rid].get("retrieved") or ""):
                records[rid] = rec
    return records


def age_days(rec):
    try:
        return (date.fromisoformat(today()) - date.fromisoformat(rec.get("retrieved") or ""))\
            .days
    except ValueError:
        return None


def status(rec):
    """verified, stale, changed, unreadable or unsourced, judged from the record alone."""
    if not rec.get("source") or not rec.get("quote"):
        return "unsourced"
    if rec.get("state") in ("changed", "unreadable"):
        return rec["state"]
    if rec.get("sha256") != digest(rec["quote"]) or not all(p.lower() in rec["quote"].lower() for p in rec.get("must_contain", [])):
        return "changed"
    age = age_days(rec)
    if age is None:
        return "unsourced"
    return "stale" if age > STALE_DAYS else "verified"


def value(rid, repo=None, records=None):
    """(value, status). The value is None unless the record is verified and fresh."""
    rec = (records or load(repo)).get(rid)
    if not rec:
        return None, "unknown"
    st = status(rec)
    return (rec["value"] if st == "verified" else None), st


def find_quote(text, pattern, must):
    """The first sentence on the page that matches the pattern and holds every required phrase, or None."""
    rx = re.compile(pattern, re.I)
    for s in web_tools.sentences(text):
        if rx.search(s) and all(p.lower() in s.lower() for p in must):
            return s
    for line in text.splitlines():  # short table cells and list items are shorter than a sentence
        line = line.strip()
        if 12 <= len(line) <= 400 and rx.search(line) and all(p.lower() in line.lower() for p in must):
            return line
    return None


def refresh_one(rec, fetch=None):
    """A new record from the live page. The old quote is kept when the page cannot be read."""
    fetch = fetch or web_tools.fetch_text
    new = dict(rec)
    text = fetch(rec["source"])
    if not text:
        if rec.get("quote"):
            new["state"] = "unreadable"
        return new
    quote = find_quote(text, rec["pattern"], rec.get("must_contain", []))
    if quote is None:
        new["state"] = "changed"  # the page is readable and no longer says it
        return new
    new.pop("state", None)
    new.update(quote=quote, sha256=digest(quote), retrieved=today())
    return new


def refresh(repo=None, only=None, write_reference=False, fetch=None, max_age=0):
    """Re-read the pages. With max_age, only records older than that many days are read."""
    records = load(repo)
    out = {}
    for rid in sorted(records):
        rec = records[rid]
        if only and rid != only:
            out[rid] = rec
            continue
        age = age_days(rec)
        if max_age and age is not None and age <= max_age and status(rec) == "verified":
            out[rid] = rec
            continue
        out[rid] = refresh_one(rec, fetch)
    payload = {"note": "Sourced facts. Each record names the official page and the sentence it rests on.", "schema": 1,
               "records": [out[k] for k in sorted(out)]}
    target = REFERENCE if write_reference else (Path(repo) / ".solsarch" / "capabilities.json" if repo else None)
    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8", newline=chr(10))
    return out


def relevant(records, plan, signals=None):
    """The records this plan depends on: the chosen cloud (and any overlay provider) plus the topics the plan and the code touch."""
    providers = {plan["cloud"]} | set(plan.get("overlays", []))
    tags = {plan["compute_model"]} | {d["key"] for d in plan["decisions"]}
    if plan["cloud"] == "azure":
        tags.add("serverless_functions")
    if (signals or {}).get("streaming") == "used":
        tags.add("streaming")
    if plan.get("profile", {}).get("account_type") == "free_tier":
        tags.add("free_tier")
    out = {}
    for rid, rec in records.items():
        when = rec.get("when", [])
        if when and when[0] in providers and all(t in tags for t in when[1:]):
            out[rid] = rec
    return out


def add(repo, rid, quote, source=None):
    """Record a quote someone read on the official page, for pages a program cannot read. Checked like a fetched one."""
    records = load(repo)
    rec = records.get(rid)
    if rec is None:
        raise ValueError("unknown id %s" % rid)
    source = source or rec["source"]
    if not source.startswith("https://"):
        raise ValueError("the source must be an https URL")
    quote = " ".join(quote.split())
    if len(quote) < 12 or not all(p.lower() in quote.lower() for p in rec.get("must_contain", [])):
        raise ValueError("the quote must hold every required phrase: %s" % ", ".join(rec.get("must_contain", [])))
    new = dict(rec, source=source, quote=quote, sha256=digest(quote), retrieved=today(), origin="supplied")
    new.pop("state", None)
    records[rid] = new
    target = Path(repo) / ".solsarch" / "capabilities.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "note": "Facts for this project.", "records": [records[k] for k in sorted(records)]}
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8", newline=chr(10))
    return new


def summary(records):
    counts = {}
    for rec in records.values():
        counts[status(rec)] = counts.get(status(rec), 0) + 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sourced facts about cloud services")
    parser.add_argument("action", choices=("check", "refresh", "show", "add"))
    parser.add_argument("id", nargs="?")
    parser.add_argument("--repo")
    parser.add_argument("--only")
    parser.add_argument("--quote", help="add: the sentence read on the official page")
    parser.add_argument("--source", help="add: the https page it was read on")
    parser.add_argument("--write-reference", action="store_true", help="Maintainers: write the shipped file instead of the project copy")
    args = parser.parse_args(argv)
    if args.action == "add":
        try:
            print(json.dumps(add(args.repo or ".", args.id, args.quote or "", args.source), indent=2, ensure_ascii=False))
        except ValueError as error:
            print(json.dumps({"status": "rejected", "message": str(error)}))
            return 1
        return 0
    if args.action == "refresh":
        if os.environ.get("SOLSARCH_OFFLINE"):
            print(json.dumps({"status": "offline", "message": "Offline mode is on; nothing was read."}))
            return 1
        records = refresh(args.repo, args.only, args.write_reference)
    else:
        records = load(args.repo)
    if args.action == "show":
        print(json.dumps(records.get(args.id) or {"status": "unknown"}, indent=2, ensure_ascii=False))
        return 0
    rows = [{"id": k, "status": status(r), "value": r.get("value"), "unit": r.get("unit"), "age_days": age_days(r)} for k, r in sorted(records.items())]
    print(json.dumps({"summary": summary(records), "records": rows}, indent=2))
    return 0 if all(r["status"] == "verified" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
