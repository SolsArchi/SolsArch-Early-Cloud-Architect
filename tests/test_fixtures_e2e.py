import json
import os

import ast_scanner
import evidence_bridge
import diagram_sanitizer as ds

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
SKILL = os.path.join(os.path.dirname(__file__), "..", "solsarch-early-cloud-architect")


def scan(name):
    return ast_scanner.scan(os.path.join(FIX, name))


def names(r):
    return {c["name"] for c in r["components"]} | set(r["languages"])


def rules(r):
    return {f["id"] for f in r["findings"]}


def test_saas_fixture():
    r = scan("saas")
    assert {"Next.js", "FastAPI", "PostgreSQL", "Redis", "AWS SDK", "Prisma"} <= names(r)
    assert rules(r) == set()


def test_rag_fixture():
    r = scan("rag")
    assert {"OpenAI SDK", "ChromaDB", "LangChain"} <= names(r)
    assert "SEC-004" in rules(r)


def test_monolith_fixture():
    r = scan("monolith")
    assert {"Django", "SQLite"} <= names(r)
    assert "SEC-003" in rules(r)


def test_rust_fixture():
    r = scan("rust_cli")
    assert {"Axum", "SQLite"} <= names(r)


def test_flat_evidence_ids_resolve_for_all_fixtures():
    for name in os.listdir(FIX):
        flat = evidence_bridge.flatten(scan(name))["evidence"]
        ids = [e["id"] for e in flat]
        assert ids == ["E%03d" % (i + 1) for i in range(len(ids))]


def test_scanner_output_is_json_serialisable_and_ascii():
    for name in os.listdir(FIX):
        json.dumps(scan(name), ensure_ascii=True)
        json.dumps(evidence_bridge.flatten(scan(name)), ensure_ascii=True)


def test_every_shipped_mermaid_block_is_clean():
    refs = os.path.join(SKILL, "references")
    for fname in sorted(f for f in os.listdir(refs) if os.path.isfile(os.path.join(refs, f))):
        path = os.path.join(refs, fname)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if "```mermaid" in text:
            _, reported, remaining = ds.sanitize_text(text, fix=False)
            assert remaining == 0, (fname, reported)


def test_skill_frontmatter_rules():
    import re
    with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m
    fm = m.group(1)
    assert "<" not in fm and ">" not in fm
    name = re.search(r"^name: (.+)$", fm, re.M).group(1)
    assert name == os.path.basename(os.path.abspath(SKILL))
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)
    assert "claude" not in name and "anthropic" not in name
    desc = re.search(r"^description: (.+)$", fm, re.M).group(1)
    assert len(desc) < 1024
    assert not os.path.exists(os.path.join(SKILL, "README.md"))


def test_every_path_mentioned_in_skill_md_exists():
    import re
    with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as fh:
        text = fh.read()
    paths = set(re.findall(r"\b((?:scripts|references|assets)/[A-Za-z0-9_\-]+\.[a-z]+)", text))
    assert paths
    for rel in paths:
        assert os.path.isfile(os.path.join(SKILL, rel)), rel
