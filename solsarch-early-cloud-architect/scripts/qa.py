#!/usr/bin/env python3
"""The quality assurance stage: run the seven gates, repair what a script can repair safely, and write the quality report.

    qa.py --repo REPO

It is not another model asking whether the design looks right. It runs the deterministic gates in qa_gates.py, and when a gate
fails because an artifact is missing or out of date (the contract, the static check, the cost report, the infrastructure code,
the sourced facts) it asks the step that owns that artifact to run again, then checks again. It does at most three passes, stops
as soon as a pass changes nothing, and writes every change to .solsarch/qa-log.json so nothing flips back and forth.

What it will not do: choose, rename or remove services, edit generated files by hand, or invent a price or a fact.

Outputs: docs/solsarch-quality.md (the report that goes first in the zip), .solsarch/quality.json, .solsarch/qa-log.json, and the
`validation` and `evaluation` parts of .solsarch/contract.json.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import capabilities  # noqa: E402
import contract  # noqa: E402
import qa_gates  # noqa: E402

MAX_PASSES = 3
LIMITS = ("No real deployment is run, so 'deploy ready' means no known blocker was found by these checks.",
          "A score from an outside reviewer may differ. This report gives the evidence and lists what could not be verified.",
          "Prices and limits are read from vendor pages on the dates shown and can change after that.")


def result_has_validation(repo):
    return (Path(repo) / ".solsarch" / "validation.json").exists()


def plan_actions(result, repo, fixers):
    """[(name, reason)] for the artifacts a script can regenerate, judged from the gate results. Order matters."""
    actions = []
    checks = {c["id"]: c for g in result["gates"] for c in g["checks"]}

    def failing(cid):
        return checks.get(cid, {}).get("status") in (qa_gates.FAIL, qa_gates.NOT_VERIFIED)

    if "costs" in fixers and (failing("C1") or checks.get("C2", {}).get("status") == qa_gates.NOT_VERIFIED):
        actions.append(("costs", "the cost report is missing or does not cover every generated resource"))
    if "iac" in fixers and any(checks.get(c, {}).get("status") == qa_gates.FAIL for c in ("X2", "K1", "K2", "X1")):
        actions.append(("iac", "the generated code was built from an older plan or contradicts it"))
    if "capabilities" in fixers and checks.get("X5", {}).get("status") in (qa_gates.WARNING, qa_gates.NOT_VERIFIED):
        actions.append(("capabilities", "a limit or free allowance is stale or not yet read from its official page"))
    if "validate" in fixers and checks.get("I1", {}).get("status") in (qa_gates.WARNING, qa_gates.NOT_VERIFIED) and not (result_has_validation(repo)):
        actions.append(("validate", "terraform and the other real validators are installed but have not been run on this code"))
    if "static" in fixers and checks.get("I1", {}).get("status") in (qa_gates.NOT_VERIFIED,):
        actions.append(("static", "no check of the generated code has run"))
    return actions


def run(repo, fixers=None, max_passes=MAX_PASSES):
    """Evaluate, repair, evaluate again. Returns the final gate result with the log of what changed."""
    fixers = fixers or {}
    log = []
    done = set()
    result = qa_gates.evaluate(repo)
    if result["verdict"] != "ok":
        return result
    for n in range(1, max_passes + 1):
        before = result["score"]
        actions = [a for a in plan_actions(result, repo, fixers) if a[0] not in done]
        if not actions:
            break
        for name, reason in actions:
            try:
                outcome = fixers[name]()
            except Exception as error:  # a repair that fails is recorded and not retried
                outcome = "error: %s: %s" % (type(error).__name__, error)
            done.add(name)
            contract.write(repo)
            log.append({"pass": n, "step": name, "reason": reason, "outcome": str(outcome)[:200]})
        result = qa_gates.evaluate(repo)
        log[-1]["score_before"], log[-1]["score_after"] = before, result["score"]
        if result["score"] == before and not any(a[0] not in done for a in plan_actions(result, repo, fixers)):
            break
    result["qa_log"] = log
    return result


def corrections(result):
    """What is left for a person to do, from the gate results, most important first."""
    todo = []
    for b in result["blockers"]:
        todo.append("Blocker %s: %s" % (b["id"], b["detail"]))
    for u in result["unmet"]:
        todo.append("To lift the cap: %s." % u["name"])
    for g in result["gates"]:
        for c in g["checks"]:
            if c["status"] in (qa_gates.FAIL, qa_gates.NOT_VERIFIED, qa_gates.WARNING):
                todo.append("%s %s (%s): %s" % (c["id"], c["name"], c["status"].replace("_", " ").lower(), c["detail"]))
    seen, out = set(), []
    for t in todo:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


ICON = {"PASS": "PASS", "FAIL": "FAIL", "WARNING": "WARNING", "NOT_VERIFIED": "NOT VERIFIED", "N/A": "n/a"}


def render(result, repo):
    caps = capabilities.relevant(capabilities.load(repo), json.loads((Path(repo) / ".solsarch" / "plan.json").read_text(encoding="utf-8")),
                                 (json.loads((Path(repo) / ".solsarch" / "scan.json").read_text(encoding="utf-8")) or {}).get("signals"))
    out = ["# Architecture quality report", "",
           "Plan `%s`. This report scores the generated architecture with fixed, repeatable checks. It uses no language model to judge the result." % result["plan_fingerprint"], "",
           "## Result", "",
           "| Measure | Value |", "| :--- | :--- |",
           "| Score | **%d out of 100** (%s) |" % (result["score"], result["band"]),
           "| What that means | %s |" % result["advice"],
           "| Deploy ready | %s |" % ("yes, no known blocker" if result["deploy_ready"] else "no"),
           "| Blocking findings | %d |" % len(result["blockers"]),
           "| Contradictions between plan, code and costs | %d |" % len(result["contradictions"])]
    if result["capped"]:
        out.append("| Score cap | The raw score was %d. It is capped at %d until every acceptance criterion below is met. |" % (result["raw_score"], result["cap"]))
    out += ["", "A score above 85 does not mean the plan is safe to deploy. Any blocking finding stops deployment whatever the number.", ""]
    out += ["## Score by area", "", "| Area | Points | Of | What cost points |", "| :--- | ---: | ---: | :--- |"]
    for a in result["areas"]:
        lost = "; ".join("%s %s" % (l["id"], ICON[l["status"]].lower()) for l in a["lost"]) or "nothing"
        out.append("| %s | %s | %d | %s |" % (a["label"], a["earned"], a["weight"], lost))
    out += ["", "## Acceptance criteria for a score above 85", "", "| Criterion | Met |", "| :--- | :--- |"]
    out += ["| %s | %s |" % (a["name"], "yes" if a["met"] else "**no**") for a in result["acceptance"]]
    out += ["", "## The seven gates", ""]
    for g in result["gates"]:
        out += ["### Gate %d: %s - %s" % (g["gate"], g["name"], ICON[g["status"]]), "", "| Check | Result | Detail |", "| :--- | :--- | :--- |"]
        for c in g["checks"]:
            out.append("| %s %s | %s | %s |" % (c["id"], c["name"], ICON[c["status"]], c["detail"].replace("|", "/")))
        out.append("")
    if result.get("qa_log"):
        out += ["## What the quality stage changed", "", "| Pass | Step run again | Why | Score before | after |", "| ---: | :--- | :--- | ---: | ---: |"]
        out += ["| %d | %s | %s | %s | %s |" % (e["pass"], e["step"], e["reason"], e.get("score_before", ""), e.get("score_after", "")) for e in result["qa_log"]]
        out.append("")
    todo = corrections(result)
    if todo:
        out += ["## What is left to do", ""] + ["- %s" % t for t in todo] + [""]
    if result["not_verified"]:
        out += ["## Not verified", "", "These checks could not be run or confirmed. They earn no points and are not reported as passed.", ""]
        out += ["- %s %s: %s" % (n["id"], n["name"], n["detail"]) for n in result["not_verified"]] + [""]
    if caps:
        out += ["## Facts this plan relies on", "", "Each limit or allowance below comes from an official page. The quote is the sentence the value rests on.", "",
                "| Fact | Value | Status | Read on | Source | Quote |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
        for rid in sorted(caps):
            r = caps[rid]
            out.append("| %s | %s %s | %s | %s | %s | %s |" % (r["topic"], r["value"], r["unit"], capabilities.status(r), r.get("retrieved") or "never", r["source"],
                                                              (r.get("quote") or "not read yet").replace("|", "/")[:160]))
        out.append("")
    out += ["## Limits of this report", ""] + ["- %s" % l for l in LIMITS] + [""]
    return chr(10).join(out)


def write(repo, result):
    state = Path(repo) / ".solsarch"
    state.mkdir(parents=True, exist_ok=True)
    (state / "quality.json").write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    (state / "qa-log.json").write_text(json.dumps(result.get("qa_log", []), indent=2) + chr(10), encoding="utf-8", newline=chr(10))
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-quality.md").write_bytes(render(result, repo).encode("utf-8"))
    path = state / "contract.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    gates = {g["gate"]: [{"id": c["id"], "status": c["status"], "detail": c["detail"]} for c in g["checks"]] for g in result["gates"]}
    data["validation"] = {"compatibility": gates[2], "infrastructure": gates[4], "security": gates[6], "cost": gates[5], "consistency": gates[7]}
    data["evaluation"] = {"score": result["score"], "critical_findings": [b["detail"] for b in result["blockers"]],
                          "warnings": [c["id"] for g in result["gates"] for c in g["checks"] if c["status"] == qa_gates.WARNING],
                          "verified": not result["not_verified"], "deploy_ready": result["deploy_ready"]}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the quality gates, repair what a script can, and write the quality report")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    result = run(args.repo)
    if result["verdict"] != "ok":
        print(json.dumps(result))
        return 1
    write(args.repo, result)
    print(json.dumps({"status": "written", "file": str(Path(args.repo) / "docs" / "solsarch-quality.md"), "score": result["score"], "band": result["band"],
                      "capped": result["capped"], "deploy_ready": result["deploy_ready"], "blockers": len(result["blockers"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
