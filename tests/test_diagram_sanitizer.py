import mermaid_lint as ds


def check(text):
    _, reported, remaining = ds.sanitize_text(text, fix=False)
    return reported, remaining


def fix(text):
    new_text, reported, remaining = ds.sanitize_text(text, fix=True)
    return new_text, remaining


def rules(text):
    return {i["rule"] for i in check(text)[0]}


GOOD_FLOW = '''flowchart TD
  user["User (Browser)"] -->|"HTTPS:443"| alb["ALB"]
  subgraph vpc["VPC 10.0.0.0/16"]
    alb --> api["API (ECS Fargate)"]
    api --> db[("Aurora PostgreSQL")]
  end
  classDef aws fill:#FF9900,color:#000
  class api aws
'''


def test_valid_flowchart_is_clean():
    reported, remaining = check(GOOD_FLOW)
    assert remaining == 0
    assert not [r for r in reported if r["severity"] == "error"]


def test_unquoted_label_with_parens_is_error_and_fixed():
    src = "flowchart LR\n  a[Web (SPA)] --> b[API]\n"
    assert "unquoted-label" in rules(src)
    fixed, remaining = fix(src)
    assert remaining == 0
    assert 'a["Web (SPA)"]' in fixed
    assert 'b["API"]' in fixed


def test_fix_is_idempotent():
    src = "flowchart LR\n  a[Web (SPA)] --> b{Is ok?}\n"
    once, _ = fix(src)
    twice, remaining = fix(once)
    assert once == twice and remaining == 0


def test_cylinder_and_stadium_shapes_survive():
    src = 'flowchart TD\n  db[(Postgres)]\n  s([Start])\n  c((Core))\n'
    fixed, remaining = fix(src)
    assert remaining == 0
    assert 'db[("Postgres")]' in fixed
    assert 's(["Start"])' in fixed
    assert 'c(("Core"))' in fixed


def test_inner_double_quote_escaped():
    src = 'flowchart TD\n  a["say "hi""]\n'
    fixed, remaining = fix(src)
    assert remaining == 0
    assert "#quot;" in fixed


def test_br_tag_allowed_other_angle_brackets_escaped():
    src = 'flowchart TD\n  a["Line1<br/>Line2"]\n  b["x < y"]\n'
    reported, _ = check(src)
    flagged = {r["line"] for r in reported if r["rule"] == "label-chars"}
    assert flagged == {3}
    fixed, remaining = fix(src)
    assert "<br/>" in fixed and "#lt;" in fixed and remaining == 0


def test_edge_label_with_parens_quoted():
    src = 'flowchart TD\n  a["A"] -->|HTTPS (443)| b["B"]\n'
    assert "edge-label" in rules(src)
    fixed, remaining = fix(src)
    assert '|"HTTPS (443)"|' in fixed and remaining == 0


def test_empty_label_flagged():
    src = 'flowchart TD\n  a[""] --> b["B"]\n'
    assert "empty-label" in rules(src)
    fixed, remaining = fix(src)
    assert remaining == 0 and 'a["a"]' in fixed


def test_unclosed_subgraph_is_error():
    src = 'flowchart TD\n  subgraph g["G"]\n    a["A"]\n'
    assert "unclosed-subgraph" in rules(src)
    assert check(src)[1] == 1


def test_extra_end_is_error():
    src = 'flowchart TD\n  a["A"]\nend\n'
    assert "unbalanced-end" in rules(src)


def test_multiword_subgraph_title_fixed():
    src = 'flowchart TD\n  subgraph Private Subnet\n    a["A"]\n  end\n'
    fixed, remaining = fix(src)
    assert remaining == 0
    assert 'subgraph Private_Subnet["Private Subnet"]' in fixed


def test_end_as_node_id_flagged():
    src = 'flowchart TD\n  a["A"] --> end\n'
    assert "reserved-end" in rules(src)


def test_word_end_inside_quoted_label_is_fine():
    src = 'flowchart TD\n  a["Backend end"] --> b["B"]\n'
    assert "reserved-end" not in rules(src)


def test_subgraph_id_collides_with_node_id():
    src = 'flowchart TD\n  api["API"]\n  subgraph api["Group"]\n    x["X"]\n  end\n'
    assert "duplicate-id" in rules(src)


def test_smart_quotes_replaced():
    src = 'flowchart TD\n  a[“Hello”]\n'
    fixed, remaining = fix(src)
    assert remaining == 0 and "“" not in fixed


def test_unknown_header_is_error():
    assert "unknown-header" in rules("hello world\n  a --> b\n")


def test_empty_diagram_is_error():
    assert "empty-diagram" in rules("\n\n")


def test_directive_before_header_ok():
    src = '%%{init: {"theme":"neutral"}}%%\nflowchart TD\n  a["A"]\n'
    assert check(src)[1] == 0


