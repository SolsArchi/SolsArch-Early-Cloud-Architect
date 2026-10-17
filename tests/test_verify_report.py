import json

import verify_report as vr

EVIDENCE = {"evidence": [{"id": "E001"}, {"id": "E002"}, {"id": "E003"}]}

GOOD = """# Review

## Blockers
| Rule | Finding | Evidence | Fix |
| :--- | :--- | :--- | :--- |
| SEC-003 | SQLite on containers | E001, E002 | Use PostgreSQL |

## Services
| Proposed service | Category | Evidence | Justification |
| :--- | :--- | :--- | :--- |
| ECS Fargate | Compute | E002 | Long-running API |
| Aurora PostgreSQL | Database | E001 | Replaces SQLite |

```mermaid
flowchart TD
  a["A"] --> b["B"]
```

- [x] done
"""


def errors(text, evidence=EVIDENCE):
    return [p for p in vr.check(text, evidence) if p["severity"] == "error"]


def test_good_report_passes():
    assert errors(GOOD) == []


def test_service_without_evidence_fails():
    bad = GOOD.replace("| ECS Fargate | Compute | E002 |", "| ECS Fargate | Compute | none |")
    assert any("cites no evidence" in p["message"] for p in errors(bad))


def test_unknown_evidence_id_fails():
    bad = GOOD.replace("E002 | Long-running", "E999 | Long-running")
    assert any("E999" in p["message"] for p in errors(bad))


def test_blocker_without_evidence_fails_but_sec001_allowed():
    bad = GOOD.replace("| SEC-003 | SQLite on containers | E001, E002 |", "| SEC-003 | SQLite on containers | - |")
    assert any("SEC-003" in p["message"] for p in errors(bad))
    ok = GOOD.replace("| SEC-003 | SQLite on containers | E001, E002 |", "| SEC-001 | No load balancer | design |")
    assert errors(ok) == []


def test_unknown_rule_id_fails():
    bad = GOOD.replace("| SEC-003 |", "| SEC-009 |")
    assert any("unknown rule" in p["message"] for p in errors(bad))


def test_leaked_secret_fails():
    assert any("secret" in p["message"] for p in errors(GOOD + "\nkey AKIAABCDEFGHIJKLMNOP\n"))


def test_unfilled_template_field_fails():
    assert any("unfilled" in p["message"] for p in errors(GOOD + "\n{{project_name}}\n"))


def test_dollar_figure_is_warning_not_error():
    problems = vr.check(GOOD + "\nCosts $450 a month\n", EVIDENCE)
    assert any(p["severity"] == "warning" and "dollar" in p["message"] for p in problems)
    assert errors(GOOD + "\nCosts $450 a month\n") == []


def test_unticked_checklist_warns():
    problems = vr.check(GOOD + "\n- [ ] pending\n", EVIDENCE)
    assert any("self-check" in p["message"] for p in problems)


def test_invalid_mermaid_in_report_fails():
    bad = GOOD.replace('a["A"]', "a[Bad (x)]")
    assert any("mermaid" in p["message"] for p in errors(bad))


def test_evidence_ids_inside_code_fences_ignored():
    text = GOOD.replace("flowchart TD", "flowchart TD\n  %% E777 note")
    assert errors(text) == []


def test_cli(tmp_path, capsys):
    rep = tmp_path / "r.md"
    rep.write_text(GOOD, encoding="utf-8")
    ev = tmp_path / "e.json"
    ev.write_text(json.dumps(EVIDENCE), encoding="utf-8")
    assert vr.main([str(rep), "--evidence", str(ev)]) == 0
    rep.write_text(GOOD.replace("E002 | Long", "E500 | Long"), encoding="utf-8")
    assert vr.main([str(rep), "--evidence", str(ev)]) == 1
    assert vr.main([str(tmp_path / "missing.md"), "--evidence", str(ev)]) == 2


def test_report_without_diagrams_is_not_a_mermaid_error():
    text = GOOD.split("```mermaid")[0]
    assert errors(text) == []


def test_service_citing_only_language_evidence_fails():
    ev = {"evidence": [{"id": "E001", "kind": "container"}, {"id": "E002", "kind": "language"}]}
    bad = GOOD.replace("| Aurora PostgreSQL | Database | E001 |", "| Aurora PostgreSQL | Database | E002 |")
    assert any("only language or port" in p["message"] for p in errors(bad, ev))
    base = GOOD.replace("| ECS Fargate | Compute | E002 |", "| ECS Fargate | Compute | E001 |")
    mixed = base.replace("| Aurora PostgreSQL | Database | E001 |", "| Aurora PostgreSQL | Database | E001, E002 |")
    assert not any("only language" in p["message"] for p in errors(mixed, ev))
