#!/usr/bin/env python3
"""Check an architecture report against the scanner evidence before delivering it.

Standard library only. Catches the mistakes that make reports inaccurate: services without
evidence, evidence ids that do not exist, blockers without proof, leaked secrets, unfilled
template fields, dollar figures, invalid Mermaid, diagrams with no explanation, placements the
report forgot to explain, and web sources that were never logged.

Usage:
  python verify_report.py REPORT.md --evidence evidence.json
         [--placements placements.json] [--research research.json] [--json]
Exit codes: 0 no errors, 1 errors found, 2 usage error.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import diagram_sanitizer  # noqa: E402

EVIDENCE_ID = re.compile(r"\bE\d{3,}\b")
RESEARCH_ID = re.compile(r"\bR\d{3,}\b")
SEC_ID = re.compile(r"\bSEC-00([1-5])\b")
SECRET_LEAK = re.compile(r"AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
WEAK_KINDS = ("language", "port")
MONEY = re.compile(r"\$\s?\d")
MIN_DIAGRAM_WORDS = 40
MIN_SERVICE_WORDS = 30
NOT_PERFORMED = re.compile(r"web verification:\s*not performed", re.I)


def table_rows(lines):
    """Yield (header_cells, row_cells, line_number) for every data row of every markdown table."""
    header = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not (s.startswith("|") and s.endswith("|")):
            header = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        yield header, cells, i + 1


def words(text):
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text))


def section_text_after(lines, start):
    """Text after lines[start] up to the next heading or code fence."""
    out = []
    for ln in lines[start + 1:]:
        s = ln.strip()
        if s.startswith("#") or s.startswith("```"):
            break
        out.append(s)
    return " ".join(out)


def check_diagram_explanations(lines, add):
    in_fence, fence_kind = False, None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("```"):
            if not in_fence:
                in_fence, fence_kind = True, s[3:].strip().lower()
            else:
                if fence_kind == "mermaid":
                    following = section_text_after(lines, i)
                    if words(following) < MIN_DIAGRAM_WORDS:
                        add("error", i + 1, "diagram has no explanation; add at least %d words after it saying what "
                            "it shows and why those services were chosen" % MIN_DIAGRAM_WORDS)
                in_fence, fence_kind = False, None


def check_placements(lines, placements, add):
    heading_idx = {}
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#"):
            for pid in re.findall(r"\bP\d{2,}\b", ln):
                heading_idx.setdefault(pid, i)
    text_low = "\n".join(lines).lower()
    for p in placements["placements"]:
        if p["service"].lower() not in text_low:
            add("error", 0, "placement %s (%s) is missing from the report" % (p["id"], p["service"]))
        idx = heading_idx.get(p["id"])
        if idx is None:
            add("error", 0, "no section heading for %s (%s); add '### %s %s' with an explanation"
                % (p["id"], p["service"], p["id"], p["service"]))
        elif words(section_text_after(lines, idx)) < MIN_SERVICE_WORDS:
            add("error", idx + 1, "section for %s is too short; explain what in the codebase needs it and why "
                "this service on this cloud (at least %d words)" % (p["id"], MIN_SERVICE_WORDS))


def check_research(report_text, placements, research, add):
    valid = {e["id"]: e for e in research.get("entries", [])}
    cited = set(RESEARCH_ID.findall(report_text))
    for rid in sorted(cited - set(valid)):
        add("error", 0, "cites %s, which is not in the research log" % rid)
    if not valid:
        if not NOT_PERFORMED.search(report_text):
            add("error", 0, "no web sources were logged; do the verification step or write "
                "'Web verification: not performed' in the report")
        return
    if placements:
        by_service = {}
        for e in valid.values():
            by_service.setdefault(e["service"], []).append(e["id"])
        for p in placements["placements"]:
            ids = by_service.get(p["service"])
            if not ids:
                if not NOT_PERFORMED.search(report_text):
                    add("error", 0, "no web source logged for %s" % p["service"])
            elif not cited & set(ids):
                add("error", 0, "report does not cite any logged source for %s (%s)" % (p["service"], ", ".join(ids)))


def check(report_text, evidence, placements=None, research=None):
    valid = {e["id"] for e in evidence.get("evidence", [])}
    kinds = {e["id"]: e.get("kind") for e in evidence.get("evidence", [])}
    problems = []

    def add(sev, line, msg):
        problems.append({"severity": sev, "line": line, "message": msg})

    lines = report_text.split("\n")
    in_fence = False
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for eid in EVIDENCE_ID.findall(ln):
            if eid not in valid:
                add("error", i, "cites %s, which is not in the scanner evidence" % eid)
        if "(delete this note" in ln.lower():
            add("error", i, "template instruction note left in the report; delete it")
        if "{{" in ln and "}}" in ln:
            add("error", i, "unfilled template field: " + re.search(r"\{\{[^}]*\}\}", ln).group(0)[:40])
        if MONEY.search(ln):
            add("warning", i, "dollar figure found; prices are not verifiable from the repo, remove or mark as user input")
        if re.match(r"\s*- \[ \]", ln):
            add("warning", i, "self-check item not ticked")
        if SECRET_LEAK.search(ln):
            add("error", i, "report contains a secret-looking value; remove it")

    service_rows = 0
    for header, cells, ln_no in table_rows(lines):
        row_text = " ".join(cells)
        is_service_table = any("service" in h.lower() for h in header) and any("evidence" in h.lower() for h in header)
        if is_service_table:
            service_rows += 1
            cited = EVIDENCE_ID.findall(row_text)
            label = next((c for h, c in zip(header, cells) if "service" in h.lower()), cells[0])[:40]
            if not cited:
                add("error", ln_no, "service '%s' cites no evidence id" % label)
            elif all(kinds.get(c) in WEAK_KINDS for c in cited if c in kinds) and any(c in kinds for c in cited):
                add("error", ln_no, "service '%s' cites only language or port evidence; cite the dependency or "
                    "config that creates the need" % label)
        if header and header[0].strip().lower() == "rule":
            m = SEC_ID.search(cells[0])
            if not m:
                if cells[0] and not cells[0].startswith("{{"):
                    add("error", ln_no, "unknown rule id '%s'" % cells[0][:20])
            elif m.group(1) != "1" and not EVIDENCE_ID.search(row_text):
                add("error", ln_no, "%s reported without evidence ids" % m.group(0))
    if service_rows == 0:
        add("warning", 0, "no service decision table found")

    if "```mermaid" in report_text:
        _, mermaid_issues, _ = diagram_sanitizer.sanitize_text(report_text, fix=False)
        for m in mermaid_issues:
            if m["severity"] == "error":
                add("error", m["line"], "mermaid [%s] %s" % (m["rule"], m["message"]))
        if placements is not None:
            check_diagram_explanations(lines, add)
    if placements is not None:
        check_placements(lines, placements, add)
    if research is not None:
        check_research(report_text, placements, research, add)
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Verify an architecture report against scanner evidence.")
    ap.add_argument("report")
    ap.add_argument("--evidence", required=True, help="JSON written by ast_scanner.py")
    ap.add_argument("--placements", help="JSON written by place_services.py")
    ap.add_argument("--research", help="JSON written by research.py add")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        with open(args.report, "r", encoding="utf-8") as fh:
            report = fh.read()
        with open(args.evidence, "r", encoding="utf-8") as fh:
            evidence = json.load(fh)
        placements = None
        if args.placements:
            with open(args.placements, "r", encoding="utf-8") as fh:
                placements = json.load(fh)
        research = None
        if args.research:
            if os.path.isfile(args.research):
                with open(args.research, "r", encoding="utf-8") as fh:
                    research = json.load(fh)
            else:
                research = {"entries": []}
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    problems = check(report, evidence, placements, research)
    errors = [p for p in problems if p["severity"] == "error"]
    if args.json:
        json.dump({"ok": not errors, "problems": problems}, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
    else:
        for p in problems:
            print("%s:%d: %s %s" % (args.report, p["line"], p["severity"], p["message"]))
        print("%s: %s" % (args.report, "OK" if not errors else "%d error(s)" % len(errors)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
