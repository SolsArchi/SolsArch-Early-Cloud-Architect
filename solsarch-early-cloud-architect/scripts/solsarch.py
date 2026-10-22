#!/usr/bin/env python3
"""Single entry point for the SolsArch Early Cloud Architect workflow.

    solsarch.py questions --repo REPO        the two questions to ask, minus anything already known
    solsarch.py scan --repo REPO             read the code, write .solsarch/scan.json
    solsarch.py plan --repo REPO --cloud aws --compute server [--assumed]
                                             choose services, write .solsarch/plan.json, save decisions
    solsarch.py run --repo REPO [--cloud C --compute M]
                                             the whole workflow in one command (scan, plan, evidence, report, verify, zip)
    solsarch.py guide                        the workflow as JSON, for agents that cannot load a skill
    solsarch.py costs --repo REPO [--refresh] [--offline] [--prices FILE]
                                             live vendor prices -> docs/solsarch-costs.md; `costs add` records a supplied price
    solsarch.py profile --repo REPO [--set traffic_tier=growth --set compliance=hipaa]
                                             optional answers that shape sizing, compliance and the design
    solsarch.py design --repo REPO           the principal-architect review: archetype, units, rules DR-01..DR-11, capacity
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
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ast_scanner  # noqa: E402
import architect  # noqa: E402
import build_report  # noqa: E402
import costs as costs_engine  # noqa: E402
import deploy_guide  # noqa: E402
import eol_check  # noqa: E402
import evidence_bridge  # noqa: E402
import iac as iac_engine  # noqa: E402
import knowledge  # noqa: E402
import mcp_sources  # noqa: E402
import facts as facts_store  # noqa: E402
import memory_harness as mh  # noqa: E402
import package_outputs  # noqa: E402
import providers  # noqa: E402
import recommend  # noqa: E402
import verify_report  # noqa: E402

CLOUD_LABELS = {"aws": "AWS", "azure": "Azure", "gcp": "Google Cloud"}
MAJOR_CLOUDS = tuple(CLOUD_LABELS)
MODE_LABELS = {"server": "Server-based (always-on containers)", "serverless": "Serverless (scales to zero)"}


def emit(payload, code=0):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
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
        reg = providers.registry()
        options = [{"value": k, "label": v, "group": reg[k]["group"]} for k, v in CLOUD_LABELS.items()]
        options += [{"value": pid, "label": reg[pid]["name"], "group": reg[pid]["group"]}
                    for pid in providers.hosting_ids() if pid not in CLOUD_LABELS]
        questions.append({"id": "cloud", "question": "Which cloud or platform do you want to use?", "options": options,
                          "present": "Offer AWS, Azure and Google Cloud first; list the others only if the user wants "
                                     "something else. For a stack that mixes providers, use plan --also."})
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


def split_also(text):
    return [x.strip().lower() for x in (text or "").split(",") if x.strip()]


def cmd_plan(args):
    scan = read_json(state_dir(args.repo) / "scan.json")
    if not scan or "components" not in scan:
        return emit({"status": "error", "message": "No scan found. Run the scan step first."}, 2)
    try:
        plan = recommend.build_plan(scan, recommend.load_catalog(), args.cloud, args.compute, split_also(args.also),
                                    architect.load_profile(args.repo))
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
    memory["overlays"] = plan["overlays"]
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
        "cloud": plan["cloud_name"], "compute_model": plan["compute_label"], "overlays": plan["overlay_names"],
        "not_available_natively": [{"role": g["role"], "message": g["message"]} for g in plan["gaps"]],
        "decisions": [{"key": d["key"], "service": d["service"], "role": d["role"], "confidence": d["confidence"]}
                      for d in plan["decisions"]],
        "to_verify": len(plan["verify_queue"]),
        "next": "Run the web verification: solsarch.py facts queue --repo REPO",
    }
    if warning or sync_warning:
        payload["warning"] = warning or sync_warning
    return emit(payload)


def cmd_profile(args):
    """Record the user's optional answers (traffic tier, account type, compliance, region...) for the design."""
    profile = architect.load_profile(args.repo)
    changed = []
    for item in args.set or []:
        key, sep, value = item.partition("=")
        if not sep:
            return emit({"status": "error", "message": "use KEY=VALUE, for example traffic_tier=growth"}, 2)
        try:
            profile[key.strip()] = architect.validate_profile_value(key.strip(), value)
        except ValueError as error:
            return emit({"status": "error", "message": str(error)}, 2)
        changed.append(key.strip())
    if changed:
        state_dir(args.repo).mkdir(parents=True, exist_ok=True)
        architect.save_profile(args.repo, profile)
    return emit({"status": "updated" if changed else "ok", "changed": changed, "profile": profile,
                 "note": "Optional. Defaults are used for anything not set, and the report labels them as assumptions. "
                         "Run plan again after changing the account type.",
                 "fields": {"traffic_tier": list(architect.TRAFFIC_TIERS), "account_type": list(architect.ACCOUNT_TYPES),
                            "high_availability": list(architect.HA_MODES), "compliance": list(architect.COMPLIANCE_FRAMEWORKS),
                            "risk_appetite": list(architect.RISK_APPETITES),
                            "other": ["region", "monthly_budget", "business_driver", "existing_cicd", "rto_minutes",
                                      "rpo_minutes", "daily_requests", "peak_concurrent_users"]}})


