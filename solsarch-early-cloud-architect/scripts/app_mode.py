#!/usr/bin/env python3
"""Work the way the SolsArch app does: staged analysis, intake questions, two readiness statements and consistency checks.

    app_mode.py stages  [--mode phase1|full]
    app_mode.py intake  --repo REPO [--set key=value ...]        the questions still open, and record answers
    app_mode.py run     --repo REPO [--mode phase1|full] [--cloud ID] [--compute server|serverless]
    app_mode.py check   --repo REPO [--mode phase1|full]         recompute readiness, the checklist and the invariants

Phase 1 reads, understands, draws and validates an architecture: no cost figures and no infrastructure code are delivered. Full adds cost,
infrastructure code, documentation and deeper security checks. The pipeline stages keep the app's agent numbers so the two read alike.
The scripts do the facts and the checks; the host agent does the reasoning and the narration (references/app-pipeline.md).
Analysis completeness (0 to 100) is reported separately from deployment readiness (Blocked, Not verified, Ready), and a check that could
not run is NOT_CHECKED, never PASS. Standard library only.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import architect  # noqa: E402

MODES = ("phase1", "full")
APP_CLOUDS = ("aws", "azure", "gcp")

# (agent id, name, modes, what the scripts do, what the host agent does)
STAGES = (
    ("0", "Orchestrator and narration", MODES, "runs the stages in order", "tell the user what each stage found, in one or two lines"),
    ("0.5", "Requirements intake", MODES, "intake: open questions, recorded answers", "ask only the open questions"),
    ("1", "Codebase intelligence", MODES, "scan", "explain services, entry points and data flow from the scan, citing files"),
    ("2", "API and requirements", MODES, "scan: dependencies and settings", "list the endpoints and auth you can read in the code, with file and line"),
    ("3", "Governance and policy", MODES, "rules ingest", "read the company documents in full; never invent a rule"),
    ("4", "Historical architecture", MODES, "impact check against a saved baseline", "read earlier architecture notes and say what changed"),
    ("5", "Traffic analytics", MODES, "profile: traffic tier", "ask for real numbers; label anything else as an assumption"),
    ("5.5", "Architecture consultation", ("full",), "compare and rank", "talk through compute strategy and let the user decide"),
    ("6", "Cloud decider", MODES, "plan; rank when no cloud was chosen", "recommend exactly one cloud with an honest runner-up"),
    ("7", "Cost and FinOps", ("full",), "costs", "quote only figures the cost report shows, with their label"),
    ("8", "Diagrams", MODES, "finish or report: C4, infrastructure, sequence and map diagrams", "explain each diagram"),
    ("9", "Infrastructure code", ("full",), "iac, validate", "report which checks ran and which did not"),
    ("9.5", "Configuration management", ("full",), "Ansible files where the compute is raw servers", "say when none is needed"),
    ("10", "Documentation", ("full",), "report, deploy guide, decision records", "walk the user through the deploy guide"),
    ("11", "Deep security", ("full",), "gate, validate", "review the findings; never call a check passed that did not run"),
    ("11.5", "Security checklist", MODES, "checklist", "explain every FAIL and every NOT_CHECKED"),
    ("12", "Final validation", MODES, "readiness and invariants", "fix wording that contradicts the facts, never the facts"),
    ("13", "Summary", MODES, "facts for the summary", "write a short executive summary using only the facts"),
)

# key: (question, allowed values or None, required, assumed default)
QUESTIONS = (
    ("business_driver", "What is driving this? (cost_reduction, compliance_deadline, cloud_migration, ai_readiness, outage, modernization, first_deployment, other)",
     ("cost_reduction", "compliance_deadline", "cloud_migration", "ai_readiness", "outage", "modernization", "first_deployment", "other"), True, "first_deployment"),
    ("risk_appetite", "How much risk is acceptable? (conservative, moderate, aggressive)", ("conservative", "moderate", "aggressive"), True, "moderate"),
    ("audience", "Who will read the result? (engineering, executive, board, finops, mixed)", ("engineering", "executive", "board", "finops", "mixed"), True, "engineering"),
    ("data_residency", "Must data stay somewhere? (none, eu_only, us_only, uk_only, apac_only, other)", ("none", "eu_only", "us_only", "uk_only", "apac_only", "other"), True, "none"),
    ("cloud", "Which cloud? (aws, azure, gcp, or not_sure to have one recommended)", APP_CLOUDS + ("not_sure",), True, "not_sure"),
    ("account_type", "What kind of cloud account? (free_trial, free_tier, pay_as_you_go, committed, not_sure)",
     ("free_trial", "free_tier", "pay_as_you_go", "committed", "not_sure"), True, "not_sure"),
    ("deployment", "Is this new, a move of something live, or a trial? (greenfield, migration, proof_of_concept)", ("greenfield", "migration", "proof_of_concept"), True, "greenfield"),
    ("compute", "Servers you manage or serverless? (server, serverless, not_sure)", ("server", "serverless", "not_sure"), True, "not_sure"),
)
# Follow-ups asked only when the repository gives a reason: (key, question, condition name)
CONDITIONAL = (
    ("sensitive_data", "Which sensitive data does it handle? (personal, health, payment, none)", "stores_data"),
    ("existing_ci", "Is there a pipeline we must not clash with?", "has_ci"),
    ("rollback", "Is an automated rollback needed, or is manual rollback fine?", "is_migration"),
    ("identity", "Which sign-in provider is used?", "has_auth"),
)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def state_of(repo):
    return Path(repo) / ".solsarch"


def save_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))


def stages(mode):
    return [{"agent": a, "name": n, "runs": mode in m, "status": "runs" if mode in m else "offloaded in Phase 1", "scripts": s, "agent_task": h}
            for a, n, m, s, h in STAGES]


def answers(repo):
    return (read(state_of(repo) / "intake.json") or {}).get("answers", {})


def triggers(repo):
    scan = read(state_of(repo) / "scan.json") or {}
    categories = {c.get("category") for c in scan.get("components", [])}
    names = " ".join(c.get("name", "") for c in scan.get("components", [])).lower()
    infra = json.dumps(scan.get("infrastructure", {})).lower()
    return {"stores_data": bool(categories & {"database", "vector", "storage"}), "has_ci": "workflow" in infra or "ci" in categories,
            "is_migration": answers(repo).get("deployment") == "migration", "has_auth": "auth" in categories or "auth" in names}


def intake_view(repo):
    given = answers(repo)
    open_questions = [{"key": k, "question": q, "options": list(o), "if_unanswered": d} for k, q, o, req, d in QUESTIONS if k not in given]
    fired = triggers(repo)
    open_questions += [{"key": k, "question": q, "options": [], "asked_because": cond} for k, q, cond in CONDITIONAL if fired[cond] and k not in given]
    return {"status": "ok", "answered": given, "open": open_questions, "required_open": sum(1 for q in open_questions if "if_unanswered" in q)}


def record_answers(repo, pairs):
    given = dict(answers(repo))
    allowed = {k: o for k, _, o, _, _ in QUESTIONS}
    extra = {k for k, _, _ in CONDITIONAL}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key, value = key.strip(), " ".join(value.split())[:200]
        if not sep or not value or (key not in allowed and key not in extra):
            return {"status": "error", "message": "Use --set key=value with a known key: %s" % ", ".join(list(allowed) + sorted(extra))}
        if key in allowed and value.lower() not in allowed[key]:
            return {"status": "error", "message": "%s must be one of: %s" % (key, ", ".join(allowed[key]))}
        given[key] = value.lower() if key in allowed else value
    save_json(state_of(repo) / "intake.json", {"schema": "solsarch.intake/1", "answers": given})
    return intake_view(repo)


def effective(repo):
    """Every question with its answer or the assumed default, and which ones were assumed."""
    given = answers(repo)
    values = {k: given.get(k, d) for k, _, _, _, d in QUESTIONS}
    return values, sorted(k for k, *_ in QUESTIONS if k not in given)


def apply_profile(repo, values):
    account = {"free_trial": "free_trial", "free_tier": "free_tier", "pay_as_you_go": "pay_as_you_go"}.get(values["account_type"], "standard")
    profile = architect.load_profile(repo)
    profile["account_type"] = account
    architect.save_profile(repo, profile)


def checklist(repo):
    scan = read(state_of(repo) / "scan.json") or {}
    findings = scan.get("findings", [])

    def hits(fid):
        out = []
        for f in findings:
            if f.get("id") == fid:
                out += ["%s:%s" % (e.get("file"), e.get("line")) for e in f.get("evidence", []) if not e.get("test_file")]
        return out

    def item(key, label, status, detail, blocking=False):
        return {"id": key, "label": label, "status": status, "detail": detail, "blocking": bool(blocking and status == "FAIL")}

    scanned = (scan.get("stats") or {}).get("files_scanned", 0)
    items = [item("input", "The scanner read real files", "PASS" if scanned else "NOT_CHECKED", "%d files scanned" % scanned)]
    for key, label, fid, blocking in (("secrets", "No hardcoded credentials", "SEC-005", True), ("db_exposure", "Database is not open to the internet", "SEC-002", True),
                                      ("ingress", "Private compute is not directly exposed", "SEC-001", False), ("data_loss", "No data kept on throwaway storage", "SEC-003", False)):
        found = hits(fid)
        if not scanned:
            items.append(item(key, label, "NOT_CHECKED", "no files were scanned"))
        else:
            items.append(item(key, label, "FAIL" if found else "PASS", ", ".join(found[:4]) if found else "nothing found by the scanner rule %s" % fid, blocking))
    history = scan.get("history") or {}
    ran = history.get("status") == "ran"
    old = hits("SEC-006")
    items.append(item("history", "No secret in git history", "FAIL" if old else ("PASS" if ran else "NOT_CHECKED"),
                      ", ".join(old[:4]) if old else ("history was searched" if ran else "git history was not searched"), True))
    for key, label in (("dependencies", "Dependencies have no known vulnerabilities"), ("cors", "Cross-origin access is restricted"),
                       ("auth", "Endpoints require authentication"), ("debug", "Debug mode is off"), ("http", "No plain-HTTP endpoints")):
        items.append(item(key, label, "NOT_CHECKED", "this skill has no checker for it; review it, or use a dedicated scanner"))
    return items


def completeness(repo, items):
    state = state_of(repo)
    scan, plan = read(state / "scan.json") or {}, read(state / "plan.json") or {}
    diagrams = len(list((Path(repo) / "docs" / "diagrams").glob("*.mmd"))) if (Path(repo) / "docs" / "diagrams").is_dir() else 0
    decisions = plan.get("decisions", [])
    cited = sum(1 for d in decisions if d.get("evidence_text"))
    arch = round(25 * cited / len(decisions)) if decisions and scan.get("components") else 0
    drawing = 20 if diagrams >= 4 else {3: 16, 2: 12, 1: 8}.get(diagrams, 0)
    given = answers(repo)
    required = [k for k, *_ in QUESTIONS]
    intake_points = round(10 * sum(1 for k in required if k in given) / len(required))
    api_points = (5 if scan.get("components") else 0) + (5 if scan.get("env_vars") or any(c.get("category") == "framework" for c in scan.get("components", [])) else 0)
    rules = read(state / "rules.json") or {}
    governance = (10 if any(r.get("status") == "active" for r in rules.get("rules", [])) else 0) + (5 if (state / "profile.json").exists() and "business_driver" in given else 0)
    ran = [i for i in items if i["status"] != "NOT_CHECKED"]
    security = round(20 * len(ran) / len(items)) if items else 0
    parts = [("Architecture understanding", arch, 25, "%d of %d services cite a file" % (cited, len(decisions))),
             ("Diagram coverage", drawing, 20, "%d diagrams" % diagrams),
             ("Requirements and API coverage", intake_points + api_points, 20, "%d of %d intake questions answered" % (sum(1 for k in required if k in given), len(required))),
             ("Governance and inputs", governance, 15, "%d company rules in force" % sum(1 for r in rules.get("rules", []) if r.get("status") == "active")),
             ("Repository security checklist", security, 20, "%d of %d checks ran" % (len(ran), len(items)))]
    return {"score": sum(p[1] for p in parts), "parts": [{"name": n, "points": p, "of": m, "evidence": e} for n, p, m, e in parts]}


def readiness(repo, mode, items):
    quality = read(state_of(repo) / "quality.json") or {}
    blockers = ["%s: %s" % (i["label"], i["detail"]) for i in items if i["blocking"]]
    if mode == "full":
        blockers += [b.get("detail", str(b)) for b in quality.get("blockers", [])]
    unverified = ["%s was not checked" % i["label"] for i in items if i["status"] == "NOT_CHECKED"]
    if mode == "phase1":
        unverified += ["Infrastructure code was not generated or checked", "Cost was not estimated", "No deep security audit ran"]
    else:
        if not quality or not quality.get("deploy_ready"):
            unverified.append("The quality gates do not report deploy ready")
        if not (state_of(repo) / "validation.json").exists():
            unverified.append("Real infrastructure validators have not run")
    unverified.append("Real traffic data was not supplied")
    status, label = ("blocked", "Blocked") if blockers else (("not_verified", "Not verified for deployment") if unverified else ("ready", "Ready"))
    return {"status": status, "label": label, "blockers": blockers, "not_verified": unverified}


def invariants(repo, mode, result):
    state = state_of(repo)
    plan = read(state / "plan.json") or {}
    docs = Path(repo) / "docs"
    diagrams = {p.stem: p.read_text(encoding="utf-8").lower() for p in sorted((docs / "diagrams").glob("*.mmd"))} if (docs / "diagrams").is_dir() else {}
    text = " ".join(diagrams.values())
    report = (docs / "solsarch-report.md").read_text(encoding="utf-8") if (docs / "solsarch-report.md").is_file() else ""
    out = []

    def add(ident, name, ok, detail, severity="warn"):
        out.append({"id": ident, "name": name, "ok": bool(ok), "detail": detail, "severity": severity})

    decisions = plan.get("decisions", [])
    drawable = [d for d in decisions if d.get("layer") in ("compute", "data", "edge", "network")]
    missing = [d["service"] for d in drawable if d.get("short", d["service"]).lower() not in text and d["service"].lower() not in text]
    add("I2", "Chosen services appear in diagrams", text and not missing, ", ".join(missing[:4]) or ("ok" if text else "no diagrams"))
    replaced = [str(d.get("replaces")) for d in decisions if d.get("replaces")]
    drawn = [r for r in replaced if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(r.lower()), text)]
    add("I3", "Replaced technologies are not drawn as live", not drawn, ", ".join(drawn[:3]) or "ok")
    if mode == "phase1":
        prices = re.findall(r"[$€£]\s?\d[\d,.]*", report)
        add("I7", "No price figures while cost is off", not prices, ", ".join(prices[:3]) or "none", "fail")
    contradiction = result["deployment_readiness"]["status"] == "ready" and any(i["status"] != "PASS" for i in result["checklist"])
    add("I8", "Readiness matches the evidence", not contradiction, "ok" if not contradiction else "Ready with checks not passed", "fail")
    add("I9", "A check that did not run is not a pass", all(i["status"] in ("PASS", "FAIL", "NOT_CHECKED") for i in result["checklist"]), "ok", "fail")
    absent = [n for n, present in (("scan", (state / "scan.json").exists()), ("plan", bool(plan)), ("report", bool(report))) if not present]
    add("I14", "Report inputs are present", not absent, ", ".join(absent) or "present", "fail")
    core = [n for n in ("c4-context", "c4-container") if not any(n in k for k in diagrams)]
    add("I15", "Core diagrams exist", not core, ", ".join(core) or "ok", "fail")
    values, _ = effective(repo)
    wrong = []
    if values["cloud"] != "not_sure" and plan and plan.get("cloud") != values["cloud"]:
        wrong.append("asked for %s, planned %s" % (values["cloud"], plan.get("cloud")))
    if values["compute"] != "not_sure" and plan and plan.get("compute_model") != values["compute"]:
        wrong.append("asked for %s compute, planned %s" % (values["compute"], plan.get("compute_model")))
    add("I16", "The user's choices are respected", not wrong, "; ".join(wrong) or "ok", "fail")
    return {"checked": [i["id"] for i in out], "violations": [i for i in out if not i["ok"]], "results": out,
            "note": "The ids match the app's consistency checks. The app's other checks need model-written artifacts this skill does not produce."}


def check(repo, mode):
    if not read(state_of(repo) / "plan.json"):
        return None
    items = checklist(repo)
    values, assumed = effective(repo)
    result = {"schema": "solsarch.app/1", "mode": mode, "plan_fingerprint": read(state_of(repo) / "plan.json").get("fingerprint"), "intake": values, "assumed": assumed,
              "checklist": items, "analysis_completeness": completeness(repo, items)}
    result["deployment_readiness"] = readiness(repo, mode, items)
    result["invariants"] = invariants(repo, mode, result)
    result["offloaded"] = [s["name"] for s in stages(mode) if not s["runs"]]
    return result


def render(result):
    c, d = result["analysis_completeness"], result["deployment_readiness"]
    out = ["# Readiness", "", "Mode: **%s**. Two separate statements: how complete the analysis is, and whether the evidence supports deploying." % ("Phase 1" if result["mode"] == "phase1" else "Full pipeline"), "",
           "## Analysis completeness: %d out of 100" % c["score"], "", "| Part | Points | Evidence |", "|---|---|---|"]
    out += ["| %s | %d of %d | %s |" % (p["name"], p["points"], p["of"], p["evidence"]) for p in c["parts"]]
    out += ["", "## Deployment readiness: %s" % d["label"], ""]
    out += (["Blocking:", ""] + ["- " + b for b in d["blockers"]] + [""]) if d["blockers"] else []
    out += (["Not verified:", ""] + ["- " + u for u in d["not_verified"]] + [""]) if d["not_verified"] else []
    out += ["## Repository security checklist", "", "| Check | Result | Detail |", "|---|---|---|"]
    out += ["| %s | %s | %s |" % (i["label"], i["status"], i["detail"].replace("|", "/")) for i in result["checklist"]]
    inv = result["invariants"]
    out += ["", "## Consistency checks", "", "Checked: %s. %s" % (", ".join(inv["checked"]), inv["note"]), ""]
    out += ["- %s %s: %s" % (v["id"], v["name"], v["detail"]) for v in inv["violations"]] or ["No violations."]
    if result["assumed"]:
        out += ["", "## Assumed answers", "", ", ".join("%s = %s" % (k, result["intake"][k]) for k in result["assumed"])]
    if result["offloaded"]:
        out += ["", "## Offloaded in this mode", "", ", ".join(result["offloaded"]) + ". Run the full mode to include them."]
    return chr(10).join(out) + chr(10)


def write(repo, result):
    save_json(state_of(repo) / "app.json", result)
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-readiness.md").write_bytes(render(result).encode("utf-8"))
    return str(docs / "solsarch-readiness.md")


def bundle(repo):
    """The Phase 1 deliverable: report, diagrams and readiness. No cost file and no infrastructure code goes in."""
    docs = Path(repo) / "docs"
    wanted = [docs / n for n in ("solsarch-readiness.md", "solsarch-report.md", "solsarch-architecture.md")]
    wanted += sorted((docs / "diagrams").glob("*")) if (docs / "diagrams").is_dir() else []
    target = docs / "solsarch-phase1.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in wanted:
            if path.is_file() and not path.is_symlink():
                info = zipfile.ZipInfo(path.relative_to(docs).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())
    return str(target)


def step(args, env=None):
    done = subprocess.run([sys.executable, "-I"] + args, capture_output=True, text=True, env=env)
    return done.returncode, done.stdout.strip()[-1200:]


def run(repo, mode, cloud=None, compute=None):
    solsarch = str(HERE / "solsarch.py")
    pairs = (["cloud=%s" % cloud] if cloud else []) + (["compute=%s" % compute] if compute else [])
    if pairs:
        recorded = record_answers(repo, pairs)
        if recorded["status"] != "ok":
            return recorded
    code, out = step([solsarch, "scan", "--repo", str(repo)])
    if code:
        return {"status": "error", "step": "scan", "detail": out}
    values, _ = effective(repo)
    apply_profile(repo, values)
    chosen, recommendation = values["cloud"], None
    model = values["compute"] if values["compute"] != "not_sure" else "server"
    if chosen == "not_sure":
        code, out = step([solsarch, "rank", "--repo", str(repo), "--clouds", ",".join(APP_CLOUDS), "--compute", model])
        ranked = (read(state_of(repo) / "ranking.json") or {}).get("ranking", [])
        if code or not ranked:
            return {"status": "error", "step": "rank", "detail": out}
        chosen = ranked[0]["cloud"]
        recommendation = {"recommended": chosen, "runner_up": ranked[1]["cloud"] if len(ranked) > 1 else None, "scores": {r["cloud"]: r["total"] for r in ranked}}
    code, out = step([solsarch, "plan", "--repo", str(repo), "--cloud", chosen, "--compute", model, "--assumed"] if recommendation else
                     [solsarch, "plan", "--repo", str(repo), "--cloud", chosen, "--compute", model])
    if code:
        return {"status": "error", "step": "plan", "detail": out}
    if mode == "full":
        code, out = step([solsarch, "finish", "--repo", str(repo)])
        if code:
            return {"status": "error", "step": "finish", "detail": out}
    else:
        for args in ([solsarch, "evidence", "--repo", str(repo)], [str(HERE / "build_report.py"), "--repo", str(repo)]):
            code, out = step(args)
            if code:
                return {"status": "error", "step": Path(args[0]).stem, "detail": out}
    result = check(repo, mode)
    if recommendation:
        result["recommendation"] = recommendation
    write(repo, result)
    summary = {"status": "done", "mode": mode, "cloud": chosen, "compute": model, "recommendation": recommendation, "analysis_completeness": result["analysis_completeness"]["score"],
               "deployment_readiness": result["deployment_readiness"]["label"], "violations": [v["id"] for v in result["invariants"]["violations"]],
               "readiness_file": str(Path(repo) / "docs" / "solsarch-readiness.md"), "assumed": result["assumed"]}
    summary["zip"] = bundle(repo) if mode == "phase1" else str(Path(repo) / "docs" / "solsarch-output.zip")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Work the way the SolsArch app does")
    parser.add_argument("action", choices=("stages", "intake", "run", "check"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--mode", choices=MODES, default=os.environ.get("SOLSARCH_MODE") if os.environ.get("SOLSARCH_MODE") in MODES else "phase1")
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--cloud", choices=APP_CLOUDS + ("not_sure",))
    parser.add_argument("--compute", choices=("server", "serverless", "not_sure"))
    args = parser.parse_args(argv)
    if args.action == "stages":
        result, code = {"status": "ok", "mode": args.mode, "stages": stages(args.mode)}, 0
    elif not Path(args.repo).is_dir():
        result, code = {"status": "error", "message": "%s is not a directory" % args.repo}, 2
    elif args.action == "intake":
        result = record_answers(args.repo, args.set) if args.set else intake_view(args.repo)
        code = 0 if result["status"] == "ok" else 2
    elif args.action == "run":
        result = run(args.repo, args.mode, args.cloud, args.compute)
        code = 0 if result["status"] == "done" else 2
    else:
        found = check(args.repo, args.mode)
        if found is None:
            result, code = {"status": "error", "message": "No plan found. Run `app run` first."}, 2
        else:
            result, code = {"status": "written", "file": write(args.repo, found), "analysis_completeness": found["analysis_completeness"]["score"],
                            "deployment_readiness": found["deployment_readiness"]["label"], "violations": [v["id"] for v in found["invariants"]["violations"]]}, 0
    print(json.dumps(result, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
