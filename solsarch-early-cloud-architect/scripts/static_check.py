#!/usr/bin/env python3
"""A built-in check of the generated files, for machines that do not have terraform, hadolint or actionlint installed.

    static_check.py --repo REPO

It reads the generated infrastructure code and looks for the mistakes that make a deploy fail on the first try: unbalanced
blocks, a variable used but never declared, a reference to a resource that does not exist, an outputs file that points at
nothing, a leftover placeholder token, a Dockerfile with no start command or one that starts a file the repository does not
have, and workflow files that are not valid YAML-looking text. It uses only the standard library.

This is NOT a replacement for the real tools. The quality score treats a clean run here as a WARNING (half credit) and only a
real validator run as PASS, and says so in the report.
"""
import argparse
import json
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"@@[a-z_]+@@")
VAR_USE_RE = re.compile(r"\bvar\.([a-zA-Z_][a-zA-Z0-9_-]*)")
VAR_DECL_RE = re.compile(r'^variable\s+"([^"]+)"', re.M)
LOCAL_USE_RE = re.compile(r"\blocal\.([a-zA-Z_][a-zA-Z0-9_-]*)")
LOCAL_DECL_RE = re.compile(r"^\s{2}([a-zA-Z_][a-zA-Z0-9_-]*)\s*=", re.M)
RES_DECL_RE = re.compile(r'^(resource|data)\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_.-]+)"', re.M)
# a reference to another resource: type.name followed by a dot, a bracket or the end of the token
RES_USE_RE = re.compile(r"\b((?:aws|azurerm|google|hcloud|linode|vultr|digitalocean|scaleway|ovh|oci|alicloud|tencentcloud|huaweicloud|ibm|"
                        r"mongodbatlas|neon|random|tls|null|time)_[a-z0-9_]+)\.([A-Za-z_][A-Za-z0-9_-]*)(?=[\.\[\s,)\]}\"]|$)")
DATA_USE_RE = re.compile(r"\bdata\.([a-z0-9_]+)\.([A-Za-z_][A-Za-z0-9_-]*)")
STRING_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
LINE_COMMENT_RE = re.compile(r"(^|\s)(#|//).*$", re.M)


