#!/usr/bin/env python3
"""The evidence ledger: every claim in the output with the proof behind it and an honest status.

    ledger.py --repo REPO            write .solsarch/ledger.json and docs/solsarch-evidence.md

Each record says what is claimed, how sure we are, where it comes from and what limits it:

    Observed    read from a file in the repository (file:line) or from a rule sentence in a company document
    Verified    a vendor page was read and the sentence is on record (limits, free allowances, live prices)
    Estimated   a number from the built-in price book or a sizing assumption; useful, not confirmed
    Assumed     a default chosen because the user did not answer; changes if the answer changes
    Unknown     could not be checked (offline, stale, unreadable or never sourced)

A verification ladder labels the generated infrastructure with the highest step actually reached:
Generated, Statically validated, Tool validated. (Plan verified and Deployed and tested need files the user supplies and are never
claimed here.) The ledger is a pure function of the files under .solsarch: no clock reads, no network. Standard library only.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import capabilities  # noqa: E402

STATUSES = ("Observed", "Verified", "Estimated", "Assumed", "Unknown")
LADDER = ("Generated", "Statically validated", "Tool validated", "Plan verified", "Deployed and tested")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def record(kind, claim, status, source, **extra):
    rec = {"kind": kind, "claim": claim, "status": status, "source": source}
    rec.update({k: v for k, v in extra.items() if v not in (None, "", [])})
    return rec


def decision_records(plan):
    out = []
    for d in plan.get("decisions", []):
        evidence = d.get("evidence_text") or ""
        confirmed = str(d.get("confidence", "")).lower() == "confirmed"
        status = "Observed" if evidence and confirmed else ("Assumed" if not evidence else "Observed")
        limits = "; ".join(d.get("caveats") or []) or ("Inferred from the code, not confirmed by you." if not confirmed else "")
        out.append(record("decision", "%s is used for %s." % (d.get("service"), d.get("role") if d.get("role") == d.get("category") else "%s (%s)" % (d.get("role"), d.get("category"))), status,
                          evidence or "no file evidence; chosen by a default rule", region=(plan.get("profile") or {}).get("region"),
                          validation="plan gate 1 (repository evidence)", limitations=limits, id="decision.%s" % d.get("key")))
    return out


def assumption_records(plan):
    return [record("assumption", a.get("text", a.get("key", "")), "Assumed", "default used because the question was not answered",
                   limitations="Change it with `profile --set` and run the plan again.", id="assumption.%s" % a.get("key"))
            for a in plan.get("assumptions", [])]


def cost_records(costs):
    out = []
    for line in (costs or {}).get("lines", []):
        live = line.get("origin") == "live"
        out.append(record("price", "%s costs $%s a month (%s %s at %s)." % (line.get("label"), line.get("monthly"), line.get("quantity"), line.get("unit"), line.get("unit_price")),
                          "Verified" if live else "Estimated", line.get("source") or "built-in price book", retrieved=line.get("retrieved"),
                          region=line.get("region"), validation="live vendor price" if live else "price book",
                          limitations="" if live else "Not read from the vendor today; check before budgeting.", id="price.%s" % line.get("resource_id")))
    for u in (costs or {}).get("unpriced", []):
        out.append(record("price", "%s has no price in this report." % u.get("service"), "Unknown", u.get("reason", "no source"),
                          region=u.get("region"), limitations="Excluded from the monthly total.", id="price.unpriced.%s.%s" % (u.get("decision"), u.get("meter"))))
    return out


def capability_records(repo, plan, scan):
    signals = (scan or {}).get("signals")
    records = capabilities.relevant(capabilities.load(repo), plan, signals)
    out = []
    for rid in sorted(records):
        rec = records[rid]
        st = capabilities.status(rec)
        verified = st == "verified"
        out.append(record("limit", "%s: %s" % (rid, rec.get("quote") or rec.get("claim") or "no sentence on record"), "Verified" if verified else "Unknown",
                          rec.get("source") or "no source", retrieved=rec.get("retrieved"),
                          validation="quote hash %s" % rec.get("sha256") if verified else "record is %s" % st,
                          limitations="" if verified else "Not used by the gates until it is re-read from the vendor page.", id="limit.%s" % rid))
    return out


def rule_records(rules):
    out = []
    for r in (rules or {}).get("rules", []):
        if r.get("status") != "active":
            continue
        src = r.get("source", {})
        out.append(record("rule", "%s: %s" % (r.get("kind"), json.dumps(r.get("value"), ensure_ascii=False)), "Observed",
                          "%s, %s: \"%s\"" % (src.get("file"), src.get("locator"), (src.get("sentence") or "")[:160]),
                          validation="company rules gate (8)", limitations="Extracted by fixed patterns; read it. Not a compliance certification." if r.get("origin") == "extracted" else "",
                          id="rule.%s" % r.get("id")))
    return out


def unverified_records(quality):
    return [record("check", str(item.get("detail", item) if isinstance(item, dict) else item), "Unknown", "quality gates",
                   limitations="This check could not run, so it earns no score.", id="check.%s" % (item.get("id", i) if isinstance(item, dict) else i))
            for i, item in enumerate((quality or {}).get("not_verified", []))]


def ladder(repo):
    """The highest verification step reached, and what is needed for the next one."""
    state = Path(repo) / ".solsarch"
    if not (state / "iac").is_dir():
        return {"level": "None", "next": "Generate the infrastructure code (finish)."}
    level, nxt = "Generated", "Run the built-in static check (finish does this)."
    static = read(state / "static-check.json") or {}
    if static.get("verdict") == "clean":
        level, nxt = "Statically validated", "Run `validate` where terraform or tofu is installed."
    validation = read(state / "validation.json") or {}
    if validation.get("verdict") in ("passed", "passed_with_findings"):
        level, nxt = "Tool validated", "Supply `terraform plan` output (not yet supported) to reach Plan verified."
    return {"level": level, "next": nxt}


CLAIM_RE = re.compile("(?<![A-Za-z])(?<!not )(?<!Not )(?<!un)(?<!Un)verified" + chr(92) + "b", re.I)
AUDITED = ("solsarch-report.md", "solsarch-costs.md", "solsarch-iac.md", "solsarch-deploy-guide.md")


def audit(repo, data):
    """Lines in the main documents that call something verified when the ledger holds no Verified record and no web check was confirmed."""
    confirmed = [f for f in ((read(Path(repo) / ".solsarch" / "facts.json") or {}).get("facts") or []) if f.get("verdict") == "confirmed"]
    if data["counts"]["Verified"] or confirmed:
        return []
    found = []
    for name in AUDITED:
        path = Path(repo) / "docs" / name
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CLAIM_RE.search(line):
                found.append({"file": "docs/" + name, "line": number, "text": line.strip()[:160]})
    return found


def build(repo):
    state = Path(repo) / ".solsarch"
    plan = read(state / "plan.json")
    if not plan:
        return None
    scan = read(state / "scan.json")
    records = (decision_records(plan) + assumption_records(plan) + cost_records(read(state / "costs.json"))
               + capability_records(repo, plan, scan) + rule_records(read(state / "rules.json")) + unverified_records(read(state / "quality.json")))
    records.sort(key=lambda r: (r["kind"], r.get("id", ""), r["claim"]))
    counts = {s: sum(1 for r in records if r["status"] == s) for s in STATUSES}
    return {"schema": "solsarch.ledger/1", "plan_fingerprint": plan.get("fingerprint"), "cloud": plan.get("cloud"), "counts": counts,
            "grounded_share": round((counts["Observed"] + counts["Verified"]) / len(records), 3) if records else 0.0,
            "ladder": ladder(repo), "records": records, "unsupported_claims": []}


def render(data):
    out = ["# Evidence ledger", "",
           "Every claim in this package with its proof. Status is one of: **Observed** (read from your files or rule documents), **Verified** (a vendor page was read and the sentence is on record), "
           "**Estimated** (built-in numbers, not confirmed), **Assumed** (a default because a question was not answered), **Unknown** (could not be checked). "
           "Nothing else in the package may call a statement verified.", "",
           "Plan fingerprint: `%s`. Cloud: %s." % (data["plan_fingerprint"], data["cloud"]), "",
           "## Summary", "", "| Status | Records |", "|---|---|"]
    out += ["| %s | %d |" % (s, data["counts"][s]) for s in STATUSES]
    out += ["", "Share backed by your files or a vendor page: **%d%%**." % round(data["grounded_share"] * 100), "",
            "## Verification ladder", "", "Reached: **%s**. Next: %s" % (data["ladder"]["level"], data["ladder"]["next"]), "",
            "Steps: %s. The skill never deploys, so the last two steps count only when you supply the plan output or a test result." % " > ".join(LADDER), ""]
    if data.get("unsupported_claims"):
        out += ["## Claims without proof", "", "These lines say \"verified\" but the ledger has no Verified record and no web check was confirmed. Fix the wording or record the check.", ""]
        out += ["- %s:%d: %s" % (c["file"], c["line"], c["text"]) for c in data["unsupported_claims"]] + [""]
    for kind, title in (("decision", "Design decisions"), ("rule", "Company rules"), ("limit", "Limits and free allowances"), ("price", "Prices"), ("assumption", "Assumptions"), ("check", "Checks that could not run")):
        rows = [r for r in data["records"] if r["kind"] == kind]
        if not rows:
            continue
        out += ["## %s" % title, "", "| Status | Claim | Source | Limits |", "|---|---|---|---|"]
        for r in rows:
            cell = lambda t: str(t or "").replace("|", "/").replace(chr(10), " ")[:220]  # noqa: E731
            out.append("| %s | %s | %s | %s |" % (r["status"], cell(r["claim"]), cell(r["source"]), cell(r.get("limitations"))))
        out.append("")
    return chr(10).join(out)


def write(repo, data):
    data["unsupported_claims"] = audit(repo, data)
    state = Path(repo) / ".solsarch"
    state.mkdir(parents=True, exist_ok=True)
    (state / "ledger.json").write_text(json.dumps(data, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-evidence.md").write_bytes(render(data).encode("utf-8"))
    return str(docs / "solsarch-evidence.md")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the evidence ledger")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    data = build(args.repo)
    if data is None:
        print(json.dumps({"status": "error", "message": "No plan found. Run the scan and plan steps first."}))
        return 2
    path = write(args.repo, data)
    print(json.dumps({"status": "written", "file": path, "counts": data["counts"], "ladder": data["ladder"]["level"], "grounded_share": data["grounded_share"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
