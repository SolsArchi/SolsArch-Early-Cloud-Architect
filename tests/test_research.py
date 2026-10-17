import json

import research as rs
from helpers import placements_for


def test_plan_is_fixed_and_two_queries_per_service():
    p = placements_for("saas", "aws", "server")
    a, b = rs.plan(p), rs.plan(p)
    assert a == b
    services = {x["service"] for x in p["placements"]}
    assert len(a["queries"]) == 2 * len(services)
    assert a["queries"][0]["id"] == "Q01"
    assert all("AWS" in q["query"] for q in a["queries"])


def test_add_list_status_flow(tmp_path, capsys):
    f = str(tmp_path / "r.json")
    pl = tmp_path / "p.json"
    p = placements_for("monolith", "aws", "server")
    pl.write_text(json.dumps(p), encoding="utf-8")
    base = ["add", "--file", f, "--url", "https://docs.aws.amazon.com/x", "--title", "Doc",
            "--finding", "Supports Multi-AZ."]
    assert rs.main(base + ["--service", p["placements"][0]["service"], "--accessed", "2026-10-17"]) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["id"] == "R001" and entry["official"] is True
    assert rs.main(base + ["--service", "Other"]) == 0
    with open(f, encoding="utf-8") as fh:
        assert json.load(fh)["entries"][1]["id"] == "R002"
    assert rs.main(["status", str(pl), "--file", f]) == 1
    capsys.readouterr()
    assert rs.main(["list", "--file", f]) == 0
    assert "R001" in capsys.readouterr().out


def test_add_validation(tmp_path):
    f = str(tmp_path / "r.json")
    ok = ["add", "--file", f, "--service", "S", "--title", "T"]
    assert rs.main(ok + ["--url", "ftp://x", "--finding", "f"]) == 2
    assert rs.main(ok + ["--url", "https://x.com", "--finding", "  "]) == 2
    assert rs.main(ok + ["--url", "https://x.com", "--finding", "x" * 500]) == 2
    assert rs.main(ok + ["--url", "https://x.com", "--finding", "f", "--accessed", "yesterday"]) == 2
    assert rs.main(ok + ["--url", "https://x.com", "--finding", "f"]) == 0


def test_non_official_url_flagged(tmp_path, capsys):
    f = str(tmp_path / "r.json")
    rs.main(["add", "--file", f, "--service", "S", "--title", "T", "--url", "https://blog.example.com/a",
             "--finding", "f"])
    assert json.loads(capsys.readouterr().out)["official"] is False


def test_status_all_covered_exit_0(tmp_path):
    f = str(tmp_path / "r.json")
    p = placements_for("monolith", "aws", "server")
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps(p), encoding="utf-8")
    for s in {x["service"] for x in p["placements"]}:
        rs.main(["add", "--file", f, "--service", s, "--title", "T", "--url", "https://learn.microsoft.com/x",
                 "--finding", "f"])
    assert rs.main(["status", str(pl), "--file", f]) == 0


def test_corrupt_log_exit_2(tmp_path):
    f = tmp_path / "r.json"
    f.write_text("{bad", encoding="utf-8")
    assert rs.main(["list", "--file", str(f)]) == 2


def test_queries_use_official_domain_and_clean_names():
    p = placements_for("rag", "gcp", "serverless")
    plan = rs.plan(p)
    assert all("site:cloud.google.com" in q["query"] for q in plan["queries"])
    assert all("(" not in q["query"] for q in plan["queries"])


def test_search_name_strips_parentheses():
    assert rs.search_name("Cloud Run (WebSocket support)") == "Cloud Run"
    assert rs.search_name("Azure Container Apps (minimum one replica)") == "Azure Container Apps"
