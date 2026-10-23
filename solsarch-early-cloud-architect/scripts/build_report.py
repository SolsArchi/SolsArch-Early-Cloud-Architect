#!/usr/bin/env python3
"""Write the full SolsArch architecture report as one markdown file.

Everything in the report is generated from the scan, the plan, the verification facts and
the saved decisions, so the same inputs give the same report whichever AI model ran the
skill. The verdict is computed from the findings. The report explains every service
choice in terms of the code that caused it, embeds every diagram with an explanation, and
lists which cloud facts were checked on the web and which were not.

Standard library only. Usage:
    python build_report.py --repo REPO [--narrative FILE] [--output FILE]

Exit codes: 0 written, 1 unreadable input or diagram failure, 2 scan or plan missing.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brand  # noqa: E402
import build_diagrams  # noqa: E402
import evidence_bridge  # noqa: E402
import facts as facts_store  # noqa: E402
import memory_harness  # noqa: E402
import render_svg  # noqa: E402

BLOCKING = ("critical", "high")
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
DECISION_ORDER = ("edge", "ingress", "static_site", "compute_web", "compute_api", "compute_app", "compute_worker", "gpu",
                  "database", "database_mysql", "database_mongo", "vector", "cache", "queue_kafka", "queue_rabbitmq",
                  "search", "storage", "network", "egress", "identity", "secrets", "registry", "observability")
NOT_COVERED = (
    "The scanner reads application code, package manifests, Dockerfiles, docker compose, Kubernetes manifests, "
    "Terraform, nginx configs, JSON and YAML policies and .env files. It does not read CloudFormation, CDK or Pulumi "
    "output, live cloud configuration or git history. A clean result means nothing was found in those files, not that "
    "the system is secure."
)


def icon_pictures(plan):
    """The SVG pictures with service icons, as {file name: svg text}. A picture is left out when nothing can be drawn."""
    pictures = {"infrastructure-icons.svg": render_svg.infrastructure(plan), "service-cards.svg": render_svg.service_sheet(plan)}
    return {name: svg for name, svg in pictures.items() if svg}


def cell(value):
    text = "" if value is None else str(value)
    return " ".join(text.replace("|", "\\|").split()) or "-"


def table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(":---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell(c) for c in row) + " |" for row in rows)
    return "\n".join(lines)


def locations(evidence, limit=3):
    parts = []
    for item in evidence[:limit]:
        label = "`%s:%s`" % (item.get("file", "?"), item.get("line", "?"))
        if item.get("test_file"):
            label += " (test file)"
        parts.append(label)
    if len(evidence) > limit:
        parts.append("+%d more" % (len(evidence) - limit))
    return ", ".join(parts) or "-"


def finding_rows(findings):
    ordered = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.get("severity"), 9), f.get("id", "")))
    return [[f.get("id"), f.get("severity"), f.get("confidence", "confirmed"), locations(f.get("evidence", [])),
             f.get("fix")] for f in ordered]


def verdict(findings):
    blockers = [f for f in findings if f.get("severity") in BLOCKING]
    if blockers:
        ids = ", ".join(sorted({f["id"] for f in blockers}))
        return "Not ready for production. %d blocker%s to fix first: %s." % (
            len(blockers), "" if len(blockers) == 1 else "s", ids), len(blockers)
    if findings:
        return "No blockers. %d item%s to fix when possible." % (len(findings), "" if len(findings) == 1 else "s"), 0
    return "No blockers found in the scanned files.", 0


def verification_status(fact, skipped):
    if fact:
        label = {"confirmed": "Verified", "changed": "Changed, review", "unavailable": "Unavailable, review"}[fact["verdict"]]
        return "%s on %s" % (label, fact["date"])
    return "Not verified" + (" (web search skipped)" if skipped else "")


SUMMARY_ORDER = ("compute_web", "compute_api", "compute_app", "compute_worker", "database", "database_mysql",
                 "database_mongo", "vector", "cache", "queue_kafka", "queue_rabbitmq", "search", "storage", "static_site")


def summary_text(plan, findings, facts, ordered):
    shorts = []
    first = [d for k in SUMMARY_ORDER for d in ordered if d["key"] == k]
    for d in first + [d for d in ordered if d not in first]:
        if d["short"] not in shorts:
            shorts.append(d["short"])
    parts = ["%s would run on %s using the %s model." % (plan.get("project") or "This project", plan["cloud_name"],
                                                         plan["compute_label"].lower()),
             "%d services were chosen from what the scan found in the code, including %s." % (
                 len(ordered), ", ".join(shorts[:6]))]
    replaced = next((d for d in ordered if d.get("replaces")), None)
    if replaced:
        parts.append("The biggest change is replacing the %s with %s." % (replaced["replaces"], replaced["service"]))
    blockers = [f for f in findings if f.get("severity") in BLOCKING]
    if blockers:
        parts.append("%d blocker%s must be fixed before production: %s." % (
            len(blockers), "" if len(blockers) == 1 else "s", ", ".join(sorted({f["id"] for f in blockers}))))
    else:
        parts.append("No blockers were found in the scanned files.")
    done = {f["key"] for f in facts["facts"]}
    checked = sum(1 for q in plan["verify_queue"] if q["key"] in done)
    if facts["facts"]:
        parts.append("Service names checked against official vendor pages: %d of %d." % (checked, len(plan["verify_queue"])))
    else:
        parts.append("No service names were checked on the web; treat names and availability as unverified.")
    return " ".join(parts)


def decision_section(number, d, record, fact, skipped, cloud_name):
    out = ["### %d. %s: %s" % (number, d["short"], d["role"]), "",
           "- **Service:** %s (%s)" % (d["service"], cloud_name),
           "- **Why this service:** %s" % d["why"],
           "- **Evidence from your code:** %s (%s)" % (d["evidence_text"], d["confidence"])]
    if d.get("replaces"):
        out.append("- **Replaces:** %s" % d["replaces"])
    for note in d.get("caveats", []):
        out.append("- **Watch out:** %s" % note)
    for alt in d.get("alternatives", []):
        out.append("- **Not chosen:** %s. %s" % (alt["service"], alt["reason"]))
    out.append("- **Web check:** %s%s" % (verification_status(fact, skipped),
                                         (". " + fact["note"]) if fact and fact.get("note") else ""))
    if fact:
        out.append("- **Source:** %s" % fact["url"])
    if record:
        out.append("- **Decision record:** %s, status %s" % (record.get("id"), record.get("status")))
    return "\n".join(out) + "\n"


def build(repo, scan, plan, memory, facts, narrative, output):
    findings = scan.get("findings", [])
    memory = memory or {}
    by_key = {d["key"]: d for d in plan["decisions"]}
    ordered = [by_key[k] for k in DECISION_ORDER if k in by_key]
    records = {r.get("key"): r for r in memory.get("architectural_decisions", []) if r.get("key")}
    fact_by_key = {f["key"]: f for f in facts["facts"]}
    skipped = facts.get("skipped")
    assumed = set(memory.get("assumed_fields") or [])
    verdict_text, blocker_count = verdict(findings)
    name = memory.get("project_name") or plan.get("project") or Path(repo).resolve().name

    out = ["# Cloud architecture report: %s" % name, "",
           "Generated by SolsArch Early Cloud Architect (Lite). Plan fingerprint %s." % plan["fingerprint"],
           "", "## 1. Verdict", "", "**%s**" % verdict_text, "",
           "## 2. Summary", "", summary_text(plan, findings, facts, ordered), ""]
    if narrative.strip():
        out += ["### Notes", "", narrative.strip(), ""]

    out += ["## 3. Your choices", "",
            table(["Choice", "Value", "Status"], [
                ["Cloud", plan["cloud_name"], "assumed, not confirmed" if "primary_cloud" in assumed else "chosen by you"],
                ["Compute model", plan["compute_label"],
                 "assumed, not confirmed" if "compute_model" in assumed else "chosen by you"],
                ["Region", "not chosen", "pick one close to your users"]]), ""]

    out += ["## 4. What the scan found in your code", ""]
    components = scan.get("components", [])
    if components:
        flat_ids = evidence_bridge.flatten(scan)["by_component"]
        out += [table(["Category", "Component", "Evidence", "IDs"],
                      [[c["category"], c["name"], locations(c.get("evidence", [])),
                        ", ".join(flat_ids.get("%s|%s" % (c["category"], c["name"]), [])) or "-"]
                       for c in components]), ""]
    else:
        out += ["No known components were found. Describe the system to continue.", ""]
    layout = scan.get("code_layout", [])
    if layout:
        out += ["Folders with source code:", "",
                table(["Folder", "Languages", "Components found there"],
                      [["(root files)" if e["path"] == "." else e["path"] + "/", ", ".join(e["languages"]),
                        ", ".join(e["components"]) or "-"] for e in layout]), ""]

    out += ["## 5. Why each service was chosen", "",
            "Each service below is tied to something the scan found in your code, or to a design rule that every "
            "deployment needs. The cloud and compute model you chose decide which product is used.", ""]
    for number, d in enumerate(ordered, 1):
        out.append(decision_section(number, d, records.get(d["key"]), fact_by_key.get(d["key"]), skipped,
                                    plan["cloud_name"]))
    if plan.get("external_services"):
        out += ["### Services that stay outside your cloud account", ""]
        for x in plan["external_services"]:
            line = "- **%s** (%s): %s Evidence: %s." % (x["name"], x["role"], x["why"], ", ".join(e["text"] for e in x["evidence"]))
            out.append(line)
        out.append("")

    out += ["## 6. Diagrams", "",
            "Each diagram is followed by what it shows, how to read it, and why the services in it were chosen.", ""]
    diagrams = build_diagrams.build(plan, scan)
    pictures = icon_pictures(plan)
    for image in build_diagrams.icon_images(pictures):
        out += [image, ""]
    if diagrams:
        out += ["\n".join(build_diagrams.diagram_markdown(d, "###").rstrip() + "\n" for d in diagrams)]
    else:
        out += ["No diagrams were drawn because the scan found no application code, data stores or web front end.", ""]

    out += ["## 7. Security gate", "", ]
    headers = ["ID", "Severity", "Confidence", "Where", "Fix"]
    blockers = [f for f in findings if f.get("severity") in BLOCKING]
    others = [f for f in findings if f.get("severity") not in BLOCKING]
    out += ["### Blockers before production", ""]
    out += [table(headers, finding_rows(blockers)), ""] if blockers else ["None found in the scanned files.", ""]
    if others:
        out += ["### Should fix", "", table(headers, finding_rows(others)), ""]
    if scan.get("advisories"):
        out += ["### Advisories", "", table(["ID", "Where", "Note"], [
            [a.get("id"), locations(a.get("evidence", [])), a.get("title") + ". " + a.get("fix", "")]
            for a in scan["advisories"]]), ""]
    out += ["Secret values are never written to this report; only locations are shown.", ""]

    out += ["## 8. Web check of cloud facts", ""]
    if skipped:
        out += ["The web check was skipped: %s (%s)." % (skipped["reason"], skipped["date"]), ""]
    rows = [[q["service"], verification_status(fact_by_key.get(q["key"]), skipped),
             (fact_by_key[q["key"]]["url"] if q["key"] in fact_by_key else "-"),
             (fact_by_key[q["key"]]["note"] or fact_by_key[q["key"]]["quote"][:140]) if q["key"] in fact_by_key else "-"]
            for q in plan["verify_queue"]]
    out += [table(["Service", "Status", "Source", "Note or quote"], rows) if rows else "No services to verify.", "",
            "Only pages on the cloud vendor's own site are accepted as sources. Prices are not quoted here: use the "
            "vendor's pricing page for your region.", ""]

    out += ["## 9. What will drive the bill", "",
            table(["Service", "Cost driver"], [[c["service"], c["driver"]] for c in plan["cost_drivers"]]), "",
            "No dollar estimate is made. Use the vendor's pricing calculator with these drivers.", ""]

    questions = list(plan["open_questions"])
    for f in facts["facts"]:
        if f["verdict"] != "confirmed":
            questions.append("%s: the web check says %s. %s Review before relying on this choice." % (
                f["service"], f["verdict"], f.get("note", "")))
    out += ["## 10. Open questions", ""] + ["- " + q for q in questions] + [""]
    if plan.get("assumptions"):
        out += ["### Assumptions made", ""] + ["- " + a["text"] for a in plan["assumptions"]] + [""]

    stats = scan.get("stats", {})
    out += ["## 11. Coverage and limits", "",
            "- Files scanned: %s%s" % (stats.get("files_scanned", "?"), " (stopped early; only part of the repository was scanned)"
                                      if stats.get("truncated") else ""),
            "- Languages: %s" % (", ".join("%s (%s)" % kv for kv in scan.get("languages", {}).items()) or "none detected"),
            "- %s" % NOT_COVERED,
            "- Static analysis of one repository. Nothing here inspects a live cloud account; the generated drift workflow does that inside "
            "your own pipeline with your own credentials. Costs, infrastructure code and quality checks are in their own files "
            "(solsarch-costs.md, solsarch-iac.md, solsarch-quality.md).",
            "- The same scan, cloud and compute model always produce the same plan, diagrams and tables "
            "(fingerprint %s)." % plan["fingerprint"], ""]
    return "\n".join(out).rstrip() + "\n", blocker_count, verdict_text, diagrams


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the SolsArch architecture report")
    parser.add_argument("--repo", default=".", help="Project root (default: current directory)")
    parser.add_argument("--narrative", help="Optional markdown file with extra notes from the assistant")
    parser.add_argument("--output", help="Report path (default: REPO/docs/solsarch-report.md)")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    output = Path(args.output) if args.output else repo / "docs" / "solsarch-report.md"
    try:
        scan = json.loads((repo / ".solsarch" / "scan.json").read_text(encoding="utf-8"))
        plan = json.loads((repo / ".solsarch" / "plan.json").read_text(encoding="utf-8"))
        if "components" not in scan or "decisions" not in plan:
            raise ValueError("scan or plan has the wrong shape")
    except (OSError, ValueError) as error:
        sys.stdout.write(json.dumps({"status": "error", "message":
                         "Cannot read the scan or plan (%s). Run the scan and plan steps first." % error}) + "\n")
        return 2
    try:
        narrative = Path(args.narrative).read_text(encoding="utf-8") if args.narrative else ""
        memory, warning = memory_harness.load(repo)
        text, blockers, verdict_text, diagrams = build(
            repo, scan, plan, memory, facts_store.load_facts(repo), narrative, output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(text.encode("utf-8"))
        arch = output.parent / "solsarch-architecture.md"
        pictures = icon_pictures(plan)
        arch.write_bytes(build_diagrams.architecture_markdown(
            diagrams, plan, build_diagrams.icon_images(pictures)).encode("utf-8"))
        folder = output.parent / "diagrams"
        folder.mkdir(parents=True, exist_ok=True)
        for old in list(folder.glob("*.mmd")) + list(folder.glob("*.svg")) + list(folder.glob("*.png")):
            old.unlink()
        for d in diagrams:
            (folder / d["file"]).write_bytes((d["mmd"] + "\n").encode("utf-8"))
        (folder / build_diagrams.LOGO_FILE).write_bytes(brand.png_bytes())
        for name, svg in pictures.items():
            (folder / name).write_bytes(svg.encode("utf-8"))
    except build_diagrams.BuildError as error:
        sys.stdout.write(json.dumps({"status": "error", "message": "Diagram failed validation: %s" % error}) + "\n")
        return 1
    except (OSError, UnicodeDecodeError, memory_harness.MemoryError_) as error:
        sys.stdout.write(json.dumps({"status": "error", "message": str(error)}) + "\n")
        return 1
    result = {"status": "written", "report": str(output), "diagrams_file": str(arch), "diagram_count": len(diagrams),
              "blockers": blockers, "verdict": verdict_text, "fingerprint": plan["fingerprint"]}
    if warning:
        result["warning"] = warning
    sys.stdout.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
