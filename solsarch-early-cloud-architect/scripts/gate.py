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
    "min_readiness": 85,
}
# Billable Terraform resource types and the plan decision each one belongs to. A type the plan did not ask for is a contradiction.
BILLABLE = {"aws_nat_gateway": "egress", "aws_db_instance": "database", "aws_rds_cluster": "database", "aws_docdb_cluster": "database_mongo",
            "aws_elasticache_replication_group": "cache", "aws_elasticache_cluster": "cache", "aws_lb": "ingress",
            "aws_apigatewayv2_api": "ingress", "aws_api_gateway_rest_api": "ingress", "aws_secretsmanager_secret": "secrets",
            "aws_wafv2_web_acl": "edge", "aws_mq_broker": "queue_rabbitmq", "aws_msk_cluster": "queue_kafka"}
FREE_TIER_FORBIDDEN = ("aws_nat_gateway", "aws_docdb_cluster", "aws_elasticache_replication_group", "aws_elasticache_cluster",
                       "aws_secretsmanager_secret", "aws_lb", "aws_apigatewayv2_api", "aws_api_gateway_rest_api", "aws_rds_cluster", "aws_db_instance")
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


def contradictions(scan, plan, manifest, costs):
    """Places where the plan, the generated code and the cost report disagree. Any one of them fails the gate."""
    found = []
    if not manifest:
        return found
    types = set(manifest.get("resource_types", []))
    keys = {d["key"] for d in plan["decisions"] if d.get("provider", plan["cloud"]) == plan["cloud"]}
    if plan["cloud"] == "aws":
        for t in sorted(types & set(BILLABLE)):
            if BILLABLE[t] not in keys:
                found.append("The generated code creates %s but the plan has no %s service." % (t, BILLABLE[t]))
        if (plan.get("profile") or {}).get("account_type") == "free_tier":
            for t in sorted(types & set(FREE_TIER_FORBIDDEN)):
                found.append("The account is always-free but the generated code creates %s, which bills without a free allowance." % t)
        signals = scan.get("signals", {})
        if plan["compute_model"] == "serverless" and "aws_lambda_function" in types:
            if signals.get("streaming") == "used" and not manifest.get("streaming_compatible"):
                found.append("The code streams responses but the generated entry point cannot stream.")
            if signals.get("scheduler") == "used" and not manifest.get("has_scheduler"):
                found.append("The code runs an in-process scheduler but no managed schedule was generated.")
    if costs:
        priced = {l["decision"] for l in costs["lines"]} | {u["decision"] for u in costs["unpriced"]} | {i["decision"] for i in costs.get("included", [])}
        for t in sorted(types & set(BILLABLE)):
            if BILLABLE[t] in keys and BILLABLE[t] not in priced:
                found.append("The generated code creates %s but the cost report has no line for %s." % (t, BILLABLE[t]))
        stale = [l["decision"] for l in costs["lines"] if l["decision"] in ("egress", "cache", "database_mongo") and l["decision"] not in keys]
        found += ["The cost report prices %s but the plan does not contain it." % k for k in sorted(set(stale))]
    return found


def readiness(checks, rules, manifest, costs, bad_contradictions):
    """A 100-point deployment-readiness score. Points are earned from checks that ran; a check that did not run earns nothing."""
    by = {c["check"]: c for c in checks}
    ok = lambda name: by.get(name, {}).get("status") == "pass"  # noqa: E731
    parts = []
    parts.append(("Plan, code and costs agree", 20 if not bad_contradictions else 0))
    parts.append(("No violated design rule", 15 if not any(r["status"] == "violated" for r in rules) else 0))
    parts.append(("No blocking security finding", 15 if ok("Pre-deployment security checks") else 0))
    parts.append(("Infrastructure code validates", 15 if ok("Infrastructure code validation") else 0))
    cap = (manifest or {}).get("parity", {}).get("score", 0)
    parts.append(("Capability checklist", round(10 * min(1.0, cap / 100.0)) if manifest else 0))
    cost_pts = 0
    if costs:
        cost_pts = 10 if not costs["unpriced"] else 5  # every service is listed either way; 10 needs a price for each
    parts.append(("Every service has a cost line", cost_pts))
    ops = by.get("Operations maturity", {})
    parts.append(("Operations maturity", 10 if ops.get("status") == "pass" else (5 if ops.get("status") == "fail" and len(ops.get("items", [])) <= 2 else 0)))
    parts.append(("No end-of-life software", 5 if ok("End-of-life software") else 0))
    return {"score": sum(p for _, p in parts), "parts": parts}


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
        checks.append(check("Infrastructure code validation", "fail" if failed else ("pass" if validation["verdict"].startswith("passed") else "warn"),
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

    contra = contradictions(scan, plan, manifest, costs)
    checks.append(check("Plan, code and cost report agree", "fail" if contra else ("pass" if manifest else "skipped"),
                        "%d contradiction(s)" % len(contra) if manifest else "no generated code to compare", contra))
    ready = readiness(checks, rules, manifest, costs, contra)
    checks.append(check("Deployment readiness score", "fail" if ready["score"] < config["min_readiness"] else "pass",
                        "%d out of 100 (minimum %s)" % (ready["score"], config["min_readiness"]), ["%s: %d" % p for p in ready["parts"]]))
    failed = [c for c in checks if c["check"] and c["status"] == "fail"]
    return {"verdict": "fail" if failed else "pass", "readiness": ready, "plan_fingerprint": plan["fingerprint"], "config": {k: v for k, v in config.items() if not k.startswith("_")},
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
