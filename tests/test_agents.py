"""The skill must work the same through every route: skill folders, rules files, function calling, MCP, one command."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_tools
import helpers

ROOT = Path(__file__).resolve().parent.parent
INSTALL = str(ROOT / "install.py")
MCP = str(helpers.SCRIPTS / "mcp_server.py")


def project(tmp_path, name="saas"):
    target = tmp_path / "project"
    shutil.copytree(helpers.FIXTURES / name, target)
    return target


# ---------------------------------------------------------------- run and guide
def test_run_does_the_whole_workflow_in_one_call(tmp_path):
    repo = project(tmp_path)
    code, out = helpers.cli("run", "--repo", repo)
    assert code == 0 and out["status"] == "done" and out["verification"] == "passed"
    assert out["assumed_answers"] is True and set(out["steps"]) == {"scan", "plan", "facts"}
    assert Path(out["zip"]).is_file() and "search the web" in out["next"]


def test_run_respects_explicit_answers_and_remembers_them(tmp_path):
    repo = project(tmp_path)
    code, out = helpers.cli("run", "--repo", repo, "--cloud", "azure", "--compute", "serverless")
    assert code == 0 and out["assumed_answers"] is False
    code, again = helpers.cli("run", "--repo", repo)
    assert code == 0 and again["steps"]["plan"]["cloud"] == "azure" and again["assumed_answers"] is False


def test_run_asks_for_input_instead_of_guessing_on_an_empty_project(tmp_path):
    code, out = helpers.cli("run", "--repo", tmp_path)
    assert code == 3 and out["status"] == "needs_input"


def test_run_reports_a_missing_folder(tmp_path):
    code, out = helpers.cli("run", "--repo", tmp_path / "missing")
    assert code != 0 and out["status"] == "error"


def test_guide_is_machine_readable():
    code, out = helpers.cli("guide")
    assert code == 0 and [s["id"] for s in out["steps"]][:2] == ["questions", "scan"]
    assert any("never" in r.lower() for r in out["rules"]) and "run" in out["shortcut"]


# ------------------------------------------------------------ function calling
def test_schemas_are_consistent_in_every_format():
    names = [t["name"] for t in agent_tools.TOOLS]
    assert len(names) == len(set(names)) and "solsarch_run" in names
    for tool in agent_tools.TOOLS:
        assert set(tool["required"]) <= set(tool["properties"])
        assert tool["description"]
    openai = agent_tools.schema("openai")
    anthropic = agent_tools.schema("anthropic")
    mcp = agent_tools.schema("mcp")
    assert [t["function"]["name"] for t in openai] == names
    assert [t["name"] for t in anthropic] == names and all("input_schema" in t for t in anthropic)
    assert [t["name"] for t in mcp] == names and all("inputSchema" in t for t in mcp)
    json.dumps(openai)


def test_call_rejects_bad_input_without_running_anything(tmp_path):
    assert agent_tools.call("nope")[0] == 2
    assert agent_tools.call("solsarch_plan", {"cloud": "aws"})[0] == 2
    assert agent_tools.call("solsarch_plan", {"cloud": "oracle", "compute": "server"})[0] == 2
    assert agent_tools.call("solsarch_scan", {"repo": "--help"})[0] == 2
    assert agent_tools.call("solsarch_scan", {"surprise": 1})[0] == 2
    assert agent_tools.call("solsarch_plan", {"cloud": "aws", "compute": "server", "assumed": "yes"})[0] == 2
    assert agent_tools.call("solsarch_scan", "not an object")[0] == 2


def test_call_walks_the_workflow_through_the_tools(tmp_path):
    repo = str(project(tmp_path, "rag"))
    code, out = agent_tools.call("solsarch_questions", {"repo": repo})
    assert code == 0 and out["ask"]
    assert agent_tools.call("solsarch_scan", {"repo": repo})[0] == 0
    code, out = agent_tools.call("solsarch_compare", {"repo": repo, "compute": "serverless"})
    assert code == 0 and out["status"] == "written"
    assert agent_tools.call("solsarch_plan", {"repo": repo, "cloud": "gcp", "compute": "serverless"})[0] == 0
    code, out = agent_tools.call("solsarch_facts", {"repo": repo, "action": "queue"})
    assert code == 0
    code, out = agent_tools.call("solsarch_facts", {"repo": repo, "action": "skip", "reason": "no web tool"})
    assert code == 0
    code, out = agent_tools.call("solsarch_finish", {"repo": repo})
    assert code == 0 and out["verification"] == "passed"
    code, out = agent_tools.call("solsarch_verify", {"repo": repo})
    assert code == 0 and out["status"] == "ok"


def test_call_cli_and_schema_cli(tmp_path):
    tool = str(helpers.SCRIPTS / "agent_tools.py")
    listed = subprocess.run([sys.executable, "-I", tool, "list"], capture_output=True, text=True)
    assert listed.returncode == 0 and "solsarch_run" in listed.stdout
    schema = subprocess.run([sys.executable, "-I", tool, "schema", "--format", "anthropic"], capture_output=True, text=True)
    assert json.loads(schema.stdout)[0]["name"] == "solsarch_guide"
    called = subprocess.run([sys.executable, "-I", tool, "call", "solsarch_run", "-"], input=json.dumps({"repo": str(project(tmp_path))}),
                            capture_output=True, text=True)
    assert called.returncode == 0 and json.loads(called.stdout)["status"] == "done"
    bad = subprocess.run([sys.executable, "-I", tool, "call", "solsarch_run", "{broken"], capture_output=True, text=True)
    assert bad.returncode == 2 and json.loads(bad.stdout)["status"] == "error"


# ------------------------------------------------------------------------- MCP
class Mcp:
    def __init__(self):
        self.proc = subprocess.Popen([sys.executable, "-I", "-B", MCP], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8")

    def send(self, message, expect=True):
        text = message if isinstance(message, str) else json.dumps(message)
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline()) if expect else None

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=20) == 0


def test_mcp_handshake_listing_and_calls(tmp_path):
    server = Mcp()
    try:
        init = server.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        assert init["result"]["protocolVersion"] == "2025-03-26" and "tools" in init["result"]["capabilities"]
        assert init["result"]["serverInfo"]["name"] == "solsarch-early-cloud-architect"
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
        assert server.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
        listed = server.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
        assert [t["name"] for t in listed] == [t["name"] for t in agent_tools.TOOLS]
        guide = server.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "solsarch_guide", "arguments": {}}})
        assert guide["result"]["isError"] is False
        assert json.loads(guide["result"]["content"][0]["text"])["steps"]
        repo = str(project(tmp_path))
        run = server.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"name": "solsarch_run", "arguments": {"repo": repo, "cloud": "aws", "compute": "server"}}})
        assert run["result"]["isError"] is False
        assert json.loads(run["result"]["content"][0]["text"])["verification"] == "passed"
        bad = server.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "solsarch_plan", "arguments": {}}})
        assert bad["result"]["isError"] is True
        empty = tmp_path / "empty"
        empty.mkdir()
        needs = server.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                             "params": {"name": "solsarch_run", "arguments": {"repo": str(empty)}}})
        assert needs["result"]["isError"] is False and "needs_input" in needs["result"]["content"][0]["text"]
        assert server.send({"jsonrpc": "2.0", "id": 8, "method": "no/such"})["error"]["code"] == -32601
        assert server.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}})["error"]["code"] == -32602
        assert server.send("{not json")["error"]["code"] == -32700
        server.proc.stdin.write("\n")
        assert server.send({"jsonrpc": "2.0", "id": 10, "method": "ping"})["id"] == 10
    finally:
        server.close()


# ------------------------------------------------------------------ installers
def install(cwd, *args):
    return subprocess.run([sys.executable, "-B", INSTALL, *args], capture_output=True, text=True, cwd=str(cwd))


RULE_FILES = {"cursor": ".cursor/rules/solsarch.mdc", "gemini": "GEMINI.md", "copilot": ".github/copilot-instructions.md",
              "windsurf": ".windsurf/rules/solsarch.md", "generic": "AGENTS.md"}


@pytest.mark.parametrize("agent,relative", sorted(RULE_FILES.items()))
def test_rules_file_agents_get_the_skill_and_an_instruction_file(tmp_path, agent, relative):
    done = install(tmp_path, "--agent", agent)
    assert done.returncode == 0, done.stderr
    skill = tmp_path / ".agents" / "skills" / "solsarch-early-cloud-architect"
    assert (skill / "SKILL.md").is_file() and (skill / "scripts" / "mcp_server.py").is_file()
    text = (tmp_path / relative).read_text(encoding="utf-8")
    assert "<!-- solsarch:begin -->" in text and "scripts/solsarch.py run --repo ." in text
    for path in re.findall(r"`(\.agents/skills/[^`\s]+\.md)`", text):
        assert (tmp_path / path).is_file(), path
    if agent == "cursor":
        assert text.startswith("---\ndescription:")


def test_existing_instruction_files_are_extended_never_replaced(tmp_path):
    (tmp_path / "GEMINI.md").write_text("# Mine\nkeep this line\n", encoding="utf-8")
    assert install(tmp_path, "--agent", "gemini").returncode == 0
    first = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert first.startswith("# Mine\nkeep this line") and first.count("solsarch:begin") == 1
    assert install(tmp_path, "--agent", "gemini", "--force").returncode == 0
    second = (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    assert second == first and second.count("solsarch:begin") == 1


def test_several_agents_share_one_project_copy(tmp_path):
    for agent in ("cursor", "gemini", "copilot", "windsurf", "generic"):
        done = install(tmp_path, "--agent", agent)
        assert done.returncode == 0, (agent, done.stderr)
    for relative in RULE_FILES.values():
        assert (tmp_path / relative).is_file()


def test_codex_uses_the_agents_skills_folder(tmp_path):
    done = install(tmp_path, "--agent", "codex", "--target", "project")
    assert done.returncode == 0, done.stderr
    assert (tmp_path / ".agents" / "skills" / "solsarch-early-cloud-architect" / "SKILL.md").is_file()
    assert not (tmp_path / "AGENTS.md").exists()


def test_list_agents_names_every_agent():
    done = install(ROOT, "--list-agents")
    assert done.returncode == 0
    for name in ("claude", "codex", "cursor", "gemini", "copilot", "windsurf", "generic", "mcp_server.py"):
        assert name in done.stdout


def test_installed_copy_runs_the_whole_workflow_and_the_mcp_server(tmp_path):
    assert install(tmp_path, "--agent", "generic").returncode == 0
    skill = tmp_path / ".agents" / "skills" / "solsarch-early-cloud-architect" / "scripts"
    repo = project(tmp_path)
    done = subprocess.run([sys.executable, "-I", str(skill / "solsarch.py"), "run", "--repo", str(repo)],
                          capture_output=True, text=True)
    assert done.returncode == 0 and json.loads(done.stdout)["verification"] == "passed"
    mcp = subprocess.run([sys.executable, "-I", str(skill / "mcp_server.py")],
                         input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                         capture_output=True, text=True)
    assert len(json.loads(mcp.stdout)["result"]["tools"]) == len(agent_tools.TOOLS)


def test_repository_level_instructions_are_valid():
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "solsarch.py run" in text and "mcp_server.py" in text and "agent_tools.py" in text
    for path in re.findall(r"`(solsarch-early-cloud-architect/[^`\s]+\.py)`", text):
        assert (ROOT / path).is_file(), path
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"