def cmd_design(args):
    scan = read_json(state_dir(args.repo) / "scan.json")
    plan = read_json(state_dir(args.repo) / "plan.json")
    if not scan or not plan:
        return emit({"status": "error", "message": "No scan or plan found. Run the scan and plan steps first."}, 2)
    return emit(dict(architect.review(args.repo, scan, plan, architect.load_profile(args.repo)), status="ok"))


def build_costs(repo, online=True, refresh=False):
    """Write costs.json and the cost report. Never raises: a failure is recorded and the rest of the run continues."""
    plan = read_json(state_dir(repo) / "plan.json")
    design = read_json(state_dir(repo) / "design.json")
    if not plan or not design:
        return {"status": "error", "message": "No plan or design found. Run the scan, plan and evidence steps first."}
    try:
        result = costs_engine.build(plan, design, architect.load_profile(repo), state_dir(repo) / costs_engine.BOOK_FILE,
                                    online=online, refresh=refresh, registry=providers.registry())
    except Exception as error:  # a pricing failure must not stop the architecture work
        result = {"schema": 1, "pricing_status": "error", "error": "%s: %s" % (type(error).__name__, error), "lines": [], "unpriced": [],
                  "totals": {"monthly": 0, "annual": 0, "priced_lines": 0, "unpriced_lines": 0, "live_share": 0.0}}
        write_json(state_dir(repo) / "costs.json", result)
        return {"status": "error", "message": result["error"]}
    write_json(state_dir(repo) / "costs.json", result)
    target = Path(repo) / "docs" / "solsarch-costs.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(costs_engine.render_markdown(result).encode("utf-8"))
    return {"status": "written", "file": str(target), "pricing_status": result["pricing_status"], "region": result["region"],
            "monthly": result["totals"]["monthly"], "priced_lines": result["totals"]["priced_lines"],
            "unpriced_lines": result["totals"]["unpriced_lines"], "live_share": result["totals"]["live_share"],
            "range": result["range"] and {k: result["range"][k] for k in ("p10", "p50", "p90")}}


