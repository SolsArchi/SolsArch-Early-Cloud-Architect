#!/usr/bin/env python3
"""Validate (and optionally repair) Mermaid diagrams so they render without errors.

Standard library only. Accepts a .mmd file or a Markdown file containing
```mermaid fences; with no fences the whole file is treated as one diagram.

Usage:
  python mermaid_lint.py FILE [FILE ...] [--fix] [--json]
  python mermaid_lint.py --stdin [--fix]

Exit codes: 0 clean (or fully repaired), 1 errors remain, 2 usage error.
"""
import argparse
import json
import re
import sys

HEADER_RE = re.compile(
    r"^(?:(?:flowchart|graph)(?:\s+(?:TD|TB|BT|RL|LR))?|sequenceDiagram|classDiagram(?:-v2)?|"
    r"stateDiagram(?:-v2)?|erDiagram|gantt|journey|pie|mindmap|timeline|"
    r"C4Context|C4Container|C4Component|C4Dynamic|C4Deployment)\b"
)

OPENERS = [("[[", "]]"), ("[(", ")]"), ("((", "))"), ("([", "])"), ("{{", "}}"), ("[", "]"), ("(", ")"), ("{", "}")]
ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
FLOW_SKIP_PREFIXES = ("%%", "classDef ", "class ", "style ", "linkStyle", "click ", "direction ")
SMART_QUOTES = {"“": '"', "”": '"', "‘": "'", "’": "'"}
LT_GT_RE = re.compile(r"<(?!br\s*/?>)|(?<!<br)(?<!<br/)(?<!<br /)>")

C4_ELEMENTS = {
    "Person", "Person_Ext", "System", "System_Ext", "SystemDb", "SystemDb_Ext", "SystemQueue", "SystemQueue_Ext",
    "Container", "Container_Ext", "ContainerDb", "ContainerDb_Ext", "ContainerQueue", "ContainerQueue_Ext",
    "Component", "Component_Ext", "ComponentDb", "ComponentDb_Ext", "ComponentQueue", "ComponentQueue_Ext",
    "Boundary", "Enterprise_Boundary", "System_Boundary", "Container_Boundary",
    "Deployment_Node", "Node", "Node_L", "Node_R",
}
C4_RELS = {"Rel", "BiRel", "Rel_U", "Rel_Up", "Rel_D", "Rel_Down", "Rel_L", "Rel_Left", "Rel_R", "Rel_Right",
           "Rel_Back", "RelIndex"}
C4_IGNORED = {"UpdateElementStyle", "UpdateRelStyle", "UpdateLayoutConfig", "LAYOUT_TOP_DOWN", "LAYOUT_LEFT_RIGHT",
              "LAYOUT_WITH_LEGEND", "SHOW_LEGEND", "SHOW_FLOATING_LEGEND", "LAYOUT_AS_SKETCH", "AddElementTag",
              "AddRelTag", "AddBoundaryTag", "HIDE_STEREOTYPE", "SetPropertyHeader", "WithoutPropertyHeader"}
C4_BOUNDARIES = {"Boundary", "Enterprise_Boundary", "System_Boundary", "Container_Boundary",
                 "Deployment_Node", "Node", "Node_L", "Node_R"}


def issue(idx, severity, rule, msg, fixable=False):
    return {"idx": idx, "severity": severity, "rule": rule, "message": msg, "fixable": fixable}


def escape_label(text):
    text = text.replace('"', "#quot;")
    return LT_GT_RE.sub(lambda m: "#lt;" if m.group(0) == "<" else "#gt;", text)


def find_close(line, start, opener, closer):
    if len(opener) > 1:
        return line.find(closer, start)
    depth = 1
    for j in range(start, len(line)):
        ch = line[j]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return j
    return -1