def test_markdown_fences_processed_and_prose_untouched():
    src = "# Title\n\nSome (prose) [here].\n\n```mermaid\nflowchart TD\n  a[Bad (x)]\n```\n\nmore text\n"
    fixed, remaining = fix(src)
    assert remaining == 0
    assert "Some (prose) [here]." in fixed
    assert 'a["Bad (x)"]' in fixed
    assert fixed.endswith("more text\n")


def test_multiple_fences_line_numbers_are_file_lines():
    src = "```mermaid\nflowchart TD\n  a[\"A\"]\n```\n\n```mermaid\nflowchart TD\n  b[Bad (x)]\n```\n"
    reported, remaining = check(src)
    assert remaining == 1
    assert reported[0]["line"] == 8


def test_crlf_preserved():
    src = "flowchart TD\r\n  a[Bad (x)]\r\n"
    fixed, _ = fix(src)
    assert "\r\n" in fixed and fixed.count("\n") == fixed.count("\r\n")


GOOD_SEQ = '''sequenceDiagram
  participant U as User
  participant API as API Gateway
  U->>API: POST /login
  alt valid
    API-->>U: 200 JWT
  else invalid
    API-->>U: 401
  end
'''


def test_valid_sequence_clean():
    assert check(GOOD_SEQ)[1] == 0


def test_sequence_semicolon_fixed():
    src = "sequenceDiagram\n  A->>B: first; second\n"
    assert "semicolon" in rules(src)
    fixed, remaining = fix(src)
    assert remaining == 0 and "#59;" in fixed


def test_sequence_unclosed_block():
    src = "sequenceDiagram\n  A->>B: hi\n  loop forever\n    B->>A: ok\n"
    assert "unclosed-block" in rules(src)


def test_sequence_bad_participant_id():
    src = "sequenceDiagram\n  participant API Gateway\n"
    assert "participant-id" in rules(src)


GOOD_C4 = '''C4Container
  title Container diagram
  Person(user, "User", "Uses the app")
  System_Boundary(sys, "SolsArch") {
    Container(api, "API", "FastAPI", "Serves requests")
    ContainerDb(db, "Database", "PostgreSQL", "Stores data")
  }
  Rel(user, api, "Uses", "HTTPS")
  Rel(api, db, "Reads/Writes", "SQL")
'''


def test_valid_c4_clean():
    assert check(GOOD_C4)[1] == 0


def test_c4_unquoted_args_fixed():
    src = "C4Context\n  Person(user, User, Uses it)\n  System(app, App, Does stuff)\n  Rel(user, app, Uses)\n"
    assert "c4-unquoted" in rules(src)
    fixed, remaining = fix(src)
    assert remaining == 0
    assert 'Person(user, "User", "Uses it")' in fixed


def test_c4_unknown_alias_in_rel():
    src = 'C4Context\n  Person(user, "User")\n  Rel(user, ghost, "Uses")\n'
    assert "c4-unknown-alias" in rules(src)


def test_c4_unbalanced_brace():
    src = 'C4Container\n  System_Boundary(s, "S") {\n    Container(a, "A", "x")\n'
    assert "unbalanced-brace" in rules(src)


def test_c4_duplicate_alias():
    src = 'C4Context\n  Person(u, "A")\n  Person(u, "B")\n'
    assert "duplicate-id" in rules(src)


def test_other_diagram_types_accepted():
    assert check("erDiagram\n  USER ||--o{ ORDER : places\n")[1] == 0
    assert check("classDiagram\n  class A\n")[1] == 0


def test_cli_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.mmd"
    good.write_text(GOOD_FLOW, encoding="utf-8")
    bad = tmp_path / "bad.mmd"
    bad.write_text("flowchart TD\n  a[Bad (x)]\n", encoding="utf-8")
    assert ds.main([str(good)]) == 0
    assert ds.main([str(bad)]) == 1
    assert ds.main([str(bad), "--fix"]) == 0
    assert 'a["Bad (x)"]' in bad.read_text(encoding="utf-8")
    assert ds.main([]) == 2
    assert ds.main([str(tmp_path / "missing.mmd")]) == 2


def test_cli_json_output(tmp_path, capsys):
    import json
    bad = tmp_path / "bad.mmd"
    bad.write_text("flowchart TD\n  a[Bad (x)]\n", encoding="utf-8")
    ds.main([str(bad), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["results"][0]["issues"][0]["rule"] == "unquoted-label"


def test_unfixable_error_remains_after_fix(tmp_path):
    bad = tmp_path / "bad.mmd"
    bad.write_text("flowchart TD\n  subgraph g[\"G\"]\n    a[\"A\"]\n", encoding="utf-8")
    assert ds.main([str(bad), "--fix"]) == 1


def test_non_ascii_letters_do_not_crash():
    src = 'flowchart TD\n  caf\u00e9["Caf\u00e9 service"] --> b["B"]\n  \u00e9t\u00e9[Bad (x)]\n'
    new_text, reported, remaining = ds.sanitize_text(src, fix=True)
    assert "Caf\u00e9 service" in new_text
