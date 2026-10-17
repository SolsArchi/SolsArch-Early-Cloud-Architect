#!/usr/bin/env python3
"""Install the SolsArch Early Cloud Architect skill, or build a zip for upload.

    python install.py                        copy to ~/.claude/skills (Claude Code, all projects)
    python install.py --target project       copy to ./.claude/skills (Claude Code, this project)
    python install.py --dest PATH            copy into any skills folder (for example Codex)
    python install.py --zip dist/skill.zip   build a zip for Claude.ai Settings > Capabilities > Skills

Existing installs are only replaced with --force. The skill is validated before anything
is copied. Standard library only. Exit codes: 0 done, 1 refused or failed.
"""

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL_NAME = "solsarch-early-cloud-architect"
SOURCE = HERE / SKILL_NAME
IGNORED = shutil.ignore_patterns("__pycache__", "*.pyc", ".solsarch")

sys.path.insert(0, str(SOURCE / "scripts"))
sys.dont_write_bytecode = True
import validate_skill  # noqa: E402


def fail(message):
    sys.stderr.write("error: %s\n" % message)
    return 1


def source_files():
    for path in sorted(SOURCE.rglob("*")):
        relative = path.relative_to(SOURCE)
        if path.is_file() and not any(part in ("__pycache__", ".solsarch") for part in relative.parts) \
                and path.suffix != ".pyc":
            yield path, relative


def build_zip(target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, relative in source_files():
            info = zipfile.ZipInfo("%s/%s" % (SKILL_NAME, relative.as_posix()), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    return target


def install(skills_dir, force):
    skills_dir = Path(skills_dir).expanduser()
    destination = skills_dir / SKILL_NAME
    if destination.exists():
        if not force:
            return None, "%s already exists. Use --force to replace it." % destination
        shutil.rmtree(destination)
    skills_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SOURCE, destination, ignore=IGNORED)
    return destination, None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install the SolsArch Early Cloud Architect skill")
    parser.add_argument("--target", choices=("user", "project"), default="user",
                        help="user = ~/.claude/skills (default), project = ./.claude/skills")
    parser.add_argument("--dest", help="Skills folder to copy into (overrides --target)")
    parser.add_argument("--zip", metavar="FILE", help="Build a zip instead of installing")
    parser.add_argument("--force", action="store_true", help="Replace an existing install")
    args = parser.parse_args(argv)

    problems = validate_skill.validate(SOURCE)
    if problems:
        return fail("the skill failed validation: " + "; ".join(problems))
    if args.zip:
        sys.stdout.write("Built %s\nUpload it in Claude.ai under Settings, Capabilities, Skills.\n" % build_zip(args.zip))
        return 0
    if args.dest:
        skills_dir = Path(args.dest)
    elif args.target == "project":
        skills_dir = Path.cwd() / ".claude" / "skills"
    else:
        skills_dir = Path.home() / ".claude" / "skills"
    try:
        destination, refusal = install(skills_dir, args.force)
    except OSError as error:
        return fail("could not copy the skill: %s" % error)
    if refusal:
        return fail(refusal)
    sys.stdout.write("Installed to %s\nStart a new agent session, then ask: "
                     "\"Design the cloud architecture for this project.\"\n" % destination)
    return 0


if __name__ == "__main__":
    sys.exit(main())
