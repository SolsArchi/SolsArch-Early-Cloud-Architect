#!/usr/bin/env python3
"""Mermaid sanitizer and structural validator for SolsArch diagrams.

Fixes the mistakes that most often break Mermaid rendering (unquoted labels with
parentheses or slashes, stray angle brackets, semicolons in sequence messages,
reserved word `end` used as a node id, smart quotes, code fences) and then checks
the result for structural problems. It is idempotent: running it twice gives the
same text.

This is a structural check, not Mermaid's own parser. Pass --render-check to also
render with the `mmdc` CLI when it is installed.

Standard library only. Usage:
    python diagram_sanitizer.py FILE [--check] [--json] [--in-place] [--render-check]
    python diagram_sanitizer.py --markdown REPORT.md [--in-place]
    cat diagram.mmd | python diagram_sanitizer.py -
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DIAGRAM_TYPES = {
    "graph", "flowchart", "flowchart-elk", "sequencediagram", "classdiagram",
    "statediagram", "statediagram-v2", "erdiagram", "gantt", "pie", "journey", "mindmap",
    "timeline", "gitgraph", "c4context", "c4container", "c4component", "c4dynamic",
    "c4deployment", "quadrantchart", "requirementdiagram", "sankey-beta", "xychart-beta",
    "block-beta", "architecture-beta", "packet-beta", "kanban",
}
FLOW_TYPES = {"graph", "flowchart", "flowchart-elk"}
C4_TYPES = {"c4context", "c4container", "c4component", "c4dynamic", "c4deployment"}
OPENERS = [("[(", ")]"), ("((", "))"), ("([", "])"), ("[[", "]]"), ("{{", "}}"),
           ("[/", "/]"), ("[\\", "\\]"), ("[", "]"), ("(", ")"), ("{", "}")]
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FLOW_PASSTHROUGH = ("%%", "style ", "classdef ", "class ", "linkstyle ", "click ", "direction ")
SEQ_BLOCK_RE = re.compile(r"^(\s*)(loop|alt|else|opt|par|and|critical|option|break|rect|title)\b(.*)$")
SEQ_PARTICIPANT_RE = re.compile(r"^(\s*)(participant|actor)\s+(.+)$")
SEQ_NO_TEXT = ("activate", "deactivate", "autonumber", "end", "%%", "box", "create", "destroy")
ENTITY_SPLIT_RE = re.compile(r"(#\w+;)")
FENCE_RE = re.compile(r"```[ \t]*([\w\-]*)[ \t]*\n(.*?)\n[ \t]*```", re.S)
CHAR_FIXES = {
    "​": "", "‌": "", "‍": "", "﻿": "", " ": " ",
    "“": '"', "”": '"', "‘": "'", "’": "'",
}


# ---------------------------------------------------------------- text helpers
def preprocess(text):
    for bad, good in CHAR_FIXES.items():
        text = text.replace(bad, good)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    for match in FENCE_RE.finditer(text):
        if match.group(1).lower() in ("", "mermaid"):
            text = match.group(2)
            break
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def escape_label(label):
    """Return label text that is safe inside a double-quoted Mermaid label."""
    text = label.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    text = text.replace("\\n", "<br/>")
    text = re.sub(r"<\s*br\s*/?\s*>", "\x00", text, flags=re.I)
    text = text.replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")
    text = text.replace("`", "'")
    text = text.replace("\x00", "<br/>")
    return re.sub(r"[ ]{2,}", " ", text)


def escape_semicolons(text):
    parts = ENTITY_SPLIT_RE.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = parts[index].replace(";", "#59;")
    return "".join(parts)


def escape_angles(text):
    text = re.sub(r"<\s*br\s*/?\s*>", "\x00", text, flags=re.I)
    text = text.replace("<", "#lt;").replace(">", "#gt;")
    return text.replace("\x00", "<br/>")


def strip_quoted(text):
    return re.sub(r'"[^"]*"', '""', text)


def detect_type(lines):
    in_front = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            in_front = not in_front
            continue
        if in_front or stripped.startswith("%%"):
            continue
        return index, stripped.split()[0].rstrip(";").lower()
    return None, None


# ---------------------------------------------------------------- flowcharts
def find_closer(line, start, opener, closer):
    """Index of the closing token for a label that begins at `start`, or -1."""
    if len(opener) == 1:
        depth = 1
        for index in range(start, len(line)):
            char = line[index]
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    return index
        return -1
    return line.find(closer, start)


def sanitize_flow_line(line):
    out = []
    i, n = 0, len(line)
    bare_end = line.strip() == "end"
    while i < n:
        char = line[i]
        if char == '"':
            j = line.find('"', i + 1)
            if j == -1:
                out.append(line[i:])
                break
            out.append(line[i:j + 1])
            i = j + 1
        elif char == "|":
            j = line.find("|", i + 1)
            if j == -1:
                out.append(line[i:])
                break
            out.append('|"%s"|' % escape_label(line[i + 1:j]))
            i = j + 1
        elif char.isalpha() or char == "_":
            match = IDENT_RE.match(line, i)
            ident, end = match.group(), match.end()
            if ident == "end" and not bare_end:
                ident = "end_node"
            consumed = False
            for opener, closer in OPENERS:
                if not line.startswith(opener, end):
                    continue
                start = end + len(opener)
                label_end = -1
                if start < n and line[start] == '"':
                    j = line.find('"', start + 1)
                    if j != -1 and line.startswith(closer, j + 1):
                        label, label_end = line[start:j + 1], j + 1
                if label_end == -1:
                    j = find_closer(line, start, opener[-1] if len(opener) == 1 else opener, closer)
                    if j == -1:
                        break
                    label, label_end = line[start:j], j
                out.append("%s%s\"%s\"%s" % (ident, opener, escape_label(label), closer))
                i = label_end + len(closer)
                consumed = True
                break
            if not consumed:
                out.append(ident)
                i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)


def sanitize_flowchart(lines, header_index, whole_text):
    out = list(lines[:header_index + 1])
    counter = 1
    for line in lines[header_index + 1:]:
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped or lowered.startswith(FLOW_PASSTHROUGH):
            out.append(line)
            continue
        if lowered.startswith("subgraph"):
            indent = line[:len(line) - len(line.lstrip())]
            rest = stripped[len("subgraph"):].strip()
            bracket = re.match(r"^([A-Za-z_][\w\-]*)\s*\[(.*)\]\s*$", rest)
            if bracket:
                out.append('%ssubgraph %s["%s"]' % (indent, bracket.group(1), escape_label(bracket.group(2))))
            elif re.match(r"^[A-Za-z_]\w*$", rest):
                out.append("%ssubgraph %s" % (indent, rest))
            elif rest:
                while ("sg_%d" % counter) in whole_text:
                    counter += 1
                out.append('%ssubgraph sg_%d["%s"]' % (indent, counter, escape_label(rest)))
                counter += 1
            else:
                out.append(line)
            continue
        out.append(sanitize_flow_line(line))
    return out


# ---------------------------------------------------------------- sequence diagrams
def sanitize_sequence(lines, header_index):
    out = list(lines[:header_index + 1])
    for line in lines[header_index + 1:]:
        stripped = line.strip()
        if not stripped or stripped.lower().startswith(SEQ_NO_TEXT) and ":" not in stripped:
            out.append(line)
            continue
        if stripped.startswith("%%"):
            out.append(line)
            continue
        participant = SEQ_PARTICIPANT_RE.match(line)
        if participant:
            indent, kind, rest = participant.groups()
            if " as " in rest:
                ident, label = rest.split(" as ", 1)
                rest = "%s as %s" % (ident, escape_semicolons(escape_angles(label)))
            out.append("%s%s %s" % (indent, kind, rest))
            continue
        block = SEQ_BLOCK_RE.match(line)
        if block:
            indent, word, rest = block.groups()
            out.append("%s%s%s" % (indent, word, escape_semicolons(escape_angles(rest))))
            continue
        if ":" in line:
            head, tail = line.split(":", 1)
            out.append("%s:%s" % (head, escape_semicolons(escape_angles(tail))))
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------- validation
def validate(lines, dtype, header_index):
    issues = []
    if dtype is None:
        return ["Diagram is empty."]
    if dtype not in DIAGRAM_TYPES:
        issues.append("Unknown diagram type '%s'." % dtype)
        return issues
    body = lines[header_index + 1:]
    if not any(line.strip() and not line.strip().startswith("%%") for line in body):
        issues.append("Diagram has a header but no content.")
    for number, line in enumerate(lines, 1):
        if line.strip().startswith("%%"):
            continue
        if line.count('"') % 2:
            issues.append("Line %d: unbalanced double quote." % number)
    if dtype in FLOW_TYPES:
        opened = sum(1 for line in body if line.strip().lower().startswith("subgraph"))
        closed = sum(1 for line in body if line.strip() == "end")
        if opened != closed:
            issues.append("%d 'subgraph' but %d 'end'." % (opened, closed))
        for number, line in enumerate(lines, 1):
            if number <= header_index + 1 or line.strip().startswith("%%"):
                continue
            plain = strip_quoted(line)
            for left, right, name in (("[", "]", "square brackets"), ("(", ")", "parentheses"),
                                      ("{", "}", "braces")):
                if plain.count(left) != plain.count(right):
                    issues.append("Line %d: unbalanced %s." % (number, name))
            if '[""]' in line or '("")' in line:
                issues.append("Line %d: empty node label." % number)
    elif dtype in C4_TYPES:
        plain = strip_quoted("\n".join(lines))
        if plain.count("(") != plain.count(")"):
            issues.append("Unbalanced parentheses in C4 diagram.")
        if plain.count("{") != plain.count("}"):
            issues.append("Unbalanced braces in C4 diagram.")
    elif dtype == "sequencediagram":
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if re.search(r"-{1,2}>{1,2}|-[xX)]", stripped) and ":" not in stripped \
                    and not stripped.startswith("%%"):
                issues.append("Line %d: message without ':' text." % number)
    return issues


def render_check(text):
    exe = shutil.which("mmdc")
    if not exe:
        return None, "render-check skipped: mmdc is not installed."
    with tempfile.TemporaryDirectory() as tmp:
        source, target = Path(tmp) / "d.mmd", Path(tmp) / "d.svg"
        source.write_text(text, encoding="utf-8")
        try:
            done = subprocess.run([exe, "-i", str(source), "-o", str(target)],
                                  capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, "render-check skipped: %s" % error
        if done.returncode != 0:
            return False, "mmdc failed: " + (done.stderr or done.stdout).strip()[:400]
    return True, "mmdc rendered the diagram."


# ---------------------------------------------------------------- public api
def sanitize(text):
    """Return (sanitized_text, diagram_type, issues)."""
    lines = preprocess(text)
    header_index, dtype = detect_type(lines)
    if dtype is None:
        return "", None, ["Diagram is empty."]
    if dtype in FLOW_TYPES:
        lines = sanitize_flowchart(lines, header_index, "\n".join(lines))
    elif dtype == "sequencediagram":
        lines = sanitize_sequence(lines, header_index)
    issues = validate(lines, dtype, header_index)
    return "\n".join(lines) + "\n", dtype, issues


def sanitize_markdown(text):
    """Sanitize every mermaid fence in a markdown document."""
    report = []
    count = [0]

    def replace(match):
        if match.group(1).lower() != "mermaid":
            return match.group(0)
        count[0] += 1
        cleaned, dtype, issues = sanitize(match.group(2))
        report.append({"block": count[0], "diagram_type": dtype, "issues": issues,
                       "changed": cleaned.strip() != match.group(2).strip()})
        return "```mermaid\n%s```" % cleaned
    return FENCE_RE.sub(replace, text), report


# ---------------------------------------------------------------- cli
def read_input(source):
    if source in (None, "-"):
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sanitize and validate Mermaid diagrams")
    parser.add_argument("file", nargs="?", help="Diagram file, or - for stdin")
    parser.add_argument("--check", action="store_true", help="Report issues only; print no diagram")
    parser.add_argument("--json", action="store_true", help="Emit a JSON report")
    parser.add_argument("--in-place", action="store_true", help="Rewrite FILE with the sanitized text")
    parser.add_argument("--markdown", action="store_true",
                        help="Treat FILE as markdown and sanitize every mermaid fence")
    parser.add_argument("--render-check", action="store_true", help="Also render with mmdc if installed")
    args = parser.parse_args(argv)
    if args.in_place and args.file in (None, "-"):
        sys.stderr.write("error: --in-place needs a file path\n")
        return 2
    try:
        text = read_input(args.file)
    except (OSError, UnicodeDecodeError) as error:
        sys.stderr.write("error: cannot read input: %s\n" % error)
        return 2

    if args.markdown:
        cleaned, report = sanitize_markdown(text)
        failed = any(item["issues"] for item in report)
        if args.in_place:
            Path(args.file).write_bytes(cleaned.encode("utf-8"))
        payload = {"blocks": report, "ok": not failed}
        if not args.in_place and not args.check:
            payload["sanitized"] = cleaned
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 1 if failed else 0

    cleaned, dtype, issues = sanitize(text)
    render_note = None
    if args.render_check and cleaned:
        ok, render_note = render_check(cleaned)
        if ok is False:
            issues.append(render_note)
    if args.in_place and cleaned:
        Path(args.file).write_bytes(cleaned.encode("utf-8"))
    if args.json:
        changed = cleaned.strip() != text.replace("\r\n", "\n").strip()
        payload = {"diagram_type": dtype, "changed": changed, "issues": issues, "ok": not issues}
        if render_note:
            payload["render_check"] = render_note
        if not args.check and not args.in_place:
            payload["sanitized"] = cleaned
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        if not args.check and not args.in_place:
            sys.stdout.write(cleaned)
        for issue in issues:
            sys.stderr.write("issue: %s\n" % issue)
        if render_note:
            sys.stderr.write(render_note + "\n")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------- block-level lint
# mermaid_lint.py checks every fenced block of a markdown file, including C4 diagrams. It is
# re-exported here so callers have one module to import.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mermaid_lint import find_blocks, sanitize_text  # noqa: E402,F401