def process_flow_line(line, idx, defined):
    """Return (fixed_line, masked_line, issues). Labels are masked in masked_line."""
    out, masked, issues = [], [], []
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c == '"':
            j = line.find('"', i + 1)
            if j == -1:
                issues.append(issue(idx, "error", "unbalanced-quote", "Unclosed double quote"))
                out.append(line[i:])
                masked.append(" " * (n - i))
                break
            out.append(line[i:j + 1])
            masked.append(" " * (j + 1 - i))
            i = j + 1
            continue
        if c == "|":
            j = line.find("|", i + 1)
            if j == -1:
                out.append(c)
                masked.append(c)
                i += 1
                continue
            inner = line[i + 1:j]
            stripped = inner.strip()
            if len(stripped) >= 2 and stripped.startswith('"') and stripped.endswith('"'):
                new_inner = inner
            elif re.search(r'[()\[\]{}"<>;]', inner):
                new_inner = '"%s"' % escape_label(stripped)
                issues.append(issue(idx, "error", "edge-label",
                                    "Edge label with special characters must be double-quoted", True))
            else:
                new_inner = inner
            out.append("|" + new_inner + "|")
            masked.append(" " * (j + 1 - i))
            i = j + 1
            continue
        m = ID_RE.match(line, i) if (i == 0 or not (line[i - 1].isalnum() or line[i - 1] == "_")) else None
        if m:
            end = m.end()
            opener = next(((o, cl) for o, cl in OPENERS if line.startswith(o, end)), None)
            if opener is None:
                out.append(m.group())
                masked.append(m.group())
                i = end
                continue
            o, cl = opener
            start = end + len(o)
            node_id = m.group()
            if line.startswith('"', start):
                k = line.find('"' + cl, start + 1)
                if k == -1:
                    issues.append(issue(idx, "error", "unbalanced-quote",
                                        "Label for '%s' has no closing quote before '%s'" % (node_id, cl)))
                    out.append(line[i:])
                    masked.append(" " * (n - i))
                    break
                content = line[start + 1:k]
                close_idx = k + 1
                if '"' in content or LT_GT_RE.search(content):
                    issues.append(issue(idx, "error", "label-chars",
                                        "Label for '%s' has a raw quote or angle bracket" % node_id, True))
                    content = escape_label(content)
            else:
                close_idx = find_close(line, start, o, cl)
                if close_idx == -1:
                    issues.append(issue(idx, "error", "unclosed-shape",
                                        "Node '%s' opens '%s' but never closes with '%s'" % (node_id, o, cl)))
                    out.append(line[i:])
                    masked.append(" " * (n - i))
                    break
                content = escape_label(line[start:close_idx].strip())
                issues.append(issue(idx, "error", "unquoted-label",
                                    "Label for '%s' must be double-quoted: %s[\"...\"]" % (node_id, node_id), True))
            if not content.strip():
                content = node_id
                issues.append(issue(idx, "error", "empty-label",
                                    "Node '%s' has an empty label" % node_id, True))
            prev = defined.get(node_id)
            if prev is not None and prev != content:
                issues.append(issue(idx, "warning", "redefined-node",
                                    "Node '%s' is defined again with a different label" % node_id))
            defined.setdefault(node_id, content)
            out.append('%s%s"%s"%s' % (node_id, o, content, cl))
            masked.append(node_id + " " * (close_idx + len(cl) - end))
            i = close_idx + len(cl)
            continue
        out.append(c)
        masked.append(c)
        i += 1
    return "".join(out), "".join(masked), issues


