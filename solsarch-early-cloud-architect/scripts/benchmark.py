#!/usr/bin/env python3
"""A public Code-to-Cloud benchmark: 20 generated repositories in five categories, scored the same way every time.

    benchmark.py run     [--cloud aws] [--compute server] [--out FILE]      build every fixture, run the pipeline, write the metrics
    benchmark.py compare RESULT.json [RESULT.json ...]                      put results side by side (yours and ones supplied by others)
    benchmark.py list                                                       the fixtures

Each fixture is a small repository written to a temporary folder, so nothing is stored and no fake secret is committed. The pipeline runs
offline (no prices, no web), so the numbers describe the design and its checks, not live prices. The metrics are deterministic:
the same SolsArch version gives the same table. Wall time is recorded separately and is the only value that varies.

`compare` merges any result file with the same fields. This is how a run by a frontier model alone, or by an engineer by hand, can be set
beside SolsArch: fill the same fields from your own review. The kit proves nothing about other workflows by itself, and a clean static
check is not production success. Standard library only.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "solsarch.py"

CATEGORIES = ("simple_fullstack", "database_intensive", "ai_agentic", "microservices", "existing_infrastructure")
DOCKER = "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\nEXPOSE 8000\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n"
NODE_DOCKER = "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\nCMD [\"node\", \"server.js\"]\n"
FASTAPI = "from fastapi import FastAPI\napp = FastAPI()\n\n\n@app.get('/health')\ndef health():\n    return {'ok': True}\n"
EXPRESS = "const express = require('express');\nconst app = express();\napp.get('/health', (req, res) => res.json({ ok: true }));\napp.listen(3000);\n"
SSE = ("from fastapi.responses import StreamingResponse\nfrom app.main import app\n\n\n@app.get('/chat')\ndef chat():\n"
       "    def gen():\n        for i in range(3):\n            yield 'data: %d\\n\\n' % i\n    return StreamingResponse(gen(), media_type='text/event-stream')\n")
PKG = '{"name": "%s", "version": "1.0.0", "dependencies": {%s}}\n'


def pkg(name, *deps):
    return PKG % (name, ", ".join('"%s": "*"' % d for d in deps))


FIXTURES = {
    "simple_fullstack": {
        "sf1-flask-api": {"requirements.txt": "flask==3.0.0\n", "app/main.py": "from flask import Flask\napp = Flask(__name__)\n", "Dockerfile": DOCKER},
        "sf2-express-postgres": {"package.json": pkg("sf2", "express", "pg"), "server.js": EXPRESS, "Dockerfile": NODE_DOCKER},
        "sf3-django-postgres": {"requirements.txt": "django==5.0\npsycopg2-binary\n", "manage.py": "import django\n", "Dockerfile": DOCKER},
        "sf4-react-static-site": {"package.json": pkg("sf4", "react", "react-dom", "vite"), "index.html": "<html><body>hello</body></html>\n"},
    },
    "database_intensive": {
        "db1-fastapi-postgres-redis": {"requirements.txt": "fastapi\npsycopg2-binary\nredis\nalembic\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER},
        "db2-node-mongo": {"package.json": pkg("db2", "express", "mongoose"), "server.js": EXPRESS, "Dockerfile": NODE_DOCKER},
        "db3-celery-mysql": {"requirements.txt": "fastapi\nmysqlclient\ncelery\nredis\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER},
        "db4-java-postgres": {"pom.xml": "<project><dependencies><dependency><artifactId>spring-boot-starter-web</artifactId></dependency><dependency><artifactId>postgresql</artifactId></dependency></dependencies></project>\n",
                              "Dockerfile": "FROM eclipse-temurin:21-jre\nEXPOSE 8080\nCMD [\"java\", \"-jar\", \"app.jar\"]\n"},
    },
    "ai_agentic": {
        "ai1-openai-streaming": {"requirements.txt": "fastapi\nopenai\n", "app/main.py": FASTAPI, "app/stream.py": SSE, "Dockerfile": DOCKER},
        "ai2-rag-pgvector": {"requirements.txt": "fastapi\nlangchain\npsycopg2-binary\npgvector\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER},
        "ai3-node-anthropic-sse": {"package.json": pkg("ai3", "express", "@anthropic-ai/sdk"), "server.js": EXPRESS, "Dockerfile": NODE_DOCKER},
        "ai4-agent-worker-queue": {"requirements.txt": "celery\nredis\nanthropic\nfastapi\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER},
    },
    "microservices": {
        "ms1-compose-three": {"docker-compose.yml": "services:\n  api:\n    build: ./api\n    ports: ['8000:8000']\n  worker:\n    build: ./worker\n  db:\n    image: postgres:16\n",
                              "api/requirements.txt": "fastapi\npsycopg2-binary\n", "api/main.py": FASTAPI, "api/Dockerfile": DOCKER, "worker/requirements.txt": "celery\nredis\n", "worker/Dockerfile": DOCKER},
        "ms2-api-rabbit": {"api/requirements.txt": "fastapi\npika\n", "api/main.py": FASTAPI, "api/Dockerfile": DOCKER, "consumer/requirements.txt": "pika\n", "consumer/Dockerfile": DOCKER},
        "ms3-gateway-python": {"gateway/package.json": pkg("gw", "express"), "gateway/server.js": EXPRESS, "gateway/Dockerfile": NODE_DOCKER,
                               "svc/requirements.txt": "fastapi\nredis\n", "svc/main.py": FASTAPI, "svc/Dockerfile": DOCKER},
        "ms4-kafka-pipeline": {"producer/requirements.txt": "kafka-python\nfastapi\n", "producer/main.py": FASTAPI, "producer/Dockerfile": DOCKER,
                               "sink/requirements.txt": "kafka-python\npsycopg2-binary\n", "sink/Dockerfile": DOCKER},
    },
    "existing_infrastructure": {
        "ei1-existing-terraform": {"requirements.txt": "fastapi\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER,
                                   "infra/main.tf": 'provider "aws" {\n  region = "us-east-1"\n}\nresource "aws_s3_bucket" "b" {\n  bucket = "demo"\n}\n'},
        "ei2-existing-k8s": {"requirements.txt": "fastapi\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER,
                             "k8s/deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\nspec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: api\n  template:\n    metadata:\n      labels:\n        app: api\n    spec:\n      containers:\n      - name: api\n        image: api:1\n"},
        "ei3-compose-and-terraform": {"docker-compose.yml": "services:\n  web:\n    build: .\n    ports: ['8000:8000']\n", "requirements.txt": "fastapi\n", "app/main.py": FASTAPI, "Dockerfile": DOCKER,
                                      "terraform/main.tf": 'provider "azurerm" {\n  features {}\n}\n'},
        "ei4-serverless-yml": {"package.json": pkg("ei4", "serverless"), "handler.js": "exports.hello = async () => ({ statusCode: 200 });\n",
                               "serverless.yml": "service: demo\nprovider:\n  name: aws\n  runtime: nodejs20.x\nfunctions:\n  hello:\n    handler: handler.hello\n"},
    },
}

METRIC_FIELDS = ("quality_score", "capped", "deploy_ready", "blockers", "contradictions", "gates_pass", "gates_warn", "gates_fail", "gates_not_verified",
                 "ledger_records", "ledger_grounded_share", "services", "unpriced_lines", "files_generated", "verified_claims_without_proof")


def all_fixtures():
    return [(category, name, files) for category in CATEGORIES for name, files in sorted(FIXTURES[category].items())]


def write_fixture(root, files):
    for relative, content in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_cli(args, repo, env):
    done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + args + ["--repo", str(repo)], capture_output=True, text=True, env=env)
    return done.returncode, done.stdout


def measure(repo):
    state = Path(repo) / ".solsarch"
    quality, ledger, plan = read(state / "quality.json") or {}, read(state / "ledger.json") or {}, read(state / "plan.json") or {}
    statuses = [g["status"] for g in quality.get("gates", [])]
    iac = state / "iac"
    return {
        "quality_score": quality.get("score"), "capped": bool(quality.get("capped")), "deploy_ready": bool(quality.get("deploy_ready")),
        "blockers": len(quality.get("blockers", [])), "contradictions": len(quality.get("contradictions", [])),
        "gates_pass": statuses.count("PASS"), "gates_warn": statuses.count("WARNING"), "gates_fail": statuses.count("FAIL"),
        "gates_not_verified": statuses.count("NOT_VERIFIED"),
        "ledger_records": sum((ledger.get("counts") or {}).values()), "ledger_grounded_share": ledger.get("grounded_share"),
        "services": len(plan.get("decisions", [])), "unpriced_lines": ((read(state / "costs.json") or {}).get("totals") or {}).get("unpriced_lines"),
        "files_generated": sum(1 for p in iac.rglob("*") if p.is_file()) if iac.is_dir() else 0,
        "verified_claims_without_proof": len(ledger.get("unsupported_claims", [])),
    }


def run(cloud="aws", compute="server", only=None):
    env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-01-01", SOLSARCH_NO_HISTORY="1")
    results = []
    for category, name, files in all_fixtures():
        if only and name not in only:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            write_fixture(tmp, files)
            started = time.time()
            steps = [run_cli(["scan"], tmp, env)[0], run_cli(["plan", "--cloud", cloud, "--compute", compute], tmp, env)[0]]
            finished = run_cli(["finish"], tmp, env)[0] if steps == [0, 0] else None
            elapsed = round(time.time() - started, 1)
            row = {"fixture": name, "category": category, "ok": steps == [0, 0] and finished in (0,), "exit_codes": steps + [finished]}
            row.update(measure(tmp) if row["ok"] else {})
            row["seconds"] = elapsed
            results.append(row)
    return {"schema": "solsarch.benchmark/1", "tool": "solsarch", "cloud": cloud, "compute": compute, "offline": True, "results": results,
            "summary": summarise(results)}


def summarise(rows):
    done = [r for r in rows if r.get("ok")]
    scores = [r["quality_score"] for r in done if r.get("quality_score") is not None]
    out = {"fixtures": len(rows), "completed": len(done), "mean_quality_score": round(sum(scores) / len(scores), 1) if scores else None,
           "deploy_ready": sum(1 for r in done if r.get("deploy_ready")), "with_blockers": sum(1 for r in done if r.get("blockers")),
           "total_contradictions": sum(r.get("contradictions", 0) for r in done),
           "unsupported_verified_claims": sum(r.get("verified_claims_without_proof", 0) for r in done),
           "mean_grounded_share": round(sum(r["ledger_grounded_share"] or 0 for r in done) / len(done), 3) if done else None}
    out["by_category"] = {}
    for category in CATEGORIES:
        part = [r for r in done if r["category"] == category and r.get("quality_score") is not None]
        out["by_category"][category] = {"completed": len(part), "mean_quality_score": round(sum(r["quality_score"] for r in part) / len(part), 1) if part else None}
    return out


def compare(paths):
    sets = []
    for path in paths:
        data = read(path)
        if not data or "results" not in data:
            raise ValueError("%s is not a benchmark result file" % path)
        sets.append((data.get("tool", Path(path).stem), data))
    names = sorted({r["fixture"] for _, d in sets for r in d["results"]})
    lines = ["# Benchmark comparison", "", "Quality score per fixture. Blank means the run did not produce that fixture.", "",
             "| Fixture | " + " | ".join(t for t, _ in sets) + " |", "|---|" + "---|" * len(sets)]
    for name in names:
        cells = []
        for _, d in sets:
            row = next((r for r in d["results"] if r["fixture"] == name), None)
            cells.append("" if not row or row.get("quality_score") is None else str(row["quality_score"]))
        lines.append("| %s | %s |" % (name, " | ".join(cells)))
    lines += ["", "| Measure | " + " | ".join(t for t, _ in sets) + " |", "|---|" + "---|" * len(sets)]
    for key in ("completed", "mean_quality_score", "deploy_ready", "with_blockers", "total_contradictions", "unsupported_verified_claims", "mean_grounded_share"):
        lines.append("| %s | %s |" % (key.replace("_", " "), " | ".join(str(d["summary"].get(key)) for _, d in sets)))
    lines += ["", "The tool that wrote each file is named in its `tool` field. Results from other workflows are supplied by whoever ran them; this table does not check them.", ""]
    return chr(10).join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Code-to-Cloud benchmark")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--cloud", default="aws")
    p.add_argument("--compute", choices=("server", "serverless"), default="server")
    p.add_argument("--only", default="", help="Comma-separated fixture names")
    p.add_argument("--out", help="Write the result JSON here")
    p = sub.add_parser("compare")
    p.add_argument("results", nargs="+")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    if args.command == "list":
        for category, name, files in all_fixtures():
            print("%-24s %s (%d files)" % (category, name, len(files)))
        return 0
    if args.command == "compare":
        try:
            print(compare(args.results))
        except ValueError as error:
            print(json.dumps({"status": "error", "message": str(error)}))
            return 2
        return 0
    result = run(args.cloud, args.compute, {x.strip() for x in args.only.split(",") if x.strip()} or None)
    text = json.dumps(result, indent=2, sort_keys=True) + chr(10)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline=chr(10))
    print(json.dumps(result["summary"], indent=2))
    return 0 if result["summary"]["completed"] == result["summary"]["fixtures"] else 1


if __name__ == "__main__":
    sys.exit(main())
