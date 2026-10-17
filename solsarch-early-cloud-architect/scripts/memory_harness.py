#!/usr/bin/env python3
"""Hermes memory harness: project memory for SolsArch across AI sessions.

Stores the project's cloud choices, constraints and architecture decision records
(ADRs) in `.solsarch/memory.json` under the project root, so a new session can
pick up where the last one stopped.

Commands (all print one JSON document):
    read                         load memory and a short greeting for the user
    set                          update project name, cloud, region, compliance, budget
    adr                          add or update an architecture decision record
    record-scan --scan-file F    remember the components and findings of a scan
    diff --scan-file F           compare a new scan with the remembered one

Writes are atomic. A corrupt memory file is moved aside, never overwritten.
Standard library only.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MEMORY_DIR = ".solsarch"
MEMORY_FILE = "memory.json"
SCHEMA_VERSION = 1
ADR_STATUSES = ("PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED")
COMPUTE_MODELS = ("server", "serverless")
ADR_ID_RE = re.compile(r"^ADR-(\d{3,})$")


class MemoryError_(Exception):
    """Raised for invalid input; the message is shown to the user."""


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def memory_path(root):
    return Path(root) / MEMORY_DIR / MEMORY_FILE


def empty_memory():
    return {
        "schema_version": SCHEMA_VERSION,
        "project_name": None,
        "primary_cloud": None,
        "compute_model": None,
        "target_region": None,
        "compliance_frameworks": [],
        "budget_ceiling_monthly_usd": None,
        "created_at": now_iso(),
        "last_analyzed": None,
        "architectural_decisions": [],
        "last_scan": None,
        "assumed_fields": [],
    }


def load(root):
    """Return (memory dict or None, warning or None)."""
    path = memory_path(root)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except (OSError, ValueError) as error:
        backup = path.with_name("memory.json.corrupt-" + datetime.now().strftime("%Y%m%d%H%M%S"))
        try:
            os.replace(path, backup)
            return None, "Memory file was unreadable (%s). Moved to %s." % (error, backup.name)
        except OSError as move_error:
            raise MemoryError_("Memory file is unreadable (%s) and could not be moved: %s"
                               % (error, move_error))
    base = empty_memory()
    base.update(data)
    if not isinstance(base.get("architectural_decisions"), list):
        base["architectural_decisions"] = []
    return base, None


def save(root, memory):
    path = memory_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".memory-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(memory, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


def greeting(memory):
    parts = []
    name = memory.get("project_name") or "this project"
    cloud = memory.get("primary_cloud")
    region = memory.get("target_region")
    line = "Welcome back to %s." % name
    if cloud:
        line += " Primary cloud: %s%s%s." % (cloud, " in " + region if region else "",
                                            ", " + memory["compute_model"] if memory.get("compute_model") else "")
    parts.append(line)
    if memory.get("compliance_frameworks"):
        parts.append("Compliance: %s." % ", ".join(memory["compliance_frameworks"]))
    if memory.get("budget_ceiling_monthly_usd") is not None:
        parts.append("Budget ceiling: %s USD per month." % memory["budget_ceiling_monthly_usd"])
    decisions = memory.get("architectural_decisions", [])
    if decisions:
        counts = {}
        for item in decisions:
            counts[item.get("status", "PROPOSED")] = counts.get(item.get("status", "PROPOSED"), 0) + 1
        parts.append("%d recorded decisions (%s)." % (
            len(decisions), ", ".join("%d %s" % (v, k.lower()) for k, v in sorted(counts.items()))))
    if memory.get("assumed_fields"):
        parts.append("Not yet confirmed by the user (assumed): %s." % ", ".join(memory["assumed_fields"]))
    if memory.get("last_analyzed"):
        parts.append("Last analysed %s." % memory["last_analyzed"])
    return " ".join(parts)


def next_adr_id(decisions):
    highest = 0
    for item in decisions:
        match = ADR_ID_RE.match(str(item.get("id", "")))
        if match:
            highest = max(highest, int(match.group(1)))
    return "ADR-%03d" % (highest + 1)


def sync_plan(root, plan):
    """Record the decisions of a plan. Existing records keep their status; new ones start PROPOSED.

    A record whose key is no longer in the plan is removed when still PROPOSED and marked
    SUPERSEDED otherwise. Returns the list of ids that now exist for the plan.
    """
    memory, warning = load(root)
    memory = memory or empty_memory()
    decisions = memory["architectural_decisions"]
    today = now_iso()[:10]
    planned = set()
    for d in plan["decisions"]:
        planned.add(d["key"])
        record = next((r for r in decisions if r.get("key") == d["key"]), None)
        fields = {"component": d["role"], "decision": d["service"], "rationale": d.get("reason", ""),
                  "evidence": d["evidence_text"]}
        if record is None:
            decisions.append(dict(fields, id=next_adr_id(decisions), key=d["key"], timestamp=today, status="PROPOSED"))
        else:
            record.update(fields)
            if record.get("status") == "SUPERSEDED":
                record["status"] = "PROPOSED"
                record.pop("superseded_by", None)
            record["updated"] = today
    kept = []
    for record in decisions:
        key = record.get("key")
        if key and key not in planned:
            if record.get("status") == "PROPOSED":
                continue
            record["status"] = "SUPERSEDED"
            record["superseded_by"] = "plan change"
        kept.append(record)
    memory["architectural_decisions"] = kept
    save(root, memory)
    return warning


def scan_summary(scan):
    components = sorted({"%s:%s" % (c["category"], c["name"]) for c in scan.get("components", [])})
    findings = sorted({f["id"] for f in scan.get("findings", [])})
    return {"components": components, "findings": findings}


def load_scan(path):
    try:
        scan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MemoryError_("Cannot read scan file %s: %s" % (path, error))
    if not isinstance(scan, dict) or "components" not in scan:
        raise MemoryError_("%s is not output of ast_scanner.py" % path)
    return scan


# ---------------------------------------------------------------- commands
def cmd_read(args):
    memory, warning = load(args.path)
    if memory is None:
        result = {"status": "empty", "message": "No SolsArch memory for this project yet.", "data": None}
    else:
        result = {"status": "found", "message": greeting(memory), "data": memory}
    if warning:
        result["warning"] = warning
    return result


def cmd_set(args):
    memory, warning = load(args.path)
    memory = memory or empty_memory()
    changed = []
    if args.project_name is not None:
        memory["project_name"] = args.project_name.strip()
        changed.append("project_name")
    if args.primary_cloud is not None:
        memory["primary_cloud"] = args.primary_cloud.strip().lower()
        changed.append("primary_cloud")
    if args.compute_model is not None:
        if args.compute_model not in COMPUTE_MODELS:
            raise MemoryError_("--compute-model must be one of %s." % ", ".join(COMPUTE_MODELS))
        memory["compute_model"] = args.compute_model
        changed.append("compute_model")
    if args.target_region is not None:
        memory["target_region"] = args.target_region.strip()
        changed.append("target_region")
    if args.compliance is not None:
        memory["compliance_frameworks"] = [c.strip() for c in args.compliance.split(",") if c.strip()]
        changed.append("compliance_frameworks")
    if args.budget is not None:
        if args.budget < 0:
            raise MemoryError_("--budget must not be negative.")
        memory["budget_ceiling_monthly_usd"] = int(args.budget) if args.budget.is_integer() else args.budget
        changed.append("budget_ceiling_monthly_usd")
    if not changed:
        raise MemoryError_("Nothing to set. Pass at least one of --project-name, --primary-cloud, "
                           "--target-region, --compliance, --budget.")
    assumed = set(memory.get("assumed_fields") or [])
    if args.assumed:
        assumed.update(changed)
    else:
        assumed.difference_update(changed)
    memory["assumed_fields"] = sorted(assumed)
    path = save(args.path, memory)
    result = {"status": "saved", "changed": changed, "file": str(path), "data": memory}
    if warning:
        result["warning"] = warning
    return result


def cmd_adr(args):
    memory, warning = load(args.path)
    memory = memory or empty_memory()
    decisions = memory["architectural_decisions"]
    status = args.status.upper() if args.status else None
    if status and status not in ADR_STATUSES:
        raise MemoryError_("--status must be one of %s." % ", ".join(ADR_STATUSES))
    today = now_iso()[:10]
    if args.id:
        if not ADR_ID_RE.match(args.id):
            raise MemoryError_("--id must look like ADR-001.")
        existing = next((d for d in decisions if d.get("id") == args.id), None)
    else:
        existing = None
    if existing is None:
        for field in ("component", "decision"):
            if not getattr(args, field):
                raise MemoryError_("A new decision needs --component and --decision.")
        record = {
            "id": args.id or next_adr_id(decisions), "timestamp": today,
            "component": args.component, "decision": args.decision,
            "status": status or "PROPOSED", "rationale": args.rationale or "",
            "evidence": args.evidence or "",
        }
        decisions.append(record)
        action = "added"
    else:
        record = existing
        for field in ("component", "decision", "rationale", "evidence"):
            value = getattr(args, field)
            if value is not None:
                record[field] = value
        if status:
            record["status"] = status
        record["updated"] = today
        action = "updated"
    if args.supersedes:
        old = next((d for d in decisions if d.get("id") == args.supersedes), None)
        if old is None:
            raise MemoryError_("--supersedes %s does not exist." % args.supersedes)
        if old is record:
            raise MemoryError_("A decision cannot supersede itself.")
        old["status"] = "SUPERSEDED"
        old["superseded_by"] = record["id"]
    path = save(args.path, memory)
    result = {"status": "saved", "action": action, "adr": record, "file": str(path)}
    if warning:
        result["warning"] = warning
    return result


def cmd_record_scan(args):
    scan = load_scan(args.scan_file)
    memory, warning = load(args.path)
    memory = memory or empty_memory()
    stamp = now_iso()
    memory["last_analyzed"] = stamp
    memory["last_scan"] = dict(scan_summary(scan), at=stamp)
    path = save(args.path, memory)
    result = {"status": "saved", "file": str(path), "last_scan": memory["last_scan"]}
    if warning:
        result["warning"] = warning
    return result


def cmd_diff(args):
    scan = load_scan(args.scan_file)
    memory, warning = load(args.path)
    previous = (memory or {}).get("last_scan")
    if not previous:
        result = {"status": "no-baseline", "message": "No earlier scan remembered. Run record-scan first."}
    else:
        now = scan_summary(scan)
        result = {
            "status": "ok",
            "since": previous.get("at"),
            "components_added": sorted(set(now["components"]) - set(previous.get("components", []))),
            "components_removed": sorted(set(previous.get("components", [])) - set(now["components"])),
            "findings_new": sorted(set(now["findings"]) - set(previous.get("findings", []))),
            "findings_resolved": sorted(set(previous.get("findings", [])) - set(now["findings"])),
        }
    if warning:
        result["warning"] = warning
    return result


def build_parser():
    parser = argparse.ArgumentParser(description="Hermes memory harness for SolsArch")
    parser.add_argument("--path", default=".", help="Project root (default: current directory)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("read", help="Load memory and a greeting")

    setter = sub.add_parser("set", help="Update project-level fields")
    setter.add_argument("--project-name")
    setter.add_argument("--primary-cloud")
    setter.add_argument("--compute-model", help="server or serverless")
    setter.add_argument("--target-region")
    setter.add_argument("--compliance", help="Comma separated, e.g. SOC2,GDPR")
    setter.add_argument("--budget", type=float, help="Monthly ceiling in USD")
    setter.add_argument("--assumed", action="store_true",
                        help="Mark these values as assumed, not confirmed by the user")

    adr = sub.add_parser("adr", help="Add or update a decision record")
    adr.add_argument("--id", help="Existing id to update, e.g. ADR-001")
    adr.add_argument("--component")
    adr.add_argument("--decision")
    adr.add_argument("--rationale")
    adr.add_argument("--evidence", help="File and line that justify it, e.g. app/db.py:4, or 'Design requirement (SEC-001)'")
    adr.add_argument("--status", help="PROPOSED, APPROVED, REJECTED or SUPERSEDED")
    adr.add_argument("--supersedes", help="Id of an earlier decision this replaces")

    record = sub.add_parser("record-scan", help="Remember a scan")
    record.add_argument("--scan-file", required=True)
    diff = sub.add_parser("diff", help="Compare a scan with the remembered one")
    diff.add_argument("--scan-file", required=True)
    return parser


COMMANDS = {"read": cmd_read, "set": cmd_set, "adr": cmd_adr,
            "record-scan": cmd_record_scan, "diff": cmd_diff}


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except MemoryError_ as error:
        sys.stdout.write(json.dumps({"status": "error", "message": str(error)}, indent=2) + "\n")
        return 1
    except OSError as error:
        sys.stdout.write(json.dumps({"status": "error", "message": "File error: %s" % error}, indent=2) + "\n")
        return 1
    sys.stdout.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
