#!/usr/bin/env python3
"""Look for secrets that were committed once and later removed.

A secret deleted from the files is still in the repository's history, and anyone who can clone can read it. This reads the added
lines of the most recent commits with git and applies the same secret patterns the file scan uses. Values are never printed or
stored: a finding holds the short commit id, the file, the kind of secret and nothing else.

It needs git and a repository. When either is missing it says so ("not_run") and reports nothing, never a clean result.
Bounded: the newest 300 commits and 12 MB of patch text, so a large repository cannot stall the scan.
"""
import re
import shutil
import subprocess
from pathlib import Path

MAX_COMMITS = 300
MAX_BYTES = 12_000_000
TIMEOUT = 60
TEXT_SKIP = re.compile(r"\.(png|jpe?g|gif|ico|svg|pdf|zip|gz|woff2?|ttf|lock|map|min\.js)$", re.I)


def run_git(root, args):
    return subprocess.run(["git", "-C", str(root)] + args, capture_output=True, timeout=TIMEOUT)


def scan(root, patterns, generic, db_url, is_placeholder, placeholder_passwords=()):
    """{"status", "reason", "commits", "truncated", "hits": [{"commit", "file", "label"}]} for the repository at root.

    patterns: [(label, compiled regex)]; generic and db_url: compiled regexes with the secret in group 1; is_placeholder: callable."""
    root = Path(root)
    if not shutil.which("git"):
        return {"status": "not_run", "reason": "git is not installed", "commits": 0, "truncated": False, "hits": []}
    if not (root / ".git").exists():
        return {"status": "not_run", "reason": "the folder is not the top of a git repository", "commits": 0, "truncated": False, "hits": []}
    try:
        counted = run_git(root, ["rev-list", "--count", "--all"])
        total = int((counted.stdout or b"0").decode().strip() or 0) if counted.returncode == 0 else 0
        done = run_git(root, ["log", "-n", str(MAX_COMMITS), "--all", "--no-color", "--no-ext-diff", "-U0", "--diff-filter=AM",
                              "--format=@@commit %h", "-p", "--", "."])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"status": "not_run", "reason": "git could not be read (%s)" % type(error).__name__, "commits": 0, "truncated": False, "hits": []}
    if done.returncode != 0:
        return {"status": "not_run", "reason": "git log failed", "commits": 0, "truncated": False, "hits": []}
    data = done.stdout[:MAX_BYTES]
    truncated = len(done.stdout) > MAX_BYTES or total > MAX_COMMITS
    commit, path, seen, hits, commits = "", "", set(), [], 0
    for raw in data.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("@@commit "):
            commit, path = raw[9:].strip(), ""
            commits += 1
        elif raw.startswith("+++ "):
            path = raw[6:] if raw.startswith("+++ b/") else ""
        elif raw.startswith("+") and not raw.startswith("+++") and path and not TEXT_SKIP.search(path):
            line = raw[1:]
            label = None
            for name, rx in patterns:
                if rx.search(line):
                    label = name
                    break
            if label is None:
                m = generic.search(line)
                if m and not is_placeholder(m.group(1)):
                    label = "Hardcoded credential assignment"
            if label is None:
                m = db_url.search(line)
                if m:
                    pw = m.group(1)
                    if (pw.lower() not in placeholder_passwords and "${" not in pw and "{{" not in pw
                            and not pw.startswith(("$", "%", "<")) and not is_placeholder(pw)):
                        label = "Database URL with embedded password"
            if label and (path, label) not in seen:
                seen.add((path, label))
                hits.append({"commit": commit, "file": path, "label": label,
                             "in_working_tree": (root / path).exists()})
    return {"status": "ran", "reason": "", "commits": commits, "truncated": bool(truncated), "hits": hits}
