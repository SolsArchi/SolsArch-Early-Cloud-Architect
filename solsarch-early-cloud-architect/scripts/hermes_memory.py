#!/usr/bin/env python3
"""Hermes memory: persist architecture decisions in <project>/.solsarch/memory.json.

Standard library only.

Commands (all accept --root DIR, default current directory):
  read                       print the memory as JSON ({"exists": false} when absent)
  summary                    print a short human-readable recap
  set KEY VALUE              set project_name | primary_cloud | compute_model (server|serverless) | target_region |
                             compliance_frameworks (comma list) | budget_ceiling_monthly_usd
                             (use "none" to record that there is no compliance need / no budget ceiling)
  add-adr --component C --decision D --rationale R [--status S] [--supersedes ADR-001]
  touch                      refresh last_analyzed
  adr | record-scan | diff   scan diffing and ADR updates by id (same as memory_harness.py; --root works here too)
"""
import argparse
import datetime
import json
import os
import re
import sys
import tempfile

SCHEMA_VERSION = 1
CLOUDS = ("aws", "azure", "gcp", "multi")
STATUSES = ("PROPOSED", "APPROVED", "REJECTED", "SUPERSEDED")
NO_VALUE = ("none", "unset", "null", "n/a", "")
COMPUTE_MODELS = ("server", "serverless")
SETTABLE = ("project_name", "primary_cloud", "compute_model", "target_region", "compliance_frameworks", "budget_ceiling_monthly_usd")


def memory_path(root):
    return os.path.join(root, ".solsarch", "memory.json")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_memory(root):
    return {
        "schema_version": SCHEMA_VERSION,
        "project_name": os.path.basename(os.path.abspath(root)),
        "primary_cloud": None,
        "compute_model": None,
        "target_region": None,
        "compliance_frameworks": [],
        "budget_ceiling_monthly_usd": None,
        "last_analyzed": None,
        "architectural_decisions": [],
    }


def load(root):
    """Return (memory_dict_or_None, warning_or_None). A corrupt file is moved aside, never lost."""
    path = memory_path(root)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("architectural_decisions", []), list):
            raise ValueError("unexpected structure")
        base = empty_memory(root)
        base.update(data)
        return base, None
    except (ValueError, OSError) as exc:
        backup = "%s.corrupt-%s" % (path, datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
        try:
            os.replace(path, backup)
        except OSError:
            backup = "(could not move)"
        return None, "memory.json was unreadable (%s); moved to %s and starting fresh" % (exc, backup)


def save(root, data):
    path = memory_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_or_new(root):
    data, warning = load(root)
    if warning:
        print("warning: " + warning, file=sys.stderr)
    return data if data is not None else empty_memory(root)


def validate_setting(key, value):
    if key not in SETTABLE:
        raise ValueError("unknown key '%s'; allowed: %s" % (key, ", ".join(SETTABLE)))
    if key == "primary_cloud":
        v = value.strip().lower()
        if v not in CLOUDS:
            raise ValueError("primary_cloud must be one of: %s" % ", ".join(CLOUDS))
        return v
    if key == "compute_model":
        v = value.strip().lower()
        if v not in COMPUTE_MODELS:
            raise ValueError("compute_model must be one of: %s" % ", ".join(COMPUTE_MODELS))
        return v
    if key == "target_region":
        v = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError("target_region looks invalid: '%s'" % value)
        return v
    if key == "compliance_frameworks":
        if value.strip().lower() in NO_VALUE:
            return []
        return [p.strip() for p in value.split(",") if p.strip()]
    if key == "budget_ceiling_monthly_usd":
        if value.strip().lower() in NO_VALUE:
            return None
        try:
            num = float(value)
        except ValueError:
            raise ValueError("budget_ceiling_monthly_usd must be a number")
        if num < 0:
            raise ValueError("budget_ceiling_monthly_usd cannot be negative")
        return int(num) if num == int(num) else num
    return value.strip()


def next_adr_id(decisions):
    highest = 0
    for d in decisions:
        m = re.fullmatch(r"ADR-(\d+)", str(d.get("id", "")))
        if m:
            highest = max(highest, int(m.group(1)))
    return "ADR-%03d" % (highest + 1)


def cmd_read(args):
    data, warning = load(args.root)
    if warning:
        print("warning: " + warning, file=sys.stderr)
    out = {"exists": False} if data is None else dict(data, exists=True)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=True)
    sys.stdout.write("\n")
    return 0


