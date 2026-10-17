"""Every kind of repository, on every cloud and compute model, must finish without errors and pass verification."""
import itertools
import zipfile

import pytest

import helpers

w = helpers.write


def monorepo(r):
    w(r, "services/api/requirements.txt",
      "fastapi\nsqlalchemy\npsycopg2-binary\nredis\ncelery\nboto3\nopenai\nqdrant-client\npyjwt\n")
    w(r, "services/api/Dockerfile", "FROM python:3.12\nEXPOSE 8000\nUSER app\n")
    w(r, "services/api/app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    w(r, "services/web/package.json", '{"dependencies": {"next": "14", "react": "18", "next-auth": "4"}}')
    w(r, "services/worker/requirements.txt", "celery\nredis\nminio\n")
    w(r, "packages/shared/package.json", '{"dependencies": {"zod": "3"}}')
    w(r, "infra/main.tf", 'resource "aws_vpc" "v" {}\n')
    w(r, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n  mq:\n    image: rabbitmq:3\n")


def workspace_of_repos(r):
    w(r, "repo-a/.git/HEAD", "ref: refs/heads/main")
    w(r, "repo-a/package.json", '{"dependencies": {"express": "4", "mongoose": "8"}}')
    w(r, "repo-b/go.mod", "module x\nrequire github.com/gin-gonic/gin v1.9\nrequire github.com/lib/pq v1\n")
    w(r, "repo-b/main.go", "package main\n")


def empty(r):
    pass


def only_readme(r):
    w(r, "README.md", "hello")


def awkward_files(r):
    w(r, "ünï/requirements.txt", "flask\n")
    w(r, "a b/package.json", "{bad json")
    (r / "bin.dat").write_bytes(bytes(range(256)) * 50)
    w(r, "big.py", "x = 1\n" * 400000)
    w(r, "requirements.txt", "﻿fastapi\n")


def serverless_first(r):
    w(r, "package.json", '{"dependencies": {"next": "14", "pg": "8", "@aws-sdk/client-s3": "3"}}')
    w(r, "serverless.yml", "service: demo\n")


CASES = {"monorepo": monorepo, "workspace": workspace_of_repos, "empty": empty, "readme": only_readme,
         "awkward": awkward_files, "serverless": serverless_first}
COMBOS = list(itertools.product(("aws", "azure", "gcp"), ("server", "serverless")))


def run(repo, cloud, mode):
    assert helpers.cli("scan", "--repo", repo)[0] == 0
    code, out = helpers.cli("plan", "--repo", repo, "--cloud", cloud, "--compute", mode)
    assert code == 0, out
    assert helpers.cli("facts", "skip", "--repo", repo, "--reason", "offline test")[0] == 0
    code, out = helpers.cli("finish", "--repo", repo)
    assert code == 0, out
    return out


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("cloud,mode", COMBOS)
def test_every_repo_shape_finishes_and_verifies(tmp_path, case, cloud, mode):
    repo = tmp_path / "project"
    repo.mkdir()
    CASES[case](repo)
    out = run(repo, cloud, mode)
    assert out["verification"] == "passed"
    names = set(zipfile.ZipFile(out["zip"]).namelist())
    assert {"report.html", "solsarch-report.md", "data/evidence.json"} <= names
    assert "diagrams/solsarch-logo.png" in names


def test_monorepo_sees_every_service():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        monorepo(repo)
        code, out = helpers.cli("scan", "--repo", repo)
        assert code == 0
        assert {"FastAPI", "Next.js", "Celery", "PostgreSQL", "Redis", "RabbitMQ", "MinIO"} <= set(out["components"])


def test_workspace_of_repos_sees_both_stacks():
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        workspace_of_repos(repo)
        code, out = helpers.cli("scan", "--repo", repo)
        assert code == 0
        assert {"Express", "MongoDB", "Gin", "PostgreSQL"} <= set(out["components"])


def test_compare_places_the_scan_on_all_three_clouds(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    monorepo(repo)
    assert helpers.cli("scan", "--repo", repo)[0] == 0
    code, out = helpers.cli("compare", "--repo", repo, "--compute", "serverless")
    assert code == 0 and out["roles"] >= 5 and set(out["fingerprints"]) == {"aws", "azure", "gcp"}
    table = (repo / "docs" / "solsarch-cloud-comparison.md").read_text(encoding="utf-8")
    assert "AWS Lambda" in table and "Azure Functions" in table and "Cloud Run" in table
    assert not (repo / ".solsarch" / "plan.json").exists()


def test_compare_needs_a_scan(tmp_path):
    code, out = helpers.cli("compare", "--repo", tmp_path)
    assert code == 2 and out["status"] == "error"