def process_flowchart(lines, header_idx):
    new_lines = list(lines)
    issues = []
    defined = {}
    subgraph_ids = {}
    depth = 0
    for idx in range(header_idx + 1, len(lines)):
        line = lines[idx]
        s = line.strip()
        if not s:
            continue
        indent = line[:len(line) - len(line.lstrip())]
        if s.startswith(FLOW_SKIP_PREFIXES):
            continue
        if s == "end":
            depth -= 1
            if depth < 0:
                issues.append(issue(idx, "error", "unbalanced-end", "'end' without a matching 'subgraph'"))
                depth = 0
            continue
        if s.startswith("subgraph"):
            rest = s[len("subgraph"):].strip()
            depth += 1
            if not rest:
                issues.append(issue(idx, "error", "subgraph-no-id", "subgraph needs an id and a quoted title"))
                continue
            if re.fullmatch(r"[A-Za-z_]\w*", rest):
                sid, fixed_rest = rest, rest
            elif re.fullmatch(r'"[^"]*"', rest):
                sid, fixed_rest = None, rest
            elif (id_match := ID_RE.match(rest)) and rest[id_match.end():id_match.end() + 1] in ("[", "(", "{"):
                sid = id_match.group()
                already_node = sid in defined
                fixed_rest, masked, found = process_flow_line(rest, idx, defined)
                issues.extend(found)
                if not already_node:
                    defined.pop(sid, None)
            else:
                sid = re.sub(r"\W+", "_", rest).strip("_") or "group"
                fixed_rest = '%s["%s"]' % (sid, escape_label(rest))
                issues.append(issue(idx, "error", "subgraph-title",
                                    "Multi-word subgraph title must be written as id[\"Title\"]", True))
            if sid is not None:
                if sid in subgraph_ids:
                    issues.append(issue(idx, "error", "duplicate-id", "Subgraph id '%s' is used twice" % sid))
                if sid in defined:
                    issues.append(issue(idx, "error", "duplicate-id",
                                        "Subgraph id '%s' collides with a node id" % sid))
                subgraph_ids[sid] = idx
            new_lines[idx] = indent + "subgraph " + fixed_rest
            continue
        fixed, masked, found = process_flow_line(s, idx, defined)
        issues.extend(found)
        if s != "end" and re.search(r"(?<![\w])end(?![\w])", masked):
            issues.append(issue(idx, "error", "reserved-end",
                                "'end' is reserved in Mermaid; rename the node id (e.g. endpoint)"))
        new_lines[idx] = indent + fixed
    if depth > 0:
        issues.append(issue(len(lines) - 1, "error", "unclosed-subgraph",
                            "%d subgraph block(s) are missing 'end'" % depth))
    clash = set(subgraph_ids) & set(defined)
    for sid in clash:
        if not any(i["rule"] == "duplicate-id" and ("'%s'" % sid) in i["message"] for i in issues):
            issues.append(issue(subgraph_ids[sid], "error", "duplicate-id",
                                "Subgraph id '%s' collides with a node id" % sid))
    return new_lines, issues


SEQ_BLOCK_RE = re.compile(r"^(alt|opt|loop|par|critical|break|rect|box)\b")
SEQ_ARROW_RE = re.compile(r"(-->>|->>|--x|-x|--\)|-\)|-->|->)")


def process_sequence(lines, header_idx):
    new_lines = list(lines)
    issues = []
    depth = 0
    for idx in range(header_idx + 1, len(lines)):
        line = lines[idx]
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        if s == "end":
            depth -= 1
            if depth < 0:
                issues.append(issue(idx, "error", "unbalanced-end", "'end' without a matching block"))
                depth = 0
            continue
        if SEQ_BLOCK_RE.match(s):
            depth += 1
            continue
        pm = re.match(r"^(participant|actor)\s+(.+)$", s)
        if pm:
            m2 = re.match(r"^(\S+)(?:\s+as\s+(.+))?$", pm.group(2))
            if not m2 or not re.fullmatch(r"[\w.\-]+", m2.group(1)):
                issues.append(issue(idx, "error", "participant-id",
                                    "Participant id must have no spaces or symbols; use 'participant ID as Label'"))
            continue
        if ":" in s and (SEQ_ARROW_RE.search(s.split(":", 1)[0]) or s.startswith("Note ")):
            head, text = line.split(":", 1)
            if ";" in re.sub(r"#\w+;", "", text):
                new_lines[idx] = head + ":" + re.sub(r"(#\w+;)|;", lambda m: m.group(1) or "#59;", text)
                issues.append(issue(idx, "error", "semicolon",
                                    "';' ends a statement in sequence diagrams; use #59;", True))
    if depth > 0:
        issues.append(issue(len(lines) - 1, "error", "unclosed-block", "%d block(s) are missing 'end'" % depth))
    return new_lines, issues


