#!/usr/bin/env python3
"""Architecture decision records, one per service decision, in the short MADR layout.

    adr.py --repo REPO      write docs/adr/NNNN-*.md and docs/adr/README.md from .solsarch/plan.json

Each record carries the evidence that caused the decision (file and line), the alternatives the catalogue lists, and the company rules
that apply to it with their latest result, so a reviewer can see why a service was chosen and which rule it was held to. The files are a
pure function of the saved plan, rules and quality result: the same inputs give the same text.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rules_extract  # noqa: E402

ALWAYS = ("allowed_clouds", "denied_clouds", "preferred_clouds", "data_residency", "allowed_regions", "compute_model")
DATA_ONLY = ("no_public_database", "no_public_storage", "encryption_at_rest", "encryption_in_transit", "customer_managed_keys", "rpo_minutes", "rto_minutes", "ha_multi_az")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48] or "decision"


def relevant(rule, decision):
    kind = rule["kind"]
    if kind in ALWAYS:
        return True
    if kind in DATA_ONLY:
        return decision.get("layer") == "data" or kind in ("encryption_in_transit",)
    if kind == "denied_services":
        return any(re.search(rules_extract.SERVICE_TERMS[k][0], decision.get("service", ""), re.I) for k in rule["value"] if k in rules_extract.SERVICE_TERMS)
    return False


def record(number, d, plan, rules, results):
    title = "%s: %s" % (d.get("role") or d.get("category") or d["key"], d.get("short") or d["service"])
    out = ["# %04d. %s" % (number, title), "",
           "- Status: proposed",
           "- Plan: `%s`" % plan["fingerprint"],
           "- Decision key: `%s` (confidence: %s)" % (d["key"], d.get("confidence", "inferred")), "",
           "## Context", "", d.get("why", "").strip() or "No reason recorded."]
    if d.get("evidence_text"):
        out += ["", "Evidence: %s." % d["evidence_text"]]
    out += ["", "## Decision", "", "Use **%s** on %s." % (d["service"], plan["cloud_name"] if d.get("provider", plan["cloud"]) == plan["cloud"] else d.get("provider"))]
    alts = d.get("alternatives") or []
    if alts:
        out += ["", "## Alternatives considered", ""] + ["- %s: %s" % (a["service"], a["reason"]) for a in alts]
    caveats = d.get("caveats") or []
    if caveats:
        out += ["", "## Consequences", ""] + ["- %s" % c for c in caveats]
    mine = [r for r in rules if relevant(r, d)]
    if mine:
        out += ["", "## Company rules that apply", "", "| Rule | Level | Result | Source |", "| :--- | :--- | :--- | :--- |"]
        for r in mine:
            src = r["source"]
            out.append("| %s: %s | %s | %s | %s, %s |" % (r["id"], r["kind"].replace("_", " "), r["level"], results.get(r["id"], "not checked"), src["file"], src["locator"]))
    return chr(10).join(out) + chr(10)


def build(repo):
    """{path: text} for docs/adr, or None when there is no plan."""
    state = Path(repo) / ".solsarch"
    plan = read(state / "plan.json")
    if not plan:
        return None
    data = read(state / "rules.json") or {}
    rules = [r for r in data.get("rules", []) if r.get("status") == "active"]
    results = {x["rule"]: x["status"] for x in (read(state / "quality.json") or {}).get("rule_results", [])}
    files, index = {}, []
    for number, d in enumerate(sorted(plan["decisions"], key=lambda x: x["key"]), 1):
        name = "adr/%04d-%s.md" % (number, slug(d["key"]))
        files[name] = record(number, d, plan, rules, results)
        index.append("| %04d | [%s: %s](%s) | %s | %s |" % (number, d.get("role") or d["key"], d.get("short") or d["service"], name.split("/", 1)[1], d.get("confidence", "inferred"),
                                                              d.get("evidence_text") or "design rule"))
    files["adr/README.md"] = chr(10).join(["# Architecture decision records", "",
                                          "One record per service decision in plan `%s`. Each shows the evidence, the alternatives and the company rules that apply." % plan["fingerprint"],
                                          "", "| No. | Decision | Confidence | Evidence |", "| :--- | :--- | :--- | :--- |"] + index) + chr(10)
    return files


def write(repo):
    files = build(repo)
    if files is None:
        return None
    root = Path(repo) / "docs"
    old = root / "adr"
    if old.is_dir():
        for p in sorted(old.glob("*.md")):
            p.unlink()
    for path, text in sorted(files.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    return sorted(files)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write architecture decision records")
    parser.add_argument("--repo", default=".")
    written = write(parser.parse_args(argv).repo)
    if written is None:
        print(json.dumps({"status": "error", "message": "No plan found. Run the scan and plan steps first."}))
        return 2
    print(json.dumps({"status": "written", "files": len(written)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
