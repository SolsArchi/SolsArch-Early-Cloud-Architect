#!/usr/bin/env python3
"""The workload as a cloud-neutral specification, before any provider is chosen.

    workload.py --repo REPO            write .solsarch/workload.json and docs/solsarch-workload.md

It states what the application needs in plain terms (compute units, data stores, networking, availability, performance assumptions,
compliance and company constraints), each with the evidence it came from, using only files the earlier steps wrote. Placement rules
then map this to a provider's services; `compare` and `rank` use the same scan. Keeping the need apart from the answer makes it
possible to review "what does this app need" without arguing about which cloud. Standard library only.
"""
import argparse
import json
import sys
from pathlib import Path


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def build(repo):
    state = Path(repo) / ".solsarch"
    scan, design, plan = read(state / "scan.json"), read(state / "design.json"), read(state / "plan.json")
    if not (scan and plan):
        return None
    design = design or {}
    rules = read(state / "rules.json") or {}
    profile = design.get("profile") or plan.get("profile") or {}
    units = [{"name": u.get("name"), "kind": u.get("kind"), "frameworks": u.get("frameworks", []), "languages": u.get("languages", []), "port": u.get("port"),
              "streaming": bool(u.get("streaming")), "scheduled_work": bool(u.get("scheduler")), "needs": sorted(u.get("uses") or []),
              "container": bool(u.get("dockerfile")), "runs_as_root": bool(u.get("runs_as_root")), "evidence": (u.get("evidence") or [])[:4]}
             for u in design.get("units", [])]
    stores = [{"need": d["role"], "category": d["category"], "technology": d.get("tech"), "evidence": d.get("evidence_text")}
              for d in plan.get("decisions", []) if d.get("layer") == "data"]
    capacity = design.get("capacity") or {}
    return {
        "schema": "solsarch.workload/1", "project": plan.get("project"), "archetype": (design.get("archetype") or {}).get("name"),
        "archetype_reasons": (design.get("archetype") or {}).get("reasons", []),
        "compute": units, "data_stores": stores,
        "networking": {"public_entry": any(u["kind"] in ("api", "web", "frontend") for u in units), "streaming": any(u["streaming"] for u in units)},
        "availability": {"high_availability": profile.get("high_availability"), "single_az_ok": bool(design.get("single_az"))},
        "performance": {"basis": capacity.get("source"), "tier": capacity.get("tier"), "peak_rps": capacity.get("peak_rps"), "peak_concurrent": capacity.get("peak_concurrent"),
                        "note": capacity.get("note")},
        "compliance": sorted(profile.get("compliance") or []),
        "budget": {"monthly_budget": profile.get("monthly_budget"), "account_type": profile.get("account_type")},
        "constraints": {"allowed_clouds": (rules.get("constraints") or {}).get("allowed_clouds"), "compliant_clouds": (rules.get("constraints") or {}).get("compliant_clouds"),
                        "active_rules": len([r for r in rules.get("rules", []) if r.get("status") == "active"])},
        "assumptions": [a.get("text", a.get("key")) for a in plan.get("assumptions", [])],
        "scan_signals": scan.get("signals", {}),
    }


def render(data):
    out = ["# Workload", "", "What the application needs, before any cloud is chosen. Each line comes from the scan; assumptions are listed at the end.", "",
           "Archetype: **%s**. %s" % (data["archetype"], " ".join(data["archetype_reasons"])), "", "## Compute", "",
           "| Unit | Kind | Frameworks | Port | Streaming | Scheduled work | Needs | Evidence |", "|---|---|---|---|---|---|---|---|"]
    for u in data["compute"]:
        out.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (u["name"], u["kind"], ", ".join(u["frameworks"]) or "none", u["port"] or "n/a",
                                                                  "yes" if u["streaming"] else "no", "yes" if u["scheduled_work"] else "no", ", ".join(u["needs"]) or "none",
                                                                  ", ".join(u["evidence"]) or "none"))
    out += ["", "## Data stores", ""] + (["| Need | Technology | Evidence |", "|---|---|---|"] + ["| %s | %s | %s |" % (s["need"], s["technology"] or "n/a", s["evidence"] or "default") for s in data["data_stores"]]
                                         if data["data_stores"] else ["None found."])
    perf = data["performance"]
    out += ["", "## Availability, performance and limits", "",
            "- High availability: %s." % (data["availability"]["high_availability"] or "not set"),
            "- Traffic basis: %s, tier %s, peak %s requests per second (%s)." % (perf["basis"], perf["tier"], perf["peak_rps"], (perf["note"] or "").replace(chr(10), " ")[:140]),
            "- Compliance named: %s." % (", ".join(data["compliance"]) or "none"),
            "- Budget: %s; account type %s." % (data["budget"]["monthly_budget"] or "not set", data["budget"]["account_type"]),
            "- Company rules in force: %d; clouds the rules allow: %s." % (data["constraints"]["active_rules"], ", ".join(data["constraints"]["compliant_clouds"] or data["constraints"]["allowed_clouds"] or []) or "no restriction")]
    if data["assumptions"]:
        out += ["", "## Assumptions", ""] + ["- " + a for a in data["assumptions"]]
    return chr(10).join(out) + chr(10)


def write(repo, data):
    state = Path(repo) / ".solsarch"
    state.mkdir(parents=True, exist_ok=True)
    (state / "workload.json").write_text(json.dumps(data, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-workload.md").write_bytes(render(data).encode("utf-8"))
    return str(docs / "solsarch-workload.md")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the cloud-neutral workload specification")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    data = build(args.repo)
    if data is None:
        print(json.dumps({"status": "error", "message": "No scan and plan found. Run them first."}))
        return 2
    print(json.dumps({"status": "written", "file": write(args.repo, data), "units": len(data["compute"]), "data_stores": len(data["data_stores"])}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
