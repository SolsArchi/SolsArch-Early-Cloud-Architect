#!/usr/bin/env python3
"""Look things up in the architecture reference library, the same way on every run.

    knowledge.py chapters                      list the 16 chapters
    knowledge.py search "pod disruption budget" [--limit 5]
    knowledge.py section 10 "state locking"    print the matching section of chapter 10
    knowledge.py playbooks                     list the specialist-agent playbooks (IaC, Ansible, security, cost, ...)

Search is plain word matching with fixed weights and a fixed tie-break, so the same question always returns the same
sections in the same order. Standard library only.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REFS = HERE.parent / "references"
LIBRARY = REFS / "knowledge"
PLAYBOOKS = REFS / "agents"
STOP = set("a an and are as at be by for from in is it of on or that the this to with how what when which why do does i you we".split())
_CACHE = {}


def with_utf8():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def words(text):
    return [w for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", text.lower()) if w not in STOP and len(w) > 1]


def chapters():
    out = []
    for path in sorted(LIBRARY.glob("[0-9][0-9]_*.md")):
        first = path.read_text(encoding="utf-8").split("\n", 1)[0].lstrip("# ").strip()
        out.append({"chapter": path.name[:2], "file": path.name, "title": first})
    return out


def sections(path):
    """Split a markdown file into (heading, body) sections at level 2 and 3 headings."""
    if path in _CACHE:
        return _CACHE[path]
    out, heading, body = [], Path(path).stem, []
    for line in Path(path).read_text(encoding="utf-8").split("\n"):
        if re.match(r"^#{2,3} ", line):
            if body:
                out.append((heading, "\n".join(body).strip()))
            heading, body = line.lstrip("# ").strip(), []
        else:
            body.append(line)
    if body:
        out.append((heading, "\n".join(body).strip()))
    _CACHE[path] = out
    return out


def search(query, limit=5, folders=(LIBRARY, PLAYBOOKS)):
    terms = words(query)
    if not terms:
        return []
    hits = []
    for folder in folders:
        for path in sorted(Path(folder).glob("*.md")):
            if path.name == "index.md":
                continue
            for heading, body in sections(path):
                head_words, body_words = words(heading), words(body)
                if not body_words:
                    continue
                score = 0.0
                for term in set(terms):
                    score += 5.0 * head_words.count(term) + min(body_words.count(term), 6)
                    if term in head_words:
                        score += 3.0
                if score <= 0 or len({t for t in terms if t in body_words or t in head_words}) < max(1, (len(set(terms)) + 1) // 2):
                    continue
                hits.append((-round(score, 3), path.name, heading, body))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    return [{"score": -h[0], "file": h[1], "heading": h[2], "excerpt": excerpt(h[3], terms)} for h in hits[:limit]]


def excerpt(body, terms, size=600):
    lower = body.lower()
    positions = [lower.find(t) for t in terms if lower.find(t) >= 0]
    start = max(0, min(positions) - 80) if positions else 0
    text = body[start:start + size].strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def section(chapter, topic=None):
    for path in sorted(LIBRARY.glob("%02d_*.md" % int(chapter))):
        if not topic:
            return {"file": path.name, "sections": [h for h, _ in sections(path)]}
        t = words(topic)
        best = max(sections(path), key=lambda s: (sum(1 for w in t if w in words(s[0])) * 10 + sum(1 for w in t if w in words(s[1])), s[0]))
        return {"file": path.name, "heading": best[0], "text": best[1]}
    return None


def playbooks():
    return [{"name": p.stem, "file": p.name, "bytes": p.stat().st_size} for p in sorted(PLAYBOOKS.glob("*.md"))]


def main(argv=None):
    with_utf8()
    parser = argparse.ArgumentParser(description="Search the architecture reference library")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chapters")
    sub.add_parser("playbooks")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p = sub.add_parser("section")
    p.add_argument("chapter", type=int)
    p.add_argument("topic", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "chapters":
        out = {"chapters": chapters()}
    elif args.command == "playbooks":
        out = {"playbooks": playbooks()}
    elif args.command == "search":
        out = {"query": args.query, "results": search(args.query, args.limit)}
    else:
        out = section(args.chapter, args.topic) or {"error": "no such chapter"}
    sys.stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
