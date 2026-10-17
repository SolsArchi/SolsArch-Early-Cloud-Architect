import json
import os

import hermes_memory as hm


def run(root, *args):
    return hm.main(list(args[:1]) + ["--root", str(root)] + list(args[1:]))


def read(root):
    with open(os.path.join(str(root), ".solsarch", "memory.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_read_when_absent(tmp_path, capsys):
    assert run(tmp_path, "read") == 0
    assert json.loads(capsys.readouterr().out) == {"exists": False}


def test_set_fields_and_persist(tmp_path):
    assert run(tmp_path, "set", "primary_cloud", "AWS") == 0
    assert run(tmp_path, "set", "target_region", "eu-west-1") == 0
    assert run(tmp_path, "set", "compliance_frameworks", "SOC2-Type-II, GDPR") == 0
    assert run(tmp_path, "set", "budget_ceiling_monthly_usd", "450") == 0
    data = read(tmp_path)
    assert data["primary_cloud"] == "aws"
    assert data["target_region"] == "eu-west-1"
    assert data["compliance_frameworks"] == ["SOC2-Type-II", "GDPR"]
    assert data["budget_ceiling_monthly_usd"] == 450
    assert data["last_analyzed"].endswith("Z")


def test_set_rejects_bad_values(tmp_path):
    assert run(tmp_path, "set", "primary_cloud", "oracle") == 2
    assert run(tmp_path, "set", "target_region", "EU WEST!") == 2
    assert run(tmp_path, "set", "budget_ceiling_monthly_usd", "-5") == 2
    assert run(tmp_path, "set", "budget_ceiling_monthly_usd", "abc") == 2
    assert run(tmp_path, "set", "nonsense", "x") == 2
    assert not os.path.exists(os.path.join(str(tmp_path), ".solsarch"))


def test_adr_ids_increment(tmp_path):
    for i in range(3):
        assert run(tmp_path, "add-adr", "--component", "DB", "--decision", "d%d" % i, "--rationale", "r") == 0
    ids = [d["id"] for d in read(tmp_path)["architectural_decisions"]]
    assert ids == ["ADR-001", "ADR-002", "ADR-003"]


def test_supersede_marks_old_decision(tmp_path):
    run(tmp_path, "add-adr", "--component", "DB", "--decision", "SQLite", "--rationale", "r")
    run(tmp_path, "add-adr", "--component", "DB", "--decision", "Aurora", "--rationale", "r", "--supersedes", "ADR-001")
    decisions = read(tmp_path)["architectural_decisions"]
    assert decisions[0]["status"] == "SUPERSEDED" and decisions[0]["superseded_by"] == "ADR-002"
    assert decisions[1]["supersedes"] == "ADR-001"


def test_supersede_unknown_fails(tmp_path):
    assert run(tmp_path, "add-adr", "--component", "c", "--decision", "d", "--rationale", "r", "--supersedes", "ADR-009") == 2


def test_bad_status_and_empty_fields(tmp_path):
    assert run(tmp_path, "add-adr", "--component", "c", "--decision", "d", "--rationale", "r", "--status", "MAYBE") == 2
    assert run(tmp_path, "add-adr", "--component", " ", "--decision", "d", "--rationale", "r") == 2


def test_corrupt_file_is_backed_up_not_lost(tmp_path, capsys):
    mem = tmp_path / ".solsarch"
    mem.mkdir()
    (mem / "memory.json").write_text("{not json", encoding="utf-8")
    assert run(tmp_path, "set", "primary_cloud", "gcp") == 0
    backups = [p for p in os.listdir(str(mem)) if p.startswith("memory.json.corrupt-")]
    assert len(backups) == 1
    assert (mem / backups[0]).read_text(encoding="utf-8") == "{not json"
    assert read(tmp_path)["primary_cloud"] == "gcp"
    assert "unreadable" in capsys.readouterr().err


def test_no_temp_files_left_behind(tmp_path):
    run(tmp_path, "set", "primary_cloud", "azure")
    assert os.listdir(os.path.join(str(tmp_path), ".solsarch")) == ["memory.json"]


def test_summary_output(tmp_path, capsys):
    run(tmp_path, "summary")
    assert "No Hermes memory" in capsys.readouterr().out
    run(tmp_path, "set", "primary_cloud", "aws")
    run(tmp_path, "add-adr", "--component", "Ingress", "--decision", "Use ALB", "--rationale", "SEC-001")
    capsys.readouterr()
    run(tmp_path, "summary")
    out = capsys.readouterr().out
    assert "Cloud: aws" in out and "ADR-001" in out and "Use ALB" in out


def test_read_after_write_roundtrip(tmp_path, capsys):
    run(tmp_path, "add-adr", "--component", "Cache", "--decision", "Redis", "--rationale", "queue")
    capsys.readouterr()
    run(tmp_path, "read")
    data = json.loads(capsys.readouterr().out)
    assert data["exists"] is True
    assert data["architectural_decisions"][0]["decision"] == "Redis"


def test_none_values_clear_budget_and_compliance(tmp_path):
    run(tmp_path, "set", "budget_ceiling_monthly_usd", "500")
    run(tmp_path, "set", "compliance_frameworks", "GDPR")
    assert run(tmp_path, "set", "budget_ceiling_monthly_usd", "none") == 0
    assert run(tmp_path, "set", "compliance_frameworks", "none") == 0
    data = read(tmp_path)
    assert data["budget_ceiling_monthly_usd"] is None
    assert data["compliance_frameworks"] == []


def test_adr_default_status_is_proposed(tmp_path):
    run(tmp_path, "add-adr", "--component", "c", "--decision", "d", "--rationale", "r")
    assert read(tmp_path)["architectural_decisions"][0]["status"] == "PROPOSED"