def split_args(raw):
    args, cur, in_q = [], [], False
    for ch in raw:
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
        elif ch == "," and not in_q:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    args.append("".join(cur).strip())
    return args, in_q


def process_c4(lines, header_idx):
    new_lines = list(lines)
    issues = []
    aliases = {}
    rels = []
    brace_depth = 0
    for idx in range(header_idx + 1, len(lines)):
        line = lines[idx]
        s = line.strip()
        if not s or s.startswith("%%") or s.startswith("title"):
            continue
        if s == "}":
            brace_depth -= 1
            if brace_depth < 0:
                issues.append(issue(idx, "error", "unbalanced-brace", "'}' without a matching '{'"))
                brace_depth = 0
            continue
        m = re.match(r"^(\w+)\((.*)\)\s*(\{)?\s*$", s)
        if not m:
            issues.append(issue(idx, "error", "c4-syntax", "Expected a macro call like Container(alias, \"Label\")"))
            continue
        macro, raw, brace = m.group(1), m.group(2), m.group(3)
        if brace:
            brace_depth += 1
        args, open_quote = split_args(raw)
        if open_quote:
            issues.append(issue(idx, "error", "unbalanced-quote", "Unclosed double quote in %s(...)" % macro))
            continue
        if macro in C4_IGNORED:
            continue
        if macro in C4_ELEMENTS or macro in C4_RELS:
            first_label = 1 if macro in C4_ELEMENTS else 2
            if macro in C4_BOUNDARIES and not brace:
                issues.append(issue(idx, "warning", "c4-boundary", "%s should open a block with '{'" % macro))
            changed = False
            for k in range(len(args)):
                a = args[k]
                if k < first_label:
                    if not re.fullmatch(r"[A-Za-z_]\w*", a):
                        issues.append(issue(idx, "error", "c4-alias",
                                            "Alias '%s' must be letters, digits or underscore" % a))
                    continue
                if a and not a.startswith(('"', "$")):
                    args[k] = '"%s"' % escape_label(a)
                    changed = True
                    issues.append(issue(idx, "error", "c4-unquoted",
                                        "Argument %d of %s must be double-quoted" % (k + 1, macro), True))
            if changed:
                indent = line[:len(line) - len(line.lstrip())]
                new_lines[idx] = "%s%s(%s)%s" % (indent, macro, ", ".join(args), " {" if brace else "")
            if macro in C4_ELEMENTS and args and re.fullmatch(r"[A-Za-z_]\w*", args[0]):
                if args[0] in aliases:
                    issues.append(issue(idx, "error", "duplicate-id", "Alias '%s' is defined twice" % args[0]))
                aliases[args[0]] = idx
            if macro in C4_RELS and len(args) >= 2:
                rels.append((idx, args[0], args[1]))
        else:
            issues.append(issue(idx, "warning", "c4-unknown-macro", "Unknown C4 macro '%s'" % macro))
    for idx, a, b in rels:
        for alias in (a, b):
            if alias not in aliases:
                issues.append(issue(idx, "error", "c4-unknown-alias",
                                    "Relationship refers to undefined alias '%s'" % alias))
    if brace_depth > 0:
        issues.append(issue(len(lines) - 1, "error", "unbalanced-brace", "%d '{' block(s) are not closed" % brace_depth))
    return new_lines, issues


