#!/usr/bin/env python3
"""Architecture impact analysis: what does a code change do to the architecture?

    impact.py baseline --repo REPO         save the current scan, plan, costs, quality and rules as the baseline
    impact.py check    --repo REPO         scan the code as it is now, plan it the same way, and show what changed

A baseline is the architecture the team agreed on, kept under .solsarch/baseline/ (commit it). `check` re-reads the code with the same
cloud, compute model and profile and compares: services added, removed or changed (each with the file and line that caused it),
workload signals that turned on or off (streaming, scheduled work), what that means for timeouts and compute, and, when the saved
costs and quality belong to the current plan, the monthly cost and score change. It writes docs/solsarch-impact.md, .solsarch/impact.json
and a short pull-request comment. Nothing here touches a cloud account; it compares two readings of files. Standard library only.
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import architect  # noqa: E402
import ast_scanner  # noqa: E402
import recommend  # noqa: E402

BASELINE = ("scan.json", "plan.json", "costs.json", "quality.json", "rules.json")

# Fixed, documented consequences of a workload signal. They mirror the quality checks A1 and H5 and the plan's compute rules.
SIGNAL_NOTES = {
    "streaming": {
        "on": "The code now streams responses (server-sent events, websockets or streamed model output). Every proxy, gateway and load balancer in front "
              "of it needs an idle timeout of at least 120 seconds (quality check H5), and a function platform that cuts or buffers responses is no longer "
              "compatible (check A1). Expect always-on compute instead of per-request functions.",
        "off": "Streaming code is gone, so long idle timeouts and always-on compute for streaming are no longer required.",
    },
    "scheduler": {
        "on": "The code now schedules work (cron, a task queue beat or a timer). Something must run it when no request arrives: an always-on worker or a scheduled job.",
        "off": "Scheduled work is gone, so a dedicated scheduler or worker may no longer be needed.",
    },
}


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def baseline_dir(repo, override=None):
    return Path(override) if override else Path(repo) / ".solsarch" / "baseline"


def save_baseline(repo, override=None):
    state = Path(repo) / ".solsarch"
    target = baseline_dir(repo, override)
    if not (state / "plan.json").exists() or not (state / "scan.json").exists():
        return {"status": "error", "message": "No scan and plan found. Run them (or finish) first, then save the baseline."}
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for name in BASELINE:
        if (state / name).exists():
            shutil.copyfile(state / name, target / name)
            saved.append(name)
    return {"status": "saved", "folder": str(target), "files": saved, "fingerprint": (read(state / "plan.json") or {}).get("fingerprint")}


def _by_key(plan):
    return {d["key"]: d for d in plan.get("decisions", [])}


def diff_decisions(old, new):
    a, b = _by_key(old), _by_key(new)
    added = [{"key": k, "service": b[k]["service"], "role": b[k].get("role"), "evidence": b[k].get("evidence_text"), "why": b[k].get("reason") or b[k].get("why", "")}
             for k in sorted(b) if k not in a]
    removed = [{"key": k, "service": a[k]["service"], "role": a[k].get("role"), "evidence": a[k].get("evidence_text")} for k in sorted(a) if k not in b]
    changed = [{"key": k, "from": a[k]["service"], "to": b[k]["service"], "role": b[k].get("role"), "evidence": b[k].get("evidence_text")}
               for k in sorted(a) if k in b and a[k]["service"] != b[k]["service"]]
    return added, removed, changed


def diff_signals(old_scan, new_scan):
    old, new = old_scan.get("signals", {}), new_scan.get("signals", {})
    out = []
    for key in sorted(set(old) | set(new)):
        was, now = old.get(key, "none"), new.get(key, "none")
        if (was == "none") != (now == "none"):
            state = "on" if now != "none" else "off"
            evidence = [("%s:%s" % (e.get("file"), e.get("line"))) for e in (new_scan.get("signal_evidence", {}).get(key) or [])
                        if not e.get("test_file")][:3]
            out.append({"signal": key, "state": state, "evidence": evidence, "meaning": SIGNAL_NOTES.get(key, {}).get(state, "")})
    return out


def diff_findings(old_scan, new_scan):
    def keys(scan):
        return {(f["id"], f.get("title", "")): f for f in scan.get("findings", [])}
    a, b = keys(old_scan), keys(new_scan)
    return ([{"id": k[0], "title": k[1], "severity": b[k].get("severity")} for k in sorted(b) if k not in a],
            [{"id": k[0], "title": k[1]} for k in sorted(a) if k not in b])


def compute(repo, override=None):
    base = baseline_dir(repo, override)
    old_plan, old_scan = read(base / "plan.json"), read(base / "scan.json")
    if not old_plan or not old_scan:
        return {"status": "error", "message": "No baseline found. Run `baseline` after the architecture is agreed, and commit .solsarch/baseline/."}
    root = Path(repo)
    new_scan = ast_scanner.scan(root)
    new_plan = recommend.build_plan(new_scan, recommend.load_catalog(), old_plan["cloud"], old_plan["compute_model"], tuple(old_plan.get("overlays") or ()),
                                    old_plan.get("profile") or architect.load_profile(repo))
    added, removed, changed = diff_decisions(old_plan, new_plan)
    signals = diff_signals(old_scan, new_scan)
    new_findings, fixed_findings = diff_findings(old_scan, new_scan)
    result = {"status": "ok", "schema": "solsarch.impact/1", "cloud": old_plan["cloud"], "compute_model": old_plan["compute_model"],
              "baseline_fingerprint": old_plan.get("fingerprint"), "current_fingerprint": new_plan.get("fingerprint"),
              "services_added": added, "services_removed": removed, "services_changed": changed, "signals": signals,
              "new_findings": new_findings, "fixed_findings": fixed_findings}
    result["changed"] = bool(added or removed or changed or signals or new_findings or fixed_findings)
    # Cost and score deltas only when the saved files belong to the plan just computed; otherwise say what to run.
    state = Path(repo) / ".solsarch"
    costs, old_costs = read(state / "costs.json"), read(base / "costs.json")
    quality, old_quality = read(state / "quality.json"), read(base / "quality.json")
    current = new_plan.get("fingerprint")
    if costs and old_costs and (read(state / "plan.json") or {}).get("fingerprint") == current:
        result["monthly_cost"] = {"before": old_costs["totals"]["monthly"], "after": costs["totals"]["monthly"],
                                  "delta": round(costs["totals"]["monthly"] - old_costs["totals"]["monthly"], 2),
                                  "note": "Priced lines only; unpriced lines (%d now, %d before) are not in either number." % (costs["totals"]["unpriced_lines"], old_costs["totals"]["unpriced_lines"])}
    else:
        result["monthly_cost"] = None
    if quality and old_quality and quality.get("plan_fingerprint") == current:
        result["quality_score"] = {"before": old_quality["score"], "after": quality["score"], "delta": quality["score"] - old_quality["score"]}
    else:
        result["quality_score"] = None
    if result["changed"] and (result["monthly_cost"] is None or result["quality_score"] is None):
        result["next"] = "Run `plan` and `finish` on the new code to add the monthly cost and quality score change."
    return result


def render(result):
    if result["status"] != "ok":
        return "# Architecture impact\n\n%s\n" % result["message"]
    out = ["# Architecture impact", "", "Cloud: %s, compute: %s. Baseline `%s` against the code as it is now (`%s`)." % (
        result["cloud"], result["compute_model"], result["baseline_fingerprint"], result["current_fingerprint"]), ""]
    if not result["changed"]:
        return chr(10).join(out + ["**No architecture change.** The code still needs the same services, so nothing to review.", ""])
    out += ["**The architecture changes.** What follows is each change with the code that caused it.", ""]
    for item in result["signals"]:
        out += ["## Workload: %s turned %s" % (item["signal"], item["state"]), "", item["meaning"] or "This changes how the app runs.",
                "Evidence: %s" % (", ".join("`%s`" % e for e in item["evidence"]) or "none in non-test code"), ""]
    if result["services_added"]:
        out += ["## Services added", "", "| Service | For | Caused by |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (s["service"], s["role"], s["evidence"] or "default rule") for s in result["services_added"]] + [""]
    if result["services_changed"]:
        out += ["## Services changed", "", "| Was | Now | For | Caused by |", "|---|---|---|---|"]
        out += ["| %s | %s | %s | %s |" % (s["from"], s["to"], s["role"], s["evidence"] or "default rule") for s in result["services_changed"]] + [""]
    if result["services_removed"]:
        out += ["## Services no longer needed", "", "| Service | For | Was caused by |", "|---|---|---|"]
        out += ["| %s | %s | %s |" % (s["service"], s["role"], s["evidence"] or "default rule") for s in result["services_removed"]] + [""]
    if result["new_findings"] or result["fixed_findings"]:
        out += ["## Security and design findings", ""]
        out += ["- New: %s %s (%s)" % (f["id"], f["title"], f["severity"]) for f in result["new_findings"]]
        out += ["- Gone: %s %s" % (f["id"], f["title"]) for f in result["fixed_findings"]] + [""]
    out += ["## Cost and score", ""]
    cost, score = result["monthly_cost"], result["quality_score"]
    out.append("- Monthly cost: $%.2f to $%.2f (%+.2f). %s" % (cost["before"], cost["after"], cost["delta"], cost["note"]) if cost else "- Monthly cost: not compared. " + result.get("next", ""))
    out.append("- Quality score: %s to %s (%+d)." % (score["before"], score["after"], score["delta"]) if score else "- Quality score: not compared.")
    out += ["", "Compared from files only. No cloud account was read; real traffic and bills may differ.", ""]
    return chr(10).join(out)


def pr_comment(result):
    if result["status"] != "ok":
        return result["message"]
    if not result["changed"]:
        return "**SolsArch**: no architecture change in this pull request."
    lines = ["**SolsArch: this change alters the architecture.**", ""]
    lines += ["- Workload %s turned %s (%s)" % (s["signal"], s["state"], ", ".join(s["evidence"]) or "no non-test evidence") for s in result["signals"]]
    lines += ["- Adds %s for %s" % (s["service"], s["role"]) for s in result["services_added"]]
    lines += ["- Changes %s to %s" % (s["from"], s["to"]) for s in result["services_changed"]]
    lines += ["- Drops %s" % s["service"] for s in result["services_removed"]]
    if result["monthly_cost"]:
        lines.append("- Monthly cost %+.2f" % result["monthly_cost"]["delta"])
    lines += ["", "Full detail: `docs/solsarch-impact.md`. Review, then run `baseline` to accept the new architecture."]
    return chr(10).join(lines)


def write(repo, result):
    state = Path(repo) / ".solsarch"
    state.mkdir(parents=True, exist_ok=True)
    (state / "impact.json").write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    (state / "impact-comment.md").write_bytes((pr_comment(result) + chr(10)).encode("utf-8"))
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-impact.md").write_bytes(render(result).encode("utf-8"))
    return str(docs / "solsarch-impact.md")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Architecture impact of a code change")
    parser.add_argument("action", choices=("baseline", "check"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--baseline", help="Baseline folder (default: REPO/.solsarch/baseline)")
    parser.add_argument("--fail-on-change", action="store_true", help="Exit 1 when the architecture changed (for a pull-request check)")
    args = parser.parse_args(argv)
    if args.action == "baseline":
        result = save_baseline(args.repo, args.baseline)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "saved" else 2
    result = compute(args.repo, args.baseline)
    if result["status"] != "ok":
        print(json.dumps(result, indent=2))
        return 2
    path = write(args.repo, result)
    print(json.dumps({"status": "written", "file": path, "changed": result["changed"], "services_added": len(result["services_added"]),
                      "services_removed": len(result["services_removed"]), "services_changed": len(result["services_changed"]),
                      "signals": [s["signal"] + ":" + s["state"] for s in result["signals"]], "comment": str(Path(args.repo) / ".solsarch" / "impact-comment.md")}, indent=2))
    return 1 if (args.fail_on_change and result["changed"]) else 0


if __name__ == "__main__":
    sys.exit(main())
