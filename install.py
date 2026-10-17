#!/usr/bin/env python3
"""Install the SolsArch Early Cloud Architect skill, or build a zip for upload.

    python install.py                        copy to ~/.claude/skills (Claude Code, all projects)
    python install.py --target project       copy to ./.claude/skills (Claude Code, this project)
    python install.py --dest PATH            copy into any skills folder (for example Codex)
    python install.py --zip dist/skill.zip   build a zip for Claude.ai Settings > Capabilities > Skills
    python install.py --agent codex          Codex: ~/.agents/skills (add --target project for ./.agents/skills)
    python install.py --agent cursor         Cursor, Gemini CLI, Copilot, Windsurf or any agent that reads a rules file:
                                             copies the skill to ./.agents/skills and writes the agent's instruction
                                             file in the current project (an existing file is extended, never replaced)
    python install.py --agent generic        only ./.agents/skills and AGENTS.md (the open standard many agents read)
    python install.py --list-agents          show every supported agent

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


BEGIN = "<!-- solsarch:begin -->"
END = "<!-- solsarch:end -->"
SKILLS_AGENTS = ("claude", "codex")
# agent -> (instruction file relative to the project, front matter or None, description)
RULE_FILES = {
    "cursor": (".cursor/rules/solsarch.mdc", "---\ndescription: Design cloud architecture for this repository with SolsArch "
               "(AWS, Azure, GCP)\nalwaysApply: false\n---\n", "Cursor rule"),
    "gemini": ("GEMINI.md", None, "Gemini CLI context file"),
    "copilot": (".github/copilot-instructions.md", None, "GitHub Copilot instructions"),
    "windsurf": (".windsurf/rules/solsarch.md", None, "Windsurf rule"),
    "generic": ("AGENTS.md", None, "AGENTS.md (read by Codex, Cursor, Copilot, Gemini CLI and others)"),
}
AGENT_CHOICES = SKILLS_AGENTS + tuple(RULE_FILES)


def instruction_text(skill_path):
    run = "python %s/scripts/solsarch.py" % skill_path
    return "\n".join([
        BEGIN,
        "## SolsArch: cloud architecture design",
        "",
        "When the user asks to design cloud architecture, choose AWS/Azure/GCP services, draw architecture or C4 diagrams, "
        "check deployment blockers or plan a first deployment, use the SolsArch skill in `%s`. "
        "Read `%s/SKILL.md` for the full rules." % (skill_path, skill_path),
        "",
        "Fast path (one command, nothing is guessed by the model):",
        "",
        "```bash",
        "%s run --repo . --cloud aws --compute server" % run,
        "```",
        "",
        "Step by step: `%s questions|scan|plan|facts|finish --repo .` (run `%s guide` for the JSON workflow)." % (run, run),
        "",
        "Rules: ask the user only which cloud (aws, azure, gcp) and server or serverless; never choose, rename or remove "
        "services yourself; never edit the generated report or diagrams; never print secrets or state prices. "
        "Outputs are written to `docs/` (report, diagrams, `solsarch-output.zip`).",
        END,
        "",
    ])


def write_instructions(project, agent, skill_path):
    """Create or extend the agent's instruction file. Returns (path, action)."""
    relative, front, _ = RULE_FILES[agent]
    target = Path(project) / relative
    block = instruction_text(skill_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(((front or "") + block).encode("utf-8"))
        return target, "created"
    text = target.read_text(encoding="utf-8")
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        target.write_bytes((head + block + tail).encode("utf-8"))
        return target, "updated"
    target.write_bytes((text.rstrip("\n") + "\n\n" + block).encode("utf-8"))
    return target, "extended"


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
                        help="user = home folder (default), project = this project's folder")
    parser.add_argument("--agent", choices=AGENT_CHOICES, default="claude",
                        help="claude (default), codex, or an agent that reads a rules file: "
                             + ", ".join(RULE_FILES))
    parser.add_argument("--list-agents", action="store_true", help="Show supported agents and where files go")
    parser.add_argument("--dest", help="Skills folder to copy into (overrides --target)")
    parser.add_argument("--zip", metavar="FILE", help="Build a zip instead of installing")
    parser.add_argument("--force", action="store_true", help="Replace an existing install")
    args = parser.parse_args(argv)

    if args.list_agents:
        sys.stdout.write("claude    ~/.claude/skills (or ./.claude/skills with --target project)\n"
                         "codex     ~/.agents/skills (or ./.agents/skills with --target project)\n")
        for name, (relative, _, text) in RULE_FILES.items():
            sys.stdout.write("%-9s ./.agents/skills + %s (%s)\n" % (name, relative, text))
        sys.stdout.write("Any other agent: run `python install.py --agent generic`, or use scripts/mcp_server.py "
                         "(MCP) or scripts/agent_tools.py (function-calling schemas).\n")
        return 0
    problems = validate_skill.validate(SOURCE)
    if problems:
        return fail("the skill failed validation: " + "; ".join(problems))
    if args.zip:
        sys.stdout.write("Built %s\nUpload it in Claude.ai under Settings, Capabilities, Skills.\n" % build_zip(args.zip))
        return 0
    project_scope = args.agent in RULE_FILES or args.target == "project"
    folder = ".claude" if args.agent == "claude" else ".agents"
    if args.dest:
        skills_dir = Path(args.dest)
    elif project_scope:
        skills_dir = Path.cwd() / folder / "skills"
    else:
        skills_dir = Path.home() / folder / "skills"
    try:
        destination, refusal = install(skills_dir, args.force)
    except OSError as error:
        return fail("could not copy the skill: %s" % error)
    if refusal and args.agent in RULE_FILES and (skills_dir / SKILL_NAME / "SKILL.md").is_file():
        destination = skills_dir / SKILL_NAME  # several agents can share one project copy of the skill
        sys.stdout.write("Skill already installed at %s; keeping it (use --force to replace)\n" % destination)
    elif refusal:
        return fail(refusal)
    else:
        sys.stdout.write("Installed to %s\n" % destination)
    if destination is None:
        return fail("the skill could not be installed")
    if args.agent in RULE_FILES:
        try:
            relative = destination.relative_to(Path.cwd()).as_posix()
        except ValueError:
            relative = destination.as_posix()
        written, action = write_instructions(Path.cwd(), args.agent, relative)
        sys.stdout.write("%s %s\n" % (action.capitalize(), written))
    sys.stdout.write("Start a new agent session, then ask: \"Design the cloud architecture for this project.\"\n"
                     "No skill support? Run `python %s/scripts/solsarch.py guide`, or register "
                     "`%s/scripts/mcp_server.py` as an MCP server.\n" % (destination.as_posix(), destination.as_posix()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
