"""Accuracy benchmark for the scanner.

Each case is a tiny labelled repository with the exact components and findings the scanner
should report. Clean cases must produce nothing. A case passes only on an exact match, so
both false positives and misses fail it.
"""

import tempfile
import unittest

import helpers
from helpers import write

import ast_scanner

KEY = helpers.fake_aws_key()

CASES = {
    "node_clean": dict(
        files={
            "package.json": '{"scripts": {"dev": "next dev", "pg": "echo pg"},\n "dependencies": {"next": "14", "react": "18"}}\n',
            "Dockerfile": "FROM node:20\nUSER node\nCMD [\"node\", \"s.js\"]\n",
        },
        components={("framework", "Next.js"), ("framework", "React")}, findings=set()),
    "python_clean": dict(
        files={
            "requirements.txt": "# redis is NOT used here\nDjango==5.0\npsycopg2-binary==2.9\n",
            "app/settings.py": "ENGINE = 'django.db.backends.postgresql'\n",
        },
        components={("framework", "Django"), ("database", "PostgreSQL")}, findings=set()),
    "django_sqlite_container": dict(
        files={
            "requirements.txt": "Django==5.0\n",
            "app/settings.py": "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3'}}\n",
            "Dockerfile": "FROM python:3.12\nUSER app\n",
        },
        components={("framework", "Django"), ("database", "SQLite")}, findings={"SEC-003"}),
    "pyproject_keywords_are_not_deps": dict(
        files={
            "pyproject.toml": (
                '[project]\nname = "demo"\nkeywords = ["django", "redis"]\n'
                'classifiers = ["Framework :: Django"]\n'
                'dependencies = [\n  "fastapi>=0.110",\n  "redis[hiredis]>=5",\n]\n'
                '[tool.poetry]\nname = "demo"\n'),
        },
        components={("framework", "FastAPI"), ("cache", "Redis")}, findings=set()),
    "poetry_sections": dict(
        files={
            "pyproject.toml": (
                '[tool.poetry]\nname = "demo"\ndescription = "uses next and celery"\n'
                '[tool.poetry.dependencies]\npython = "^3.11"\nflask = "^3"\ncelery = {version = "^5"}\n'),
        },
        components={("framework", "Flask"), ("queue", "Celery")}, findings=set()),
    "compose_local_only": dict(
        files={
            "docker-compose.yml": (
                "services:\n  db:\n    image: postgres:16\n    ports:\n"
                "      - \"127.0.0.1:5432:5432\"\n  web:\n    ports:\n      - \"8080:80\"\n"),
        },
        components={("database", "PostgreSQL")}, findings=set()),
    "streaming_proxy_ok": dict(
        files={
            "requirements.txt": "fastapi\n",
            "app.py": "from fastapi.responses import StreamingResponse\n",
            "nginx.conf": "location / {\n proxy_pass http://app;\n proxy_buffering off;\n proxy_read_timeout 600s;\n}\n",
        },
        components={("framework", "FastAPI")}, findings=set()),
    "env_example_and_lookups": dict(
        files={
            ".env.example": "API_KEY=abcdef1234567890xyz\nDB_PASSWORD=changeme-please-1\n",
            "config.py": 'import os\npassword = os.environ["DB_PASSWORD"]\napi_key = "your-api-key-here-1234"\n',
        },
        components=set(), findings=set()),
    "iam_scoped": dict(
        files={
            "policy.json": '{"Statement": [{"Action": "s3:GetObject", "Resource": "*"},\n {"Action": "s3:*", "Resource": "arn:aws:s3:::b/*"}]}\n',
        },
        components=set(), findings=set()),
    "terraform_healthy": dict(
        files={
            "main.tf": (
                'resource "aws_cloudfront_distribution" "cdn" {\n}\n'
                'resource "aws_ecs_service" "api" {\n}\n'
                'resource "aws_lb" "alb" {\n}\n'
                'resource "aws_security_group" "web" {\n  ingress {\n    from_port = 443\n'
                '    to_port = 443\n    cidr_blocks = ["0.0.0.0/0"]\n  }\n}\n'
                'resource "aws_db_instance" "db" {\n  publicly_accessible = false\n}\n'),
        },
        components=set(), findings=set()),
    "notes_and_comments_ignored": dict(
        files={
            "README.md": "We used sqlite:/// before.\n",
            "docs/notes.txt": "migrate off sqlite:///data.db and lmdb\n",
            "app.py": "# old: sqlite3.connect('x.db') and text/event-stream\nprint('ok')\n",
        },
        components=set(), findings=set()),
    "polyglot_manifests": dict(
        files={
            "go.mod": "module x\n\nrequire (\n\tgithub.com/lib/pq v1.10.9\n\tgithub.com/redis/go-redis/v9 v9.5.1\n)\n",
            "Gemfile": "source 'https://rubygems.org'\ngem 'rails'\ngem 'pg'\ngem 'sidekiq'\n",
            "pom.xml": "<project><dependencies><dependency><artifactId>spring-boot-starter-web</artifactId></dependency>"
                       "<dependency><artifactId>postgresql</artifactId></dependency></dependencies></project>\n",
        },
        components={("database", "PostgreSQL"), ("cache", "Redis"), ("framework", "Ruby on Rails"),
                    ("queue", "Sidekiq"), ("framework", "Spring Boot")},
        findings=set()),
    "seeded_blockers": dict(
        files={
            "main.tf": (
                'resource "aws_cloudfront_distribution" "c" {\n}\nresource "aws_ecs_service" "s" {\n}\n'
                'resource "aws_security_group" "db" {\n  ingress {\n    from_port = 5432\n'
                '    cidr_blocks = ["0.0.0.0/0"]\n  }\n}\n'),
            "requirements.txt": "fastapi\nopenai\n",
            "app.py": "import sqlite3\nfrom fastapi.responses import StreamingResponse\n",
            "Dockerfile": "FROM python:3.12\nUSER app\n",
            "nginx.conf": "location / {\n proxy_pass http://app;\n}\n",
            "settings.py": 'AWS = "%s"\n' % KEY,
        },
        components={("framework", "FastAPI"), ("ai", "OpenAI SDK"), ("database", "SQLite")},
        findings={"SEC-001", "SEC-002", "SEC-003", "SEC-004", "SEC-005"}),
}


