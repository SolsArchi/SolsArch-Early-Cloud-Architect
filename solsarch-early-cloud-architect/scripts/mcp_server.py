#!/usr/bin/env python3
"""SolsArch as a Model Context Protocol (MCP) server over stdio.

Standard library only. Any MCP-capable agent (Claude Desktop and Claude Code, Codex, Cursor, Windsurf, Gemini CLI,
VS Code and custom hosts) can list and call the SolsArch tools. Messages are JSON-RPC 2.0, one per line.

Register it with your agent, for example:
  command: python        args: ["/path/to/solsarch-early-cloud-architect/scripts/mcp_server.py"]
Claude Code:  claude mcp add solsarch -- python /path/to/solsarch-early-cloud-architect/scripts/mcp_server.py

Logs go to stderr only; stdout carries protocol messages.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_tools  # noqa: E402

SERVER_INFO = {"name": "solsarch-early-cloud-architect", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2024-11-05"
INSTRUCTIONS = ("Designs cloud architecture for a local repository. Call solsarch_guide for the workflow, "
                "or solsarch_run for the whole workflow in one call. Ask the user only which cloud and whether "
                "server or serverless. Never edit the generated report or diagrams.")


def reply(message_id, result=None, error=None):
    body = {"jsonrpc": "2.0", "id": message_id}
    if error is not None:
        body["error"] = error
    else:
        body["result"] = result
    return body


def handle(message):
    """Return a response dict, or None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return reply(None, error={"code": -32600, "message": "invalid request"})
    method, message_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if message_id is None:
        return None  # notifications such as notifications/initialized need no answer
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, dict) else None
        return reply(message_id, {"protocolVersion": asked or DEFAULT_PROTOCOL, "capabilities": {"tools": {}},
                                  "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "ping":
        return reply(message_id, {})
    if method == "tools/list":
        return reply(message_id, {"tools": agent_tools.schema("mcp")})
    if method == "tools/call":
        if not isinstance(params, dict) or "name" not in params:
            return reply(message_id, error={"code": -32602, "message": "tools/call needs a name"})
        code, payload = agent_tools.call(params["name"], params.get("arguments"))
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        return reply(message_id, {"content": [{"type": "text", "text": text}],
                                  "isError": code != 0 and payload.get("status") not in ("needs_input",)})
    if method in ("resources/list", "prompts/list"):
        return reply(message_id, {method.split("/")[0]: []})
    return reply(message_id, error={"code": -32601, "message": "method not found: %s" % method})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = reply(None, error={"code": -32700, "message": "parse error"})
        else:
            try:
                response = handle(message)
            except Exception as error:  # a bad request must not stop the server
                sys.stderr.write("solsarch mcp: %s%s" % (error, chr(10)))
                response = reply(message.get("id") if isinstance(message, dict) else None,
                                 error={"code": -32603, "message": "internal error"})
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + chr(10))
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