def cmd_summary(args):
    data, warning = load(args.root)
    if warning:
        print("warning: " + warning, file=sys.stderr)
    if data is None:
        print("No Hermes memory yet for this project.")
        return 0
    print("Project: %s" % data["project_name"])
    print("Cloud: %s   Compute model: %s   Region: %s" % (
        data["primary_cloud"] or "not set", data.get("compute_model") or "not set", data["target_region"] or "not set"))
    print("Compliance: %s" % (", ".join(data["compliance_frameworks"]) or "none recorded"))
    budget = data["budget_ceiling_monthly_usd"]
    print("Monthly budget ceiling: %s" % (budget if budget is not None else "not set"))
    print("Last analyzed: %s" % (data["last_analyzed"] or "never"))
    decisions = data["architectural_decisions"]
    print("Decisions (%d):" % len(decisions))
    for d in decisions:
        print("  %s [%s] %s: %s" % (d.get("id"), d.get("status"), d.get("component"), d.get("decision")))
    return 0


def cmd_set(args):
    try:
        value = validate_setting(args.key, args.value)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    data = load_or_new(args.root)
    data[args.key] = value
    data["last_analyzed"] = now_iso()
    save(args.root, data)
    print("%s = %s" % (args.key, json.dumps(value)))
    return 0


def cmd_add_adr(args):
    if args.status not in STATUSES:
        print("error: status must be one of %s" % ", ".join(STATUSES), file=sys.stderr)
        return 2
    for field in ("component", "decision", "rationale"):
        if not getattr(args, field).strip():
            print("error: --%s cannot be empty" % field, file=sys.stderr)
            return 2
    data = load_or_new(args.root)
    decisions = data["architectural_decisions"]
    if args.supersedes:
        target = next((d for d in decisions if d.get("id") == args.supersedes), None)
        if target is None:
            print("error: cannot supersede unknown decision %s" % args.supersedes, file=sys.stderr)
            return 2
    adr_id = next_adr_id(decisions)
    if args.supersedes:
        target["status"] = "SUPERSEDED"
        target["superseded_by"] = adr_id
    entry = {
        "id": adr_id,
        "timestamp": now_iso()[:10],
        "component": args.component.strip(),
        "decision": args.decision.strip(),
        "status": args.status,
        "rationale": args.rationale.strip(),
    }
    if args.supersedes:
        entry["supersedes"] = args.supersedes
    decisions.append(entry)
    data["last_analyzed"] = now_iso()
    save(args.root, data)
    print(json.dumps(entry, ensure_ascii=True))
    return 0


def cmd_touch(args):
    data = load_or_new(args.root)
    data["last_analyzed"] = now_iso()
    save(args.root, data)
    print(data["last_analyzed"])
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description="Hermes memory for SolsArch Early Cloud Architect.")
    sub = ap.add_subparsers(dest="command", required=True)

    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--root", default=os.getcwd(), help="project root (default: current directory)")
        p.set_defaults(func=fn)
        return p

    add("read", cmd_read, "print memory as JSON")
    add("summary", cmd_summary, "print a human-readable recap")
    add("touch", cmd_touch, "refresh last_analyzed")
    p = add("set", cmd_set, "set a project-level field")
    p.add_argument("key")
    p.add_argument("value")
    p = add("add-adr", cmd_add_adr, "record an architectural decision")
    p.add_argument("--component", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--rationale", required=True)
    p.add_argument("--status", default="PROPOSED")
    p.add_argument("--supersedes", default=None)
    return ap


HARNESS_COMMANDS = ("adr", "record-scan", "diff")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--path" in argv:  # legacy memory_harness form: --path DIR COMMAND ...
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import memory_harness
        return memory_harness.main(argv)
    if argv and argv[0] in HARNESS_COMMANDS:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import memory_harness
        rest = ["--path" if a == "--root" else a for a in argv[1:]]
        # the harness takes --path before the sub-command
        path = []
        if "--path" in rest:
            i = rest.index("--path")
            path, rest = rest[i:i + 2], rest[:i] + rest[i + 2:]
        return memory_harness.main(path + [argv[0]] + rest)
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
