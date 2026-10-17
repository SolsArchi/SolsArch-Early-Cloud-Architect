"""Shared test helpers. Fixtures are built in temp directories so no fake secret is ever committed."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # keep __pycache__ out of the skill folder

SKILL_DIR = Path(__file__).resolve().parent.parent / "solsarch-early-cloud-architect"
SCRIPTS = SKILL_DIR / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def write(root, relative, content):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def fake_aws_key():
    return "AKIA" + "ABCDEFGHIJKLMNOP"


def build_saas(root):
    write(root, "package.json", """{
  "name": "shop",
  "dependencies": {
    "next": "14.2.0",
    "react": "18.3.0",
    "pg": "8.11.0",
    "ioredis": "5.3.0",
    "bullmq": "5.0.0"
  }
}
""")
    write(root, "Dockerfile", "FROM node:20-alpine\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n")
    write(root, "docker-compose.yml", """services:
  db:
    image: postgres:16
    ports:
      - "5432:5432"
  cache:
    image: redis:7
""")


def build_rag(root):
    write(root, "requirements.txt", "fastapi==0.110.0\nopenai>=1.0\nchromadb\n")
    write(root, "app/main.py", """import sqlite3
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
db = sqlite3.connect("data.db")
""")
    write(root, "deploy/nginx.conf", """server {
  listen 80;
  location / {
    proxy_pass http://app:8000;
  }
}
""")
    write(root, "Dockerfile", "FROM python:3.12\nCMD [\"uvicorn\", \"app.main:app\"]\n")
    write(root, "config.py", 'AWS_KEY = "%s"\n' % fake_aws_key())


def build_terraform(root):
    write(root, "main.tf", """resource "aws_cloudfront_distribution" "cdn" {
  enabled = true
}

resource "aws_ecs_service" "api" {
  name = "api"
}

resource "aws_security_group" "db" {
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "main" {
  engine              = "postgres"
  publicly_accessible = true
}
""")
    write(root, "policy.json", '{"Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}\n')


def build_demo(root):
    """RAG app with a Next.js front end: the standard end-to-end fixture."""
    build_rag(root)
    write(root, "web/package.json", '{"dependencies": {"next": "14.2.0", "react": "18.3.0"}}\n')


def cli(*args):
    """Run scripts/solsarch.py and return (exit code, parsed JSON output)."""
    import json
    import subprocess
    done = subprocess.run([sys.executable, "-I", "-B", str(SCRIPTS / "solsarch.py"), *map(str, args)],
                          capture_output=True, text=True)
    try:
        return done.returncode, json.loads(done.stdout)
    except ValueError:
        raise AssertionError("not JSON (exit %s): %s %s" % (done.returncode, done.stdout, done.stderr))


def pipeline(repo, cloud="aws", mode="server", assumed=False):
    """scan -> plan -> finish. Returns the output of finish."""
    code, out = cli("scan", "--repo", repo)
    assert code == 0, out
    extra = ["--assumed"] if assumed else []
    code, out = cli("plan", "--repo", repo, "--cloud", cloud, "--compute", mode, *extra)
    assert code == 0, out
    code, out = cli("facts", "skip", "--repo", repo, "--reason", "test run")
    assert code == 0, out
    code, out = cli("finish", "--repo", repo)
    assert code == 0, out
    return out


# ---- flat-evidence helpers for the placement engine, the verifier and the packagers
FIXTURES = Path(__file__).resolve().parent / "fixtures"
LONG = ("This part of the codebase needs the service because the cited files show the dependency, so the team "
        "gets managed hosting, backups and scaling without running servers, and the alternative was weighed.")
DIAGRAM = '```mermaid\nflowchart TD\n  a["A"] --> b["B"]\n```\n'
DIAGRAM_TEXT = ("The diagram shows the user reaching the application through the entry point, then the data tier, "
                "and each box maps to a numbered service section above, chosen from the evidence found in the repo, so a reader "
                "can follow every arrow from the first request to stored data.")


def evidence_for(fixture):
    """Flat numbered evidence (E001, ...) for a fixture repository under tests/fixtures."""
    import ast_scanner
    import evidence_bridge
    return evidence_bridge.flatten(ast_scanner.scan(FIXTURES / fixture))


def placements_for(fixture, cloud="aws", model="server"):
    import json
    import place_services
    with open(place_services.DEFAULT_RULES, encoding="utf-8") as fh:
        rules = json.load(fh)
    return place_services.place(evidence_for(fixture), rules, cloud, model)


def make_report(placements, research_ids=()):
    """A report that satisfies every check; tests then break one thing at a time."""
    lines = ["# Review", "", "## Blockers", "| Rule | Finding | Evidence | Fix |", "| :--- | :--- | :--- | :--- |",
             "| SEC-001 | design check | design | ALB |", "", "## Services",
             "| ID | Service | Category | Evidence | Confidence | Why |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for p in placements["placements"]:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (p["id"], p["service"], p["category"],
                                                          ", ".join(p["evidence_ids"]), p["confidence"], "short"))
    lines.append("")
    for p in placements["placements"]:
        lines += ["### %s %s" % (p["id"], p["service"]), LONG, ""]
    lines += ["## Diagrams", "### Infrastructure", DIAGRAM, DIAGRAM_TEXT, ""]
    lines += ["## Sources"]
    if research_ids:
        lines.append("Sources: " + ", ".join(research_ids))
    else:
        lines.append("Web verification: not performed")
    return "\n".join(lines) + "\n"