def strip_strings_and_comments(text):
    """Text with comments removed and string contents blanked, so braces inside strings are not counted."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<<-?([A-Z_]+)\n.*?\n\s*\1\n", "<<HEREDOC\n", text, flags=re.S)
    text = STRING_RE.sub('""', text)
    return LINE_COMMENT_RE.sub(r"\1", text)


def balanced(text):
    """(ok, detail) for braces, brackets and parentheses in HCL text."""
    stack, pairs = [], {")": "(", "]": "[", "}": "{"}
    for n, line in enumerate(strip_strings_and_comments(text).split("\n"), 1):
        for ch in line:
            if ch in "([{":
                stack.append((ch, n))
            elif ch in ")]}":
                if not stack or stack[-1][0] != pairs[ch]:
                    return False, "unexpected '%s' on line %d" % (ch, n)
                stack.pop()
    if stack:
        return False, "'%s' opened on line %d is never closed" % stack[-1]
    return True, ""


def check_terraform(files):
    findings = []
    tf = {p: t for p, t in files.items() if p.endswith(".tf")}
    by_dir = {}
    for p, t in tf.items():
        by_dir.setdefault(p.rsplit("/", 1)[0] if "/" in p else "", {})[p] = t
    for folder, group in sorted(by_dir.items()):
        text_all = "\n".join(group[p] for p in sorted(group))
        declared_vars = set(VAR_DECL_RE.findall(text_all))
        declared_locals = set()
        for m in re.finditer(r"^locals\s*\{(.*?)^\}", text_all, re.M | re.S):
            declared_locals |= set(LOCAL_DECL_RE.findall(m.group(1)))
        declared = {(m[1], m[2]) for m in RES_DECL_RE.findall(text_all) if m[0] == "resource"}
        declared_data = {(m[1], m[2]) for m in RES_DECL_RE.findall(text_all) if m[0] == "data"}
        for p in sorted(group):
            text = group[p]
            ok, detail = balanced(text)
            if not ok:
                findings.append({"file": p, "check": "balanced-blocks", "detail": detail})
            if PLACEHOLDER_RE.search(text):
                findings.append({"file": p, "check": "placeholder", "detail": "an unfilled template token remains: %s" % PLACEHOLDER_RE.search(text).group(0)})
            code = strip_strings_and_comments(text)
            interp = "\n".join(re.findall(r"\$\{([^}]*)\}", "\n".join(STRING_RE.findall(text))))
            scan_text = code + "\n" + interp
            for name in sorted(set(VAR_USE_RE.findall(scan_text)) - declared_vars):
                findings.append({"file": p, "check": "undeclared-variable", "detail": "var.%s is used but never declared in %s" % (name, folder or ".")})
            for name in sorted(set(LOCAL_USE_RE.findall(scan_text)) - declared_locals):
                findings.append({"file": p, "check": "undeclared-local", "detail": "local.%s is used but never defined in %s" % (name, folder or ".")})
            for rtype, name in sorted(set(RES_USE_RE.findall(scan_text))):
                if (rtype, name) not in declared and (rtype, name) not in declared_data and not re.search(r"\bdata\.%s\.%s\b" % (rtype, name), scan_text):
                    if rtype in {t for t, _ in declared} or rtype.split("_")[0] in {"aws", "azurerm", "google"}:
                        findings.append({"file": p, "check": "missing-resource", "detail": "%s.%s is referenced but not declared in %s" % (rtype, name, folder or ".")})
            for rtype, name in sorted(set(DATA_USE_RE.findall(scan_text))):
                if (rtype, name) not in declared_data:
                    findings.append({"file": p, "check": "missing-data-source", "detail": "data.%s.%s is referenced but not declared in %s" % (rtype, name, folder or ".")})
        if any("required_providers" in t for t in group.values()) is False and any(p.endswith("main.tf") for p in group) and folder.endswith("terraform"):
            findings.append({"file": folder + "/", "check": "providers", "detail": "no required_providers block"})
    return findings


def check_dockerfiles(files, repo):
    findings = []
    for p, t in sorted(files.items()):
        if not re.search(r"(^|/)Dockerfile[^/]*$", p):
            continue
        if not re.search(r"^FROM\s+\S+", t, re.M):
            findings.append({"file": p, "check": "dockerfile-from", "detail": "no FROM line"})
        if not re.search(r"^(CMD|ENTRYPOINT)\b", t, re.M):
            findings.append({"file": p, "check": "dockerfile-start", "detail": "no CMD or ENTRYPOINT, so the container has nothing to run"})
        if not re.search(r"^USER\s+(?!root\b)\S+", t, re.M):
            findings.append({"file": p, "check": "dockerfile-user", "detail": "the container would run as root"})
        for m in re.finditer(r"uvicorn\s+([\w.]+):(\w+)", t):
            module = m.group(1).replace(".", "/")
            hits = list(Path(repo).rglob(module + ".py")) + list(Path(repo).rglob(module + "/__init__.py"))
            hits = [h for h in hits if ".solsarch" not in h.parts and "node_modules" not in h.parts]
            if not hits:
                findings.append({"file": p, "check": "start-target", "detail": "uvicorn starts %s but no %s.py exists in the repository" % (m.group(0).split()[1], module)})
        if re.search(r"\$\{?[A-Z_]+\}?\s*$", t.split("\n")[0]):
            findings.append({"file": p, "check": "dockerfile-from", "detail": "the base image comes from an unset variable"})
    return findings


def check_workflows(files):
    findings = []
    for p, t in sorted(files.items()):
        if ".github/workflows/" in p or p.startswith("ci/") and p.endswith((".yml", ".yaml")):
            if "\t" in t.replace("\n\t", "\n  ") and re.search(r"^\t", t, re.M):
                findings.append({"file": p, "check": "yaml-tabs", "detail": "a tab character indents a line; YAML needs spaces"})
            if not re.search(r"^on:|^\"on\":|^'on':", t, re.M):
                findings.append({"file": p, "check": "workflow-trigger", "detail": "no 'on:' trigger"})
            if not re.search(r"^jobs:", t, re.M):
                findings.append({"file": p, "check": "workflow-jobs", "detail": "no 'jobs:' section"})
    return findings


def check_outputs(files):
    findings = []
    for p, t in sorted(files.items()):
        if p.endswith("outputs.tf") and not re.search(r'^output\s+"', t, re.M):
            findings.append({"file": p, "check": "outputs", "detail": "the outputs file declares no output"})
    return findings


def load(repo):
    root = Path(repo) / ".solsarch" / "iac"
    files = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if p.is_file() and ".terraform" not in p.parts and p.stat().st_size < 400000:
                try:
                    files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
    return files


def run(repo):
    files = load(repo)
    if not files:
        return {"verdict": "not_run", "message": "No generated infrastructure code found. Run the iac step first.", "findings": [], "files": 0}
    findings = check_terraform(files) + check_dockerfiles(files, repo) + check_workflows(files) + check_outputs(files)
    findings.sort(key=lambda f: (f["file"], f["check"], f["detail"]))
    return {"verdict": "clean" if not findings else "findings", "files": len(files), "findings": findings,
            "note": "Built-in check only. It does not replace terraform validate, hadolint or actionlint."}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Built-in static check of the generated infrastructure code")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    result = run(args.repo)
    (Path(args.repo) / ".solsarch").mkdir(parents=True, exist_ok=True)
    (Path(args.repo) / ".solsarch" / "static-check.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "clean" else 1


if __name__ == "__main__":
    sys.exit(main())