def build_iac(repo):
    """Write the infrastructure code, its manifest, the capability report and the deploy guide. Never raises."""
    plan = read_json(state_dir(repo) / "plan.json")
    scan = read_json(state_dir(repo) / "scan.json")
    design = read_json(state_dir(repo) / "design.json")
    if not (plan and scan and design):
        return {"status": "error", "message": "No scan, plan and design found. Run the scan, plan and evidence steps first."}
    try:
        profile = architect.load_profile(repo)
        result = iac_engine.build(plan, design, profile, scan, Path(repo).resolve().name)
        out_dir = state_dir(repo) / "iac"
        if out_dir.exists():
            import shutil
            shutil.rmtree(out_dir)
        iac_engine.write(result["files"], out_dir)
        manifest = result["manifest"]
        write_json(state_dir(repo) / "iac-manifest.json", manifest)
        docs = Path(repo) / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / "solsarch-iac.md").write_bytes(iac_engine.render_markdown(manifest).encode("utf-8"))
        guide = deploy_guide.build(plan, design, profile, manifest, result["files"])
        (docs / "solsarch-deploy-guide.md").write_bytes(guide.encode("utf-8"))
    except Exception as error:  # infrastructure code must not stop the architecture work
        return {"status": "error", "message": "%s: %s" % (type(error).__name__, error)}
    p = manifest["parity"]
    return {"status": "written", "folder": str(out_dir), "files": manifest["file_count"], "families": [f["label"] for f in manifest["families"]],
            "capability_score": p["score"], "capability_gaps": p["gaps"], "guide": str(docs / "solsarch-deploy-guide.md"),
            "next_steps": manifest["next_steps"]}


