#!/usr/bin/env python3
"""A pass or fail check to run on every pull request before anything is deployed.

    gate.py --repo REPO [--config FILE] [--sarif FILE] [--comment FILE]

It reads what the other steps wrote (scan, plan, design, costs, scorecard, validation) and applies thresholds that live in a
file you commit (`solsarch.gate.json` at the repository root, or `.solsarch/gate.json`). Defaults are sensible; every one can
be changed. It writes a Markdown comment for the pull request and a SARIF file that GitHub code scanning shows as annotations.
Exit code 0 passes, 1 fails. No cloud credential is needed or used.
"""
import argparse
import json
import sys
from pathlib import Path

DEFAULTS = {
    "fail_on_security": ["critical", "high"],
    "fail_on_design_rules": ["violated"],
    "fail_on_ops_findings": ["critical", "high"],
    "max_end_of_life": 0,
    "min_capability_score": 70,
    "min_ops_level": 0,
    "budget_overrun_percent": 10,
    "require_validation_pass": False,
    "require_costs": False,
}
SEVERITY_LEVEL = {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(repo, explicit=None):
    config = dict(DEFAULTS)
    for candidate in ([explicit] if explicit else []) + [Path(repo) / "solsarch.gate.json", Path(repo) / ".solsarch" / "gate.json"]:
        if candidate and Path(candidate).exists():
            custom = read(candidate) or {}
            config.update({k: v for k, v in custom.items() if k in DEFAULTS})
            config["_file"] = str(candidate)
            break
    return config


def check(name, status, detail, items=()):
    return {"check": name, "status": status, "detail": detail, "items": list(items)[:12]}


def evaluate(repo, config=None):
    config = config or load_config(repo)
    state = Path(repo) / ".solsarch"
    scan, plan, design = read(state / "scan.json"), read(state / "plan.json"), read(state / "design.json")
    if not (scan and plan and design):
        return {"verdict": "error", "message": "No scan, plan and design found. Run the scan, plan and finish steps first.", "checks": []}
    checks = []

    findings = scan.get("findings", [])
    bad = [f for f in findings if f.get("severity") in config["fail_on_security"]]
    checks.append(check("Pre-deployment security checks", "fail" if bad else "pass",
                        "%d blocking finding(s) of %d" % (len(bad), len(findings)),
                        ["%s %s (%s)" % (f["id"], f["title"], f["severity"]) for f in bad]))

    rules = design.get("rules", [])
    broken = [r for r in rules if r["status"] in config["fail_on_design_rules"]]
    bent = [r for r in rules if r["status"] == "bent"]
    checks.append(check("Design rules", "fail" if broken else "pass", "%d violated, %d bent with a stated risk, %d met" % (
        len(broken), len(bent), sum(1 for r in rules if r["status"] == "met")), ["%s %s: %s" % (r["id"], r["name"], r["detail"]) for r in broken]))

    eol = [r for r in rules if r["id"] == "DR-12" and r["status"] == "violated"]
    card = read(state / "ops-scorecard.json")
    if card:
        ops_bad = [f for f in card["findings"] if f["severity"] in config["fail_on_ops_findings"]]
        level_ok = card["overall_level"] >= config["min_ops_level"]
        checks.append(check("Operations maturity", "fail" if ops_bad or not level_ok else "pass",
                            "overall level %d (%s); %d blocking finding(s)" % (card["overall_level"], card["overall_label"], len(ops_bad)),
                            ["%s %s (%s)" % (f["rule"], f["message"], f["where"]) for f in ops_bad]))
    else:
        checks.append(check("Operations maturity", "skipped", "No scorecard found; run the scorecard step."))

    manifest = read(state / "iac-manifest.json")
    if manifest:
        score = manifest["parity"]["score"]
        checks.append(check("Infrastructure capability checklist", "fail" if score < config["min_capability_score"] else "pass",
                            "%.1f%% (minimum %s%%)" % (score, config["min_capability_score"]), ["%s" % g for g in manifest["parity"]["gaps"]]))
    else:
        checks.append(check("Infrastructure capability checklist", "skipped", "No infrastructure code was generated."))

    validation = read(state / "validation.json")
    if validation:
        failed = validation.get("failed", [])
        checks.append(check("Infrastructure code validation", "fail" if failed else ("pass" if validation["verdict"] == "passed" else "warn"),
                            "verdict %s" % validation["verdict"].replace("_", " "), failed + validation.get("skipped", [])[:4]))
    else:
        checks.append(check("Infrastructure code validation", "fail" if config["require_validation_pass"] else "skipped", "validate has not been run"))

    costs = read(state / "costs.json")
    budget = (design.get("profile") or {}).get("monthly_budget")
    if costs and budget:
        monthly = costs["totals"]["monthly"]
        limit = float(budget) * (1 + config["budget_overrun_percent"] / 100.0)
        checks.append(check("Monthly cost against budget", "fail" if monthly > limit else "pass",
                            "estimate %.0f against budget %s (%s%% allowed over)" % (monthly, budget, config["budget_overrun_percent"]),
                            ["%d lines are unpriced, so the estimate may be low" % costs["totals"]["unpriced_lines"]] if costs["totals"]["unpriced_lines"] else []))
    elif config["require_costs"]:
        checks.append(check("Monthly cost against budget", "fail", "no cost estimate or no monthly_budget in the profile"))
    else:
        checks.append(check("Monthly cost against budget", "skipped", "set monthly_budget in the profile and run the costs step to enable this"))

    over = len(eol)
    checks.append(check("End-of-life software", "fail" if over > config["max_end_of_life"] else "pass",
                        "past end of life: %s" % (eol[0]["detail"] if eol else "none found")))

    failed = [c for c in checks if c["check"] and c["status"] == "fail"]
    return {"verdict": "fail" if failed else "pass", "plan_fingerprint": plan["fingerprint"], "config": {k: v for k, v in config.items() if not k.startswith("_")},
            "checks": checks, "failed": [c["check"] for c in failed]}


def comment(result):
    icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN", "skipped": "SKIPPED"}
    lines = ["## SolsArch pre-deploy gate: **%s**" % result["verdict"].upper(), ""]
    if result["verdict"] == "error":
        return "\n".join(lines + [result["message"]]) + "\n"
    lines += ["| Check | Result | Detail |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (c["check"], icon[c["status"]], c["detail"]) for c in result["checks"]]
    for c in result["checks"]:
        if c["status"] == "fail" and c["items"]:
            lines += ["", "**%s**" % c["check"]] + ["- " + i for i in c["items"]]
    lines += ["", "_Plan `%s`. Thresholds are in `solsarch.gate.json`. Prepared with SolsArch Early Cloud Architect._" % result["plan_fingerprint"]]
    return "\n".join(lines) + "\n"


def sarif(repo):
    state = Path(repo) / ".solsarch"
    scan, design, card = read(state / "scan.json") or {}, read(state / "design.json") or {}, read(state / "ops-scorecard.json") or {}
    rules, results = {}, []

    def add(rule_id, title, level, message, uri=None, line=None, help_text=""):
        rules.setdefault(rule_id, {"id": rule_id, "name": title, "shortDescription": {"text": title}, "help": {"text": help_text or title}})
        loc = {"physicalLocation": {"artifactLocation": {"uri": uri or "."}}}
        if line:
            loc["physicalLocation"]["region"] = {"startLine": int(line)}
        results.append({"ruleId": rule_id, "level": level, "message": {"text": message}, "locations": [loc]})

    for f in scan.get("findings", []):
        ev = (f.get("evidence") or [{}])[0]
        add(f["id"], f["title"], SEVERITY_LEVEL.get(f["severity"], "warning"), "%s %s" % (f.get("detail", ""), f.get("fix", "")), ev.get("file"), ev.get("line"), f.get("fix", ""))
    for r in design.get("rules", []):
        if r["status"] in ("violated", "bent"):
            ev = r.get("evidence") or []
            first = ev[0] if ev and isinstance(ev[0], str) else None
            uri, line = (first.rsplit(":", 1) + [None])[:2] if first and ":" in first else (None, None)
            add(r["id"], r["name"], "error" if r["status"] == "violated" else "warning", "%s %s" % (r["detail"], r.get("action", "")), uri, line if line and str(line).isdigit() else None)
    for f in card.get("findings", []):
        add(f["rule"], f["message"], SEVERITY_LEVEL.get(f["severity"], "warning"), f["message"] + " " + f["fix"], f["where"], None, f["fix"])
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0",
            "runs": [{"tool": {"driver": {"name": "SolsArch Early Cloud Architect", "informationUri": "https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect",
                                          "rules": [rules[k] for k in sorted(rules)]}}, "results": results}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pre-deploy gate")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--config")
    parser.add_argument("--sarif")
    parser.add_argument("--comment")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    result = evaluate(args.repo, load_config(args.repo, args.config))
    if args.sarif:
        Path(args.sarif).write_text(json.dumps(sarif(args.repo), indent=2) + "\n", encoding="utf-8")
    if args.comment:
        Path(args.comment).write_text(comment(result), encoding="utf-8")
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