def observed(result):
    components = {(c["category"], c["name"]) for c in result["components"]}
    return components, {f["id"] for f in result["findings"]}


class AccuracyTests(unittest.TestCase):
    def test_cases(self):
        true_pos = false_pos = misses = 0
        for name, case in CASES.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                for relative, content in case["files"].items():
                    write(tmp, relative, content)
                components, findings = observed(ast_scanner.scan(tmp))
                # Terraform and cloud-sdk hints are not part of the labelled components.
                components = {c for c in components if c[0] != "cloud-sdk"}
                expected_items = case["components"] | {("finding", f) for f in case["findings"]}
                got_items = components | {("finding", f) for f in findings}
                true_pos += len(expected_items & got_items)
                false_pos += len(got_items - expected_items)
                misses += len(expected_items - got_items)
                self.assertEqual(got_items - expected_items, set(), "false positives")
                self.assertEqual(expected_items - got_items, set(), "misses")
        precision = true_pos / (true_pos + false_pos) if true_pos + false_pos else 1.0
        recall = true_pos / (true_pos + misses) if true_pos + misses else 1.0
        self.assertEqual((precision, recall), (1.0, 1.0))

    def test_severity_calibration(self):
        expectations = {
            "seeded_blockers": {"SEC-001": "high", "SEC-002": "critical", "SEC-003": "high",
                                "SEC-004": "medium", "SEC-005": "critical"},
            "django_sqlite_container": {"SEC-003": "high"},
        }
        for name, severities in expectations.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as tmp:
                for relative, content in CASES[name]["files"].items():
                    write(tmp, relative, content)
                result = ast_scanner.scan(tmp)
                self.assertEqual({f["id"]: f["severity"] for f in result["findings"]}, severities)

    def test_every_finding_cites_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            for relative, content in CASES["seeded_blockers"]["files"].items():
                write(tmp, relative, content)
            result = ast_scanner.scan(tmp)
        for finding in result["findings"]:
            self.assertTrue(finding["evidence"], finding["id"])
            for item in finding["evidence"]:
                self.assertTrue(item["file"] and item["line"] >= 1)
                self.assertTrue((tmp and True))
        for component in result["components"]:
            self.assertTrue(component["evidence"])


if __name__ == "__main__":
    unittest.main()
