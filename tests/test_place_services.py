import json

import place_services as ps
from helpers import evidence_for, placements_for


def load_rules():
    with open(ps.DEFAULT_RULES, encoding="utf-8") as fh:
        return json.load(fh)


def services(p):
    return {x["category"]: x["service"] for x in p["placements"]}


def test_saas_aws_server():
    s = services(placements_for("saas", "aws", "server"))
    assert s["Backend compute"] == "Amazon ECS on AWS Fargate"
    assert s["Frontend compute"] == "Amazon ECS on AWS Fargate"
    assert s["Public entry point"] == "Application Load Balancer"
    assert s["Relational database"] == "Amazon Aurora PostgreSQL"
    assert s["Cache"] == "Amazon ElastiCache for Redis"


def test_saas_aws_serverless_changes_compute_and_entry():
    s = services(placements_for("saas", "aws", "serverless"))
    assert "Lambda" in s["Backend compute"]
    assert s["Public entry point"] == "Amazon API Gateway"
    assert "Serverless" in s["Relational database"]


def test_clouds_differ_in_names():
    assert services(placements_for("saas", "azure", "server"))["Relational database"].startswith("Azure Database")
    assert services(placements_for("saas", "gcp", "server"))["Relational database"] == "Cloud SQL for PostgreSQL"


def test_rag_vector_secrets_egress_and_model_rule():
    server = services(placements_for("rag", "aws", "server"))
    assert "pgvector" in server["Vector store"]
    assert server["Secrets management"] == "AWS Secrets Manager"
    assert server["Outbound internet access"] == "NAT Gateway"
    serverless = services(placements_for("rag", "aws", "serverless"))
    assert "Outbound internet access" not in serverless
    assert "Realtime connections" in serverless


def test_streaming_caveats_depend_on_model():
    server = placements_for("rag", "aws", "server")
    api = next(x for x in server["placements"] if x["category"] == "Backend compute")
    assert any("idle timeout" in c for c in api["caveats"])
    serverless = placements_for("rag", "aws", "serverless")
    api2 = next(x for x in serverless["placements"] if x["category"] == "Backend compute")
    assert any("time limits" in c for c in api2["caveats"])


def test_sqlite_caveat_on_database_only():
    p = placements_for("monolith", "aws", "server")
    db = next(x for x in p["placements"] if x["category"] == "Relational database")
    assert any("SQLite" in c for c in db["caveats"])
    others = [x for x in p["placements"] if x is not db]
    assert all(not any("SQLite" in c for c in x["caveats"]) for x in others)


def test_confidence_rules():
    by = {x["category"]: x for x in placements_for("saas", "aws", "server")["placements"]}
    assert by["Relational database"]["confidence"] == "Confirmed"
    assert by["Cache"]["confidence"] == "Confirmed"
    assert by["Public entry point"]["confidence"] == "Inferred"


def test_deterministic_and_order_independent():
    ev = evidence_for("saas")
    rules = load_rules()
    a = ps.place(ev, rules, "aws", "server")
    shuffled = dict(ev, evidence=list(reversed(ev["evidence"])))
    b = ps.place(shuffled, rules, "aws", "server")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_placement_ids_sequential_and_evidence_ids_exist():
    for fixture in ("saas", "rag", "monolith"):
        ev = evidence_for(fixture)
        valid = {e["id"] for e in ev["evidence"]}
        p = ps.place(ev, load_rules(), "gcp", "serverless")
        assert [x["id"] for x in p["placements"]] == ["P%02d" % (i + 1) for i in range(len(p["placements"]))]
        for x in p["placements"]:
            assert x["evidence_ids"] and set(x["evidence_ids"]) <= valid
            assert "{" not in x["why"]


def test_unless_blocks_static_hosting_when_next_present():
    assert "Static frontend hosting" not in services(placements_for("saas", "aws", "server"))


def test_empty_evidence_gives_no_placements():
    assert ps.place({"evidence": []}, load_rules(), "aws", "server")["placements"] == []


def test_invalid_cloud_or_model_raises():
    for cloud, model in (("oracle", "server"), ("aws", "hybrid")):
        try:
            ps.place({"evidence": []}, load_rules(), cloud, model)
            assert False
        except ValueError:
            pass


def test_rules_file_is_complete():
    rules = load_rules()
    ids = [r["id"] for r in rules["rules"]]
    assert len(ids) == len(set(ids))
    for r in rules["rules"]:
        for cloud in ("aws", "azure", "gcp"):
            for model in ("server", "serverless"):
                assert r["services"][cloud][model].strip()
        assert set(r["alternative"]) == {"server", "serverless"}
        r["why"].format(evidence_names="x", service="y", model="z")
        assert r["confidence"] in ("Confirmed", "Inferred")


def test_cli(tmp_path, capsys):
    ev = tmp_path / "e.json"
    ev.write_text(json.dumps(evidence_for("saas")), encoding="utf-8")
    assert ps.main([str(ev), "--cloud", "aws", "--model", "server"]) == 0
    assert json.loads(capsys.readouterr().out)["cloud"] == "aws"
    assert ps.main([str(tmp_path / "missing.json"), "--cloud", "aws", "--model", "server"]) == 2


def test_each_caveat_text_appears_once():
    for fixture, model in (("rag", "server"), ("rag", "serverless"), ("monolith", "server")):
        texts = [c for x in placements_for(fixture, "aws", model)["placements"] for c in x["caveats"]]
        assert len(texts) == len(set(texts))
