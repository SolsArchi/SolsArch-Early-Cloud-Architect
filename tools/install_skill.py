#!/usr/bin/env python3
"""Install the skill into your AI agent's skills folder.

Usage: python tools/install_skill.py [--target claude|agents|DIR] [--force]
  --target claude   ~/.claude/skills   (Claude Code; default)
  --target agents   ~/.agents/skills   (Codex and other Agent Skills hosts)
  --target DIR      any folder
Refuses to overwrite an existing install unless --force is given.
Exit codes: 0 installed, 1 already installed, 2 error.
"""
import argparse
import os
import shutil
import sys

SKILL = "solsarch-early-cloud-architect"
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".solsarch")


def resolve_target(target, home=None):
    home = home or os.path.expanduser("~")
    if target == "claude":
        return os.path.join(home, ".claude", "skills")
    if target == "agents":
        return os.path.join(home, ".agents", "skills")
    return os.path.abspath(target)


def install(target_dir, force=False, repo=None):
    repo = repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(repo, SKILL)
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        raise FileNotFoundError("SKILL.md not found in %s" % src)
    dest = os.path.join(target_dir, SKILL)
    if os.path.exists(dest):
        if not force:
            return None
        shutil.rmtree(dest)
    os.makedirs(target_dir, exist_ok=True)
    shutil.copytree(src, dest, ignore=IGNORE)
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Install the SolsArch Early Cloud Architect skill.")
    ap.add_argument("--target", default="claude", help="claude, agents, or a folder path")
    ap.add_argument("--force", action="store_true", help="replace an existing install")
    args = ap.parse_args(argv)
    try:
        dest = install(resolve_target(args.target), args.force)
    except (OSError, FileNotFoundError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if dest is None:
        print("already installed; rerun with --force to replace it", file=sys.stderr)
        return 1
    print("installed to %s" % dest)
    print("Start a new agent session and ask: How should I deploy this?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
