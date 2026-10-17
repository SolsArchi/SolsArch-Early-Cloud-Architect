#!/usr/bin/env python3
"""Single entry point for the SolsArch Early Cloud Architect workflow.

    solsarch.py questions --repo REPO        the two questions to ask, minus anything already known
    solsarch.py scan --repo REPO             read the code, write .solsarch/scan.json
    solsarch.py plan --repo REPO --cloud aws --compute server [--assumed]
                                             choose services, write .solsarch/plan.json, save decisions
    solsarch.py compare --repo REPO [--compute server|serverless]
                                             the same scan placed on AWS, Azure and GCP side by side (no plan is saved)
    solsarch.py facts queue|add|skip|list --repo REPO ...
                                             web verification of the chosen services (see facts.py)
    solsarch.py evidence --repo REPO [--icon-dir D]
                                             number the evidence (E001...), place services, draw the icon picture
    solsarch.py verify --repo REPO           check the report against the evidence (hallucinated ids, secrets, diagrams)
    solsarch.py finish --repo REPO [--icon-dir D] [--force]
                                             evidence, report, diagrams, verification, then the output zip

Every command prints JSON. Standard library only. Exit codes: 0 ok, 1 failed, 2 earlier step missing.
"""

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ast_scanner  # noqa: E402
import build_report  # noqa: E402
import evidence_bridge  # noqa: E402
import facts as facts_store  # noqa: E402
import memory_harness as mh  # noqa: E402
import package_outputs  # noqa: E402
import recommend  # noqa: E402
import verify_report  # noqa: E402

CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud"}
MODE_LABELS = {"server": "Server-based (always-on containers)", "serverless": "Serverless (scales to zero)"}


def emit(payload, code=0):
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return code