def cmd_validate(args):
    """Run the real tools (terraform fmt/init/validate, TFLint, Trivy, Checkov, kubectl, ansible-lint) over the generated code."""
    root = state_dir(args.repo) / "iac"
    if not root.is_dir():
        built = build_iac(args.repo)
        if built.get("status") != "written":
            return emit(built, 2)
    import toolchain
    result = toolchain.check_all(root, args.engine, not args.no_fix, args.offline)
    write_json(state_dir(args.repo) / "validation.json", result)
    docs = Path(args.repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-validation.md").write_bytes(toolchain.render_markdown(result).encode("utf-8"))
    return emit({"status": result["verdict"], "failed": result["failed"], "security_findings": result["security_findings"],
                 "not_run": result["skipped"], "report": str(docs / "solsarch-validation.md")}, 1 if result["failed"] else 0)


def cmd_iac(args):
    return emit(build_iac(args.repo), 0)


def cmd_deploy_guide(args):
    result = build_iac(args.repo)
    return emit(result, 0 if result.get("status") == "written" else 1)


def cmd_knowledge(args):
    if args.action == "search":
        return emit({"query": args.query, "results": knowledge.search(args.query or "", args.limit)})
    if args.action == "chapters":
        return emit({"chapters": knowledge.chapters()})
    if args.action == "playbooks":
        return emit({"playbooks": knowledge.playbooks()})
    return emit(knowledge.section(args.chapter, args.query) or {"error": "no such chapter"})


def cmd_eol(args):
    return emit(eol_check.check(args.repo))


def cmd_mcp(args):
    if args.action == "tools":
        return emit({"mcp_servers": mcp_sources.TOOLS, "command_line_tools": mcp_sources.CLI_TOOLS})
    if args.action == "discover":
        return emit(mcp_sources.discover(args.provider))
    return emit(mcp_sources.listing(args.provider))


def cmd_costs(args):
    book = state_dir(args.repo) / costs_engine.BOOK_FILE
    if args.action == "add":
        missing = [n for n in ("provider", "meter", "unit_price", "url", "quote") if getattr(args, n) in (None, "")]
        if missing:
            return emit({"status": "error", "message": "costs add needs --%s" % ", --".join(m.replace("_", "-") for m in missing)}, 2)
        try:
            item = costs_engine.add_supplied(book, args.provider, args.meter, args.unit_price, args.unit or "unit", args.url, args.quote,
                                             args.region or "global")
        except costs_engine.ps.PriceError as error:
            return emit({"status": "error", "message": str(error)}, 2)
        return emit({"status": "recorded", "price": item, "next": "Run solsarch.py costs --repo REPO to apply it."})
    if args.prices:
        try:
            added = costs_engine.load_prices_file(book, args.prices)
        except (OSError, ValueError, costs_engine.ps.PriceError) as error:
            return emit({"status": "error", "message": str(error)}, 2)
        emit_note = {"prices_loaded": len(added)}
    else:
        emit_note = {}
    if not (state_dir(args.repo) / "design.json").exists():
        code = build_evidence_safe(args.repo)
        if code:
            return emit({"status": "error", "message": "Run the scan and plan steps first."}, 2)
    result = build_costs(args.repo, online=not args.offline, refresh=args.refresh)
    if result["status"] == "error":
        return emit(result, 1)
    return emit(dict(result, **emit_note))


def build_evidence_safe(repo):
    try:
        build_evidence(repo)
        return 0
    except ValueError:
        return 2


def cmd_facts(args):
    return facts_store.main(args.rest)


GUIDE = {
    "name": "solsarch-early-cloud-architect",
    "summary": "Designs cloud architecture for a local repository: scan, plan, verify, diagrams, report, zip.",
    "run_from": "the folder that holds scripts/solsarch.py; use python, python3 or py",
    "rules": [
        "Ask the user only two questions: which cloud (aws, azure, gcp) and server or serverless.",
        "Never choose, rename or remove services yourself and never edit the generated report or diagrams.",
        "Never print a secret value and never state a price.",
        "If the user cannot be asked, use the defaults and mark the answers as assumed.",
    ],
    "steps": [
        {"id": "questions", "command": "solsarch.py questions --repo REPO",
         "do": "Ask exactly the questions listed in 'ask'. Skip any already answered."},
        {"id": "scan", "command": "solsarch.py scan --repo REPO",
         "do": "Reads the code. If components is empty, ask the user to describe the system and stop."},
        {"id": "compare", "command": "solsarch.py compare --repo REPO --compute server", "optional": True,
         "do": "Only when the user asks which cloud fits best: AWS, Azure and GCP side by side."},
        {"id": "plan", "command": "solsarch.py plan --repo REPO --cloud aws|azure|gcp --compute server|serverless [--assumed]",
         "do": "Chooses services with fixed rules. Show the user the decisions."},
        {"id": "facts", "command": "solsarch.py facts queue --repo REPO", "optional": True,
         "do": "With a web search tool: search each query, then record one official sentence with 'facts add'. "
               "Without one: solsarch.py facts skip --repo REPO --reason \"no web search tool\"."},
        {"id": "finish", "command": "solsarch.py finish --repo REPO",
         "do": "Writes evidence, report, diagrams, runs the verification gate, then the zip. Fix errors, never use --force unless asked."},
        {"id": "tell", "do": "Reply with the verdict, a short summary, any blocker, services marked changed or "
                              "unavailable, assumed answers, and the paths of the zip and the report."},
    ],
    "shortcut": "solsarch.py run --repo REPO --cloud aws --compute server  (does scan, plan, facts skip and finish in one call)",
}


def cmd_guide(args):
    return emit(GUIDE)


def cmd_run(args):
    """Scan, plan, skip the web check and finish in one call. For agents that cannot drive the steps one by one."""
    repo = str(args.repo)
    steps = {}
    code, out = capture(main, ["scan", "--repo", repo])
    steps["scan"] = {"status": out.get("status"), "components": out.get("components", [])}
    if code != 0:
        return emit({"status": "error", "step": "scan", "detail": out}, code)
    if not out.get("components"):
        return emit({"status": "needs_input", "step": "scan", "steps": steps,
                     "message": "No known components were found. Ask the user to describe the system; do not guess services."}, 3)
    assumed = False
    memory, _ = mh.load(repo)
    memory = memory or {}
    cloud = args.cloud or (memory.get("primary_cloud") if "primary_cloud" not in (memory.get("assumed_fields") or []) else None)
    compute = args.compute or (memory.get("compute_model") if "compute_model" not in (memory.get("assumed_fields") or []) else None)
    if not cloud:
        hints = out.get("suggested_cloud")
        cloud, assumed = hints or "aws", True
    if not compute:
        compute, assumed = "server", True
    also = args.also or ",".join(memory.get("overlays") or [])
    plan_args = ["plan", "--repo", repo, "--cloud", cloud, "--compute", compute] + (["--also", also] if also else []) \
        + (["--assumed"] if assumed else [])
    code, plan = capture(main, plan_args)
    steps["plan"] = {"status": plan.get("status"), "cloud": cloud, "compute": compute, "assumed": assumed}
    if code != 0:
        return emit({"status": "error", "step": "plan", "detail": plan}, code)
    data = facts_store.load_facts(repo)
    if not data.get("facts") and not data.get("skipped"):
        capture(facts_store.main, ["skip", "--repo", repo, "--reason", "web verification not performed by the run command"])
        steps["facts"] = "skipped: no web search was done; the report says services are not verified"
    finish_args = ["finish", "--repo", repo] + (["--icon-dir", args.icon_dir] if args.icon_dir else []) + (["--force"] if args.force else [])
    code, done = capture(main, finish_args)
    if code != 0:
        return emit({"status": "error", "step": "finish", "steps": steps, "detail": done}, code)
    done["steps"] = steps
    done["assumed_answers"] = assumed
    done["next"] = ("If you can search the web, run 'facts queue', record the sources, then 'finish' again. "
                    "If the answers were assumed, ask the user and rerun with --cloud and --compute.")
    return emit(done)


def cmd_compare(args):
    """Place the same scan on all three clouds and write one side-by-side table. Does not change the saved plan."""
    scan = read_json(state_dir(args.repo) / "scan.json")
    if not scan or "components" not in scan:
        return emit({"status": "error", "message": "No scan found. Run the scan step first."}, 2)
    catalog = recommend.load_catalog()
    chosen = providers.hosting_ids() if args.clouds == "all" else split_also(args.clouds) or list(MAJOR_CLOUDS)
    unknown = [c for c in chosen if c not in providers.hosting_ids()]
    if unknown:
        return emit({"status": "error", "message": "unknown provider: %s" % ", ".join(unknown)}, 2)
    plans = {cloud: recommend.build_plan(scan, catalog, cloud, args.compute) for cloud in chosen}
    labels = {cloud: plans[cloud]["cloud_name"] for cloud in chosen}
    order, roles = [], {}
    for plan in plans.values():
        for d in plan["decisions"]:
            if d["key"] not in roles:
                order.append(d["key"])
                roles[d["key"]] = d["role"]
    rows = []
    for key in order:
        cells = []
        for cloud in chosen:
            match = next((d for d in plans[cloud]["decisions"] if d["key"] == key), None)
            gap = next((g for g in plans[cloud]["gaps"] if g["key"] == key), None)
            cells.append(match["service"] if match else ("not native: " + gap["message"] if gap else "not needed"))
        rows.append((roles[key], cells))
    lines = ["# Cloud comparison: %s" % (scan.get("root") or "project"), "",
             "Compute model: %s. The same scan was placed on each cloud with the same fixed rules. "
             "This file compares service choices only; it does not estimate price or performance." % MODE_LABELS[args.compute],
             "", "| Role | " + " | ".join(labels.values()) + " |", "| :--- | " + " | ".join(":---" for _ in chosen) + " |"]
    lines += ["| %s | %s |" % (role, " | ".join(c.replace("|", "/") for c in cells)) for role, cells in rows]
    lines += ["", "To continue with one provider, run: solsarch.py plan --repo REPO --cloud PROVIDER --compute %s" % args.compute, ""]
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
    if plan and plan.get("decisions"):
        design = architect.review(repo, scan, plan, architect.load_profile(repo))
        write_json(state_dir(repo) / "design.json", design)
        summary["design_rules"] = design["rule_counts"]
        summary["archetype"] = design["archetype"]["name"]
        summary["units"] = len(design["units"])
    if plan and plan.get("cloud") and plan.get("compute_model"):
        import icon_diagram
        placements = evidence_bridge.placements_from_plan(plan, evidence)
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
    extras = {}
    costs_result = build_costs(args.repo, online=not os.environ.get("SOLSARCH_OFFLINE"))
    extras["costs"] = costs_result.get("status") if costs_result.get("status") != "error" else "error: " + costs_result.get("message", "")
    iac_result = build_iac(args.repo)
    extras["infrastructure"] = iac_result.get("status") if iac_result.get("status") != "error" else "error: " + iac_result.get("message", "")
    code, package = capture(package_outputs.main, base)
    if code != 0:
        return emit({"status": "error", "step": "zip", "detail": package}, code)
    return emit({
        "status": "done", "verdict": report["verdict"], "blockers": report["blockers"],
        "report": report["report"], "diagrams_file": report["diagrams_file"],
        "diagram_count": report["diagram_count"], "zip": package["zip"], "fingerprint": report["fingerprint"],
        "verification": "failed (forced)" if errors else "passed", "extras": extras,
        "infrastructure_folder": iac_result.get("folder"), "deploy_guide": iac_result.get("guide"),
        "capability_score": iac_result.get("capability_score"), "monthly_cost_estimate": costs_result.get("monthly"),
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
    p = sub.add_parser("run")
    p.add_argument("--repo", default=".")
    p.add_argument("--cloud", choices=providers.hosting_ids())
    p.add_argument("--also", default="", help="Comma-separated overlay providers, for example neon,cloudflare")
    p.add_argument("--compute", choices=("server", "serverless"))
    p.add_argument("--icon-dir")
    p.add_argument("--force", action="store_true")
    sub.add_parser("guide")
    p = sub.add_parser("costs")
    p.add_argument("action", nargs="?", choices=("run", "add"), default="run")
    p.add_argument("--repo", default=".")
    p.add_argument("--refresh", action="store_true", help="Read fresh vendor prices instead of the price book")
    p.add_argument("--offline", action="store_true", help="Use the price book only; never the network")
    p.add_argument("--prices", metavar="FILE", help="JSON file of supplied prices: [{provider, meter, price, unit, source, quote}]")
    p.add_argument("--provider")
    p.add_argument("--meter")
    p.add_argument("--unit-price", type=float)
    p.add_argument("--unit")
    p.add_argument("--url")
    p.add_argument("--quote")
    p.add_argument("--region")
    p = sub.add_parser("profile")
    p.add_argument("--repo", default=".")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="Set a profile field; repeat for several")
    p = sub.add_parser("design")
    p.add_argument("--repo", default=".")
    p = sub.add_parser("iac")
    p.add_argument("--repo", default=".")
    p = sub.add_parser("validate")
    p.add_argument("--repo", default=".")
    p.add_argument("--engine", choices=("auto", "terraform", "tofu"), default="auto")
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--offline", action="store_true")
    p = sub.add_parser("deploy-guide")
    p.add_argument("--repo", default=".")
    p = sub.add_parser("eol")
    p.add_argument("--repo", default=".")
    p = sub.add_parser("knowledge")
    p.add_argument("action", nargs="?", choices=("search", "chapters", "playbooks", "section"), default="search")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--chapter", type=int, default=1)
    p = sub.add_parser("mcp")
    p.add_argument("action", nargs="?", choices=("list", "discover", "tools"), default="list")
    p.add_argument("--provider")
    p = sub.add_parser("compare")
    p.add_argument("--clouds", default="aws,azure,gcp", help="Comma-separated provider ids, or 'all'")
    p.add_argument("--repo", default=".")
    p.add_argument("--compute", choices=("server", "serverless"), default="server")
    p = sub.add_parser("plan")
    p.add_argument("--repo", default=".")
    p.add_argument("--cloud", required=True, choices=providers.hosting_ids())
    p.add_argument("--also", default="", help="Comma-separated overlay providers, for example neon,cloudflare")
    p.add_argument("--compute", required=True, choices=("server", "serverless"))
    p.add_argument("--assumed", action="store_true", help="The user did not choose these; mark them as assumed")
    p = sub.add_parser("facts")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    handler = {"questions": cmd_questions, "scan": cmd_scan, "plan": cmd_plan, "facts": cmd_facts,
               "finish": cmd_finish, "evidence": cmd_evidence, "verify": cmd_verify, "compare": cmd_compare,
               "run": cmd_run, "guide": cmd_guide, "profile": cmd_profile, "design": cmd_design, "costs": cmd_costs,
               "iac": cmd_iac, "validate": cmd_validate, "deploy-guide": cmd_deploy_guide, "knowledge": cmd_knowledge, "eol": cmd_eol, "mcp": cmd_mcp}
    try:
        return handler[args.command](args)
    except (OSError, mh.MemoryError_) as error:
        return emit({"status": "error", "message": str(error)}, 1)


if __name__ == "__main__":
    sys.exit(main())
