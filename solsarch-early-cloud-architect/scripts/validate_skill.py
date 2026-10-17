#!/usr/bin/env python3
"""Check a skill folder against the Agent Skills packaging rules.

Checks: exact SKILL.md name, YAML frontmatter delimiters, kebab-case name that matches
the folder, no reserved words in the name, description present and under 1024 characters,
no angle brackets in the frontmatter, no README.md inside the skill, SKILL.md under 500
lines, and that every file path mentioned in SKILL.md exists.

Standard library only. Usage:  python validate_skill.py [SKILL_DIR]
Exit code 0 when valid, 1 when problems were found, 2 on usage errors.
"""

import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESERVED = ("claude", "anthropic")
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_BODY_LINES = 500
PATH_MENTION_RE = re.compile(r"`((?:references|scripts|assets|evals)/[A-Za-z0-9_.\-/]+)`")


def parse_frontmatter(text):
    """Return (fields, body, error). Handles plain, quoted and folded block scalars."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, text, "SKILL.md must start with a '---' line."
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, text, "Frontmatter is not closed with a second '---' line."
    fields, index = {}, 1
    while index < end:
        line = lines[index]
        match = re.match(r"^([A-Za-z_][\w\-]*):\s*(.*)$", line)
        if not match:
            index += 1
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in (">", ">-", ">+", "|", "|-", "|+"):
            block = []
            index += 1
            while index < end and (lines[index].startswith((" ", "\t")) or not lines[index].strip()):
                block.append(lines[index].strip())
                index += 1
            joiner = "\n" if value.startswith("|") else " "
            fields[key] = joiner.join(part for part in block if part)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
        index += 1
    return fields, "\n".join(lines[end + 1:]), None


def validate(skill_dir):
    skill_dir = Path(skill_dir)
    problems = []
    if not skill_dir.is_dir():
        return ["%s is not a directory." % skill_dir]
    names = {p.name for p in skill_dir.iterdir()}
    if "SKILL.md" not in names:
        similar = [n for n in names if n.lower() == "skill.md"]
        problems.append("SKILL.md not found%s." % (" (found %s; the name is case-sensitive)" % similar[0] if similar else ""))
        return problems
    if any(n.lower() == "readme.md" for n in names):
        problems.append("README.md must not be inside the skill folder; keep it at the repository level.")
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    fields, body, error = parse_frontmatter(text)
    if error:
        problems.append(error)
        return problems
    name = fields.get("name", "")
    if not name:
        problems.append("Frontmatter is missing 'name'.")
    else:
        if not NAME_RE.match(name):
            problems.append("name '%s' must be kebab-case (lowercase letters, digits, hyphens)." % name)
        if any(word in name for word in RESERVED):
            problems.append("name '%s' contains a reserved word." % name)
        if name != skill_dir.name:
            problems.append("name '%s' does not match the folder name '%s'." % (name, skill_dir.name))
    description = fields.get("description", "")
    if not description:
        problems.append("Frontmatter is missing 'description'.")
    elif len(description) > MAX_DESCRIPTION:
        problems.append("description is %d characters; the limit is %d." % (len(description), MAX_DESCRIPTION))
    elif not re.search(r"\buse (?:when|this|it)\b", description, re.I):
        problems.append("description should say when to use the skill (for example 'Use when ...').")
    compatibility = fields.get("compatibility", "")
    if compatibility and not 1 <= len(compatibility) <= MAX_COMPATIBILITY:
        problems.append("compatibility must be 1 to %d characters." % MAX_COMPATIBILITY)
    frontmatter_text = text.split("\n---", 2)[0] if text.startswith("---") else ""
    if "<" in frontmatter_text or ">" in frontmatter_text.replace(": >", ":").replace(": >-", ":"):
        problems.append("Frontmatter must not contain angle brackets.")
    body_lines = len(body.strip().split("\n"))
    if body_lines > MAX_BODY_LINES:
        problems.append("SKILL.md body has %d lines; keep it under %d." % (body_lines, MAX_BODY_LINES))
    for mention in sorted(set(PATH_MENTION_RE.findall(body))):
        if not (skill_dir / mention).exists():
            problems.append("SKILL.md mentions '%s' but that file does not exist." % mention)
    return problems


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        sys.stderr.write("usage: validate_skill.py [SKILL_DIR]\n")
        return 2
    skill_dir = Path(argv[0]) if argv else Path(__file__).resolve().parent.parent
    problems = validate(skill_dir)
    sys.stdout.write(json.dumps({"skill": str(skill_dir), "valid": not problems, "problems": problems}, indent=2) + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