def state_dir(repo):
    return Path(repo) / ".solsarch"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def capture(function, argv):
    """Run another script's main() and return (exit code, its parsed JSON output)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = function(argv)
    try:
        return code, json.loads(buffer.getvalue())
    except ValueError:
        return code, {"raw": buffer.getvalue()}


def cmd_questions(args):
    memory, warning = mh.load(args.repo)
    memory = memory or {}
    assumed = set(memory.get("assumed_fields") or [])
    known = {}
    if memory.get("primary_cloud") and "primary_cloud" not in assumed:
        known["cloud"] = memory["primary_cloud"]
    if memory.get("compute_model") and "compute_model" not in assumed:
        known["compute"] = memory["compute_model"]
    scan = read_json(state_dir(args.repo) / "scan.json") or {}
    hints = scan.get("provider_hints", {})
    suggested = max(hints, key=lambda k: (hints[k], k == "aws")) if hints and max(hints.values()) > 0 else None
    questions = []
    if "cloud" not in known:
        questions.append({"id": "cloud", "question": "Which cloud do you want to use?",
                          "options": [{"value": k, "label": v} for k, v in CLOUD_LABELS.items()]})
    if "compute" not in known:
        questions.append({"id": "compute", "question": "Should the app run on servers or serverless?",
                          "options": [{"value": k, "label": v} for k, v in MODE_LABELS.items()]})
    payload = {"status": "ok", "ask": questions, "already_known": known,
               "if_user_cannot_answer": {"cloud": suggested or "aws", "compute": "server", "mark_as": "assumed"},
               "rule": "Ask only these questions. Do not ask about region, budget or compliance."}
    if warning:
        payload["warning"] = warning
    return emit(payload)


def cmd_scan(args):
    root = Path(args.repo)
    if not root.is_dir():
        return emit({"status": "error", "message": "%s is not a directory" % args.repo}, 2)
    result = ast_scanner.scan(root)
    target = state_dir(root) / "scan.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((json.dumps(result, indent=2) + "\n").encode("utf-8"))
    hints = result["provider_hints"]
    return emit({
        "status": "written", "file": str(target), "files_scanned": result["stats"]["files_scanned"],
        "truncated": result["stats"]["truncated"], "components": [c["name"] for c in result["components"]],
        "findings": [{"id": f["id"], "severity": f["severity"], "confidence": f["confidence"]} for f in result["findings"]],
        "suggested_cloud": max(hints, key=lambda k: (hints[k], k == "aws")) if hints and max(hints.values()) > 0 else None,
    })


def cmd_plan(args):
    scan = read_json(state_dir(args.repo) / "scan.json")
    if not scan or "components" not in scan:
        return emit({"status": "error", "message": "No scan found. Run the scan step first."}, 2)
    try:
        plan = recommend.build_plan(scan, recommend.load_catalog(), args.cloud, args.compute)
    except ValueError as error:
        return emit({"status": "error", "message": str(error)}, 1)
    target = state_dir(args.repo) / "plan.json"
    target.write_bytes((json.dumps(plan, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))

    # Drop web checks recorded for a service the new plan no longer uses.
    data = facts_store.load_facts(args.repo)
    wanted = {k: q["service"] for q in plan["verify_queue"] for k in q["keys"]}
    kept = [f for f in data["facts"] if wanted.get(f["key"]) == f["service"]]
    if len(kept) != len(data["facts"]):
        data["facts"] = kept
        facts_store.save_facts(args.repo, data)

    memory, warning = mh.load(args.repo)
    memory = memory or mh.empty_memory()
    assumed = set(memory.get("assumed_fields") or [])
    memory["primary_cloud"], memory["compute_model"] = args.cloud, args.compute
    if args.assumed:
        assumed.update(("primary_cloud", "compute_model"))
    else:
        assumed.difference_update(("primary_cloud", "compute_model"))
    memory["assumed_fields"] = sorted(assumed)
    stamp = mh.now_iso()
    memory["last_analyzed"] = stamp
    memory["last_scan"] = dict(mh.scan_summary(scan), at=stamp)
    mh.save(args.repo, memory)
    sync_warning = mh.sync_plan(args.repo, plan)
    payload = {
        "status": "written", "file": str(target), "fingerprint": plan["fingerprint"],
        "cloud": plan["cloud_name"], "compute_model": plan["compute_label"],
        "decisions": [{"key": d["key"], "service": d["service"], "role": d["role"], "confidence": d["confidence"]}
                      for d in plan["decisions"]],
        "to_verify": len(plan["verify_queue"]),
        "next": "Run the web verification: solsarch.py facts queue --repo REPO",
    }
    if warning or sync_warning:
        payload["warning"] = warning or sync_warning
    return emit(payload)


def cmd_facts(args):
    return facts_store.main(args.rest)


def cmd_compare(args):
    """Place the same scan on all three clouds and write one side-by-side table. Does not change the saved plan."""
    scan = read_json(state_dir(args.repo) / "scan.json")
    if not scan or "components" not in scan:
        return emit({"status": "error", "message": "No scan found. Run the scan step first."}, 2)
    catalog = recommend.load_catalog()
    plans = {cloud: recommend.build_plan(scan, catalog, cloud, args.compute) for cloud in CLOUD_LABELS}
    order, roles = [], {}
    for plan in plans.values():
        for d in plan["decisions"]:
            if d["key"] not in roles:
                order.append(d["key"])
                roles[d["key"]] = d["role"]
    rows = []
    for key in order:
        cells = []
        for cloud in CLOUD_LABELS:
            match = next((d for d in plans[cloud]["decisions"] if d["key"] == key), None)
            cells.append(match["service"] if match else "not needed")
        rows.append((roles[key], cells))
    lines = ["# Cloud comparison: %s" % (scan.get("root") or "project"), "",
             "Compute model: %s. The same scan was placed on each cloud with the same fixed rules. "
             "This file compares service choices only; it does not estimate price or performance." % MODE_LABELS[args.compute],
             "", "| Role | " + " | ".join(CLOUD_LABELS.values()) + " |", "| :--- | :--- | :--- | :--- |"]
    lines += ["| %s | %s |" % (role, " | ".join(c.replace("|", "/") for c in cells)) for role, cells in rows]
    lines += ["", "To continue with one cloud, run: solsarch.py plan --repo REPO --cloud aws|azure|gcp --compute %s" % args.compute, ""]
    target = Path(args.repo) / "docs" / "solsarch-cloud-comparison.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(chr(10).join(lines).encode("utf-8"))
    return emit({"status": "written", "file": str(target), "roles": len(rows),
                 "fingerprints": {c: p["fingerprint"] for c, p in plans.items()}})


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(payload, indent=2, ensure_ascii=True) + chr(10)).encode("utf-8"))


def build_evidence(repo, icon_dir=None):
    """Write evidence.json, placements.json and the icon picture. Returns a summary dict."""
    scan = read_json(state_dir(repo) / "scan.json")
    plan = read_json(state_dir(repo) / "plan.json")
    if not scan or "components" not in scan:
        raise ValueError("No scan found. Run the scan and plan steps first.")
    evidence = evidence_bridge.flatten(scan)
    write_json(state_dir(repo) / "evidence.json", evidence)
    summary = {"evidence_count": len(evidence["evidence"])}
    if plan and plan.get("cloud") and plan.get("compute_model"):
        import icon_diagram
        import place_services
        rules = read_json(Path(place_services.DEFAULT_RULES))
        placements = place_services.place(evidence, rules, plan["cloud"], plan["compute_model"])
        write_json(state_dir(repo) / "placements.json", placements)
        summary["placements"] = len(placements["placements"])
        try:
            svg = icon_diagram.render(placements, evidence, icon_dir)
            (state_dir(repo) / "architecture-icons.svg").write_bytes(svg.encode("utf-8"))
            summary["icon_diagram"] = True
        except (ValueError, KeyError, OSError) as error:
            summary["icon_diagram"] = "skipped: %s" % error
    return summary


def cmd_evidence(args):
    try:
        return emit(dict(status="written", **build_evidence(args.repo, args.icon_dir)))
    except ValueError as error:
        return emit({"status": "error", "message": str(error)}, 2)


def run_verify(repo):
    report_path = Path(repo) / "docs" / "solsarch-report.md"
    evidence = read_json(state_dir(repo) / "evidence.json")
    if not report_path.is_file() or evidence is None:
        return None
    text = report_path.read_text(encoding="utf-8")
    architecture = Path(repo) / "docs" / "solsarch-architecture.md"
    if architecture.is_file():
        text += chr(10) + architecture.read_text(encoding="utf-8")
    return verify_report.check(text, evidence)


def cmd_verify(args):
    problems = run_verify(args.repo)
    if problems is None:
        return emit({"status": "error", "message": "Report or evidence missing. Run the evidence and finish steps first."}, 2)
    errors = [p for p in problems if p["severity"] == "error"]
    return emit({"status": "failed" if errors else "ok", "errors": errors,
                 "warnings": [p for p in problems if p["severity"] != "error"]}, 1 if errors else 0)


def cmd_finish(args):
    base = ["--repo", str(args.repo)]
    try:
        build_evidence(args.repo, args.icon_dir)
    except ValueError as error:
        return emit({"status": "error", "step": "evidence", "detail": str(error)}, 2)
    code, report = capture(build_report.main, base + (["--narrative", args.narrative] if args.narrative else []))
    if code != 0:
        return emit({"status": "error", "step": "report", "detail": report}, code)
    problems = run_verify(args.repo) or []
    errors = [p for p in problems if p["severity"] == "error"]
    if errors and not args.force:
        return emit({"status": "error", "step": "verify", "detail": errors,
                     "hint": "Fix the cause and run finish again, or pass --force to package anyway."}, 1)
    code, package = capture(package_outputs.main, base)
    if code != 0:
        return emit({"status": "error", "step": "zip", "detail": package}, code)
    return emit({
        "status": "done", "verdict": report["verdict"], "blockers": report["blockers"],
        "report": report["report"], "diagrams_file": report["diagrams_file"],
        "diagram_count": report["diagram_count"], "zip": package["zip"], "fingerprint": report["fingerprint"],
        "verification": "failed (forced)" if errors else "passed",
        "warnings": [p["message"] for p in problems if p["severity"] == "warning"],
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description="SolsArch Early Cloud Architect workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("questions", "scan", "finish", "evidence", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        if name in ("finish", "evidence"):
            p.add_argument("--icon-dir", help="Folder of official vendor icon SVGs to use in the icon picture")
        if name == "finish":
            p.add_argument("--narrative", help="Optional markdown notes to include in the report")
            p.add_argument("--force", action="store_true", help="Package even if verification found errors")
    p = sub.add_parser("compare")
    p.add_argument("--repo", default=".")
    p.add_argument("--compute", choices=("server", "serverless"), default="server")
    p = sub.add_parser("plan")
    p.add_argument("--repo", default=".")
    p.add_argument("--cloud", required=True, choices=("aws", "azure", "gcp"))
    p.add_argument("--compute", required=True, choices=("server", "serverless"))
    p.add_argument("--assumed", action="store_true", help="The user did not choose these; mark them as assumed")
    p = sub.add_parser("facts")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    handler = {"questions": cmd_questions, "scan": cmd_scan, "plan": cmd_plan, "facts": cmd_facts,
               "finish": cmd_finish, "evidence": cmd_evidence, "verify": cmd_verify, "compare": cmd_compare}
    try:
        return handler[args.command](args)
    except (OSError, mh.MemoryError_) as error:
        return emit({"status": "error", "message": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