def process_block(lines):
    """Validate one diagram. Returns (new_lines, issues) with idx relative to the block."""
    issues = []
    new_lines = []
    for idx, line in enumerate(lines):
        replaced = line
        for bad, good in SMART_QUOTES.items():
            replaced = replaced.replace(bad, good)
        if replaced != line:
            issues.append(issue(idx, "error", "smart-quote", "Typographic quotes break Mermaid; use ASCII quotes", True))
        new_lines.append(replaced)
    header_idx = next((i for i, ln in enumerate(new_lines) if ln.strip() and not ln.strip().startswith("%%")), None)
    if header_idx is None:
        return new_lines, issues + [issue(0, "error", "empty-diagram", "Diagram is empty")]
    header = new_lines[header_idx].strip()
    if not HEADER_RE.match(header):
        return new_lines, issues + [issue(header_idx, "error", "unknown-header",
                                          "Unknown diagram type '%s'" % header.split()[0])]
    if header.startswith(("flowchart", "graph")):
        new_lines, more = process_flowchart(new_lines, header_idx)
    elif header.startswith("sequenceDiagram"):
        new_lines, more = process_sequence(new_lines, header_idx)
    elif header.startswith("C4"):
        new_lines, more = process_c4(new_lines, header_idx)
    else:
        more = []
    return new_lines, issues + more


def find_blocks(lines):
    """Return list of (first_line_idx, last_line_idx_exclusive) for mermaid fences, or None if no fences."""
    blocks, start = [], None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if start is None and s.lower().startswith("```mermaid"):
            start = i + 1
        elif start is not None and s.startswith("```"):
            blocks.append((start, i))
            start = None
    if start is not None:
        blocks.append((start, len(lines)))
    return blocks or None


def sanitize_text(text, fix):
    """Return (new_text, reported_issues, remaining_errors) for a whole file's text."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.replace("\r\n", "\n").split("\n")
    blocks = find_blocks(lines) or [(0, len(lines))]
    reported = []
    remaining = 0
    out = list(lines)
    for start, stop in blocks:
        block = lines[start:stop]
        new_block, found = process_block(block)
        for f in found:
            f = dict(f)
            f["line"] = start + f["idx"] + 1
            reported.append(f)
        if fix:
            out[start:stop] = new_block
            _, after = process_block(new_block)
            remaining += sum(1 for a in after if a["severity"] == "error")
        else:
            remaining += sum(1 for f in found if f["severity"] == "error")
    return newline.join(out), reported, remaining


def format_issue(path, f, fix):
    tag = f["severity"]
    if fix and f["fixable"]:
        tag = "fixed"
    return "%s:%d: %s [%s] %s" % (path, f["line"], tag, f["rule"], f["message"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Validate and repair Mermaid diagrams.")
    ap.add_argument("files", nargs="*")
    ap.add_argument("--fix", action="store_true", help="rewrite files with auto-fixable problems repaired")
    ap.add_argument("--json", action="store_true", help="print machine-readable results")
    ap.add_argument("--stdin", action="store_true", help="read from stdin; with --fix print repaired text to stdout")
    args = ap.parse_args(argv)
    if not args.files and not args.stdin:
        ap.print_usage(sys.stderr)
        return 2
    results, total_remaining = [], 0
    if args.stdin:
        text = sys.stdin.read()
        new_text, reported, remaining = sanitize_text(text, args.fix)
        results.append({"file": "<stdin>", "issues": reported, "remaining_errors": remaining})
        total_remaining += remaining
        if args.fix and not args.json:
            sys.stdout.write(new_text)
    for path in args.files:
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print("error: cannot read %s: %s" % (path, exc), file=sys.stderr)
            return 2
        new_text, reported, remaining = sanitize_text(text, args.fix)
        if args.fix and new_text != text:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
        results.append({"file": path, "issues": reported, "remaining_errors": remaining})
        total_remaining += remaining
    if args.json:
        json.dump({"results": results, "ok": total_remaining == 0}, sys.stdout, indent=2, ensure_ascii=True)
        sys.stdout.write("\n")
    else:
        stream = sys.stderr if args.stdin and args.fix else sys.stdout
        for r in results:
            for f in r["issues"]:
                print(format_issue(r["file"], f, args.fix), file=stream)
            status = "OK" if r["remaining_errors"] == 0 else "%d error(s) remain" % r["remaining_errors"]
            print("%s: %s" % (r["file"], status), file=stream)
    return 0 if total_remaining == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
