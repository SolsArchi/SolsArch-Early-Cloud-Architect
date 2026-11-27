#!/usr/bin/env python3
"""A named person approves a specific architecture; a later change to the plan or the rules voids the approval.

    approvals.py approve --repo REPO --by NAME [--note TEXT]
    approvals.py status  --repo REPO

An approval records who agreed, when, the plan fingerprint, a hash of the company rules in force and the quality score at that moment.
It is kept in .solsarch/approvals.json (commit it) and docs/solsarch-approvals.md. The pull-request gate fails when the latest approval no
longer matches the plan or the rules, so an architecture cannot change after sign-off without someone signing off again.
This is a record for people to rely on, not a signature or an access control. The date comes from SOLSARCH_TODAY or the clock. Standard library only.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

NAME_LIMIT = 80
NOTE_LIMIT = 400


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def today():
    return os.environ.get("SOLSARCH_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def rules_hash(repo):
    data = read(Path(repo) / ".solsarch" / "rules.json") or {}
    parts = sorted("%s|%s|%s" % (r.get("id"), r.get("kind"), json.dumps(r.get("value"), sort_keys=True)) for r in data.get("rules", []) if r.get("status") == "active")
    return hashlib.sha256(chr(10).join(parts).encode("utf-8")).hexdigest()[:16] if parts else "none"


def clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def load(repo):
    return (read(Path(repo) / ".solsarch" / "approvals.json") or {}).get("approvals", [])


def status(repo):
    """none (no approval yet), approved, or stale with the reason."""
    plan = read(Path(repo) / ".solsarch" / "plan.json")
    history = load(repo)
    if not history:
        return {"state": "none", "detail": "No one has approved this architecture."}
    latest = history[-1]
    if not plan:
        return {"state": "stale", "detail": "No plan to compare with.", "approval": latest}
    reasons = []
    if latest["plan_fingerprint"] != plan.get("fingerprint"):
        reasons.append("the plan changed (%s to %s)" % (latest["plan_fingerprint"], plan.get("fingerprint")))
    if latest["rules_hash"] != rules_hash(repo):
        reasons.append("the company rules changed")
    if reasons:
        return {"state": "stale", "detail": "Approved by %s on %s, but %s. Approve again after review." % (latest["by"], latest["date"], " and ".join(reasons)), "approval": latest}
    return {"state": "approved", "detail": "Approved by %s on %s." % (latest["by"], latest["date"]), "approval": latest}


def approve(repo, by, note=""):
    by, note = clean(by, NAME_LIMIT), clean(note, NOTE_LIMIT)
    if not by:
        return {"status": "error", "message": "Say who is approving: --by NAME."}
    state = Path(repo) / ".solsarch"
    plan = read(state / "plan.json")
    if not plan:
        return {"status": "error", "message": "No plan found. Run the scan and plan steps first."}
    quality = read(state / "quality.json") or {}
    record = {"by": by, "note": note, "date": today(), "plan_fingerprint": plan.get("fingerprint"), "cloud": plan.get("cloud"), "compute_model": plan.get("compute_model"),
              "rules_hash": rules_hash(repo), "quality_score": quality.get("score"), "quality_plan_matches": quality.get("plan_fingerprint") == plan.get("fingerprint"),
              "deploy_ready": bool(quality.get("deploy_ready")) if quality.get("plan_fingerprint") == plan.get("fingerprint") else None}
    history = load(repo) + [record]
    state.mkdir(parents=True, exist_ok=True)
    (state / "approvals.json").write_text(json.dumps({"schema": "solsarch.approvals/1", "approvals": history}, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-approvals.md").write_bytes(render(history, status(repo)).encode("utf-8"))
    return {"status": "approved", "approval": record, "file": str(docs / "solsarch-approvals.md")}


def render(history, current):
    out = ["# Approvals", "", "Who agreed to this architecture, and for which version. An approval stops counting when the plan or the company rules change.", "",
           "Current state: **%s**. %s" % (current["state"], current["detail"]), "",
           "| Date | Approved by | Cloud | Plan | Rules | Score | Note |", "|---|---|---|---|---|---|---|"]
    for r in reversed(history):
        out.append("| %s | %s | %s | `%s` | `%s` | %s | %s |" % (r["date"], r["by"].replace("|", "/"), r["cloud"], r["plan_fingerprint"], r["rules_hash"],
                                                              r["quality_score"] if r["quality_plan_matches"] else "not current", r["note"].replace("|", "/") or "none"))
    out += ["", "This is a record for the team to rely on. It is not a signature and does not control who can deploy.", ""]
    return chr(10).join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record and check approval of an architecture")
    parser.add_argument("action", choices=("approve", "status"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--by", default="")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)
    if args.action == "approve":
        result = approve(args.repo, args.by, args.note)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "approved" else 2
    current = status(args.repo)
    print(json.dumps({k: v for k, v in current.items() if k != "approval"}, indent=2))
    return 0 if current["state"] == "approved" else 1


if __name__ == "__main__":
    sys.exit(main())
