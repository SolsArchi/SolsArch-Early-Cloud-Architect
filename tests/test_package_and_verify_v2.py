import json
import zipfile

import package_report as po
import render_diagram
import verify_report as vr
from helpers import DIAGRAM, evidence_for, make_report, placements_for


def errors(report, evidence, placements=None, research=None):
    return [p["message"] for p in vr.check(report, evidence, placements, research) if p["severity"] == "error"]


def setup(fixture="saas", cloud="aws", model="server"):
    return evidence_for(fixture), placements_for(fixture, cloud, model)


def test_complete_report_passes_all_checks():
    ev, pl = setup()
    assert errors(make_report(pl), ev, pl, {"entries": []}) == []


def test_diagram_without_explanation_fails():
    ev, pl = setup()
    rep = make_report(pl)
    start = rep.index("The diagram shows")
    bad = rep[:start] + "\n## Sources\nWeb verification: not performed\n"
    assert any("no explanation" in m for m in errors(bad, ev, pl))


def test_missing_placement_fails():
    ev, pl = setup()
    rep = make_report(pl)
    victim = pl["placements"][-1]
    lines = [ln for ln in rep.split("\n") if victim["service"] not in ln]
    assert any(victim["id"] in m for m in errors("\n".join(lines), ev, pl))


def test_short_service_section_fails():
    ev, pl = setup()
    rep = make_report(pl)
    first = pl["placements"][0]
    marker = "### %s %s\n" % (first["id"], first["service"])
    i = rep.index(marker) + len(marker)
    j = rep.index("\n\n", i)
    bad = rep[:i] + "Short." + rep[j:]
    assert any("too short" in m for m in errors(bad, ev, pl))


def test_research_must_be_logged_or_declared_not_performed():
    ev, pl = setup()
    rep = make_report(pl).replace("Web verification: not performed", "Sources: none")
    assert any("no web sources were logged" in m for m in errors(rep, ev, pl, {"entries": []}))


def test_research_unknown_id_and_uncited_source_fail():
    ev, pl = setup()
    entries = [{"id": "R001", "service": pl["placements"][0]["service"]}]
    rep = make_report(pl, ["R009"])
    msgs = errors(rep, ev, pl, {"entries": entries})
    assert any("R009" in m for m in msgs)
    rep2 = make_report(pl, ["R001"])
    msgs2 = errors(rep2, ev, pl, {"entries": entries})
    assert any("no web source logged for" in m for m in msgs2)


def test_research_all_services_cited_passes():
    ev, pl = setup()
    services = []
    for p in pl["placements"]:
        if p["service"] not in services:
            services.append(p["service"])
    entries = [{"id": "R%03d" % (i + 1), "service": s} for i, s in enumerate(services)]
    rep = make_report(pl, [e["id"] for e in entries])
    assert errors(rep, ev, pl, {"entries": entries}) == []


def build_zip(tmp_path, monkeypatch, with_cli=False, report=None, force=False):
    ev, pl = setup("monolith")
    (tmp_path / "e.json").write_text(json.dumps(ev), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(pl), encoding="utf-8")
    (tmp_path / "r.md").write_text(report or make_report(pl), encoding="utf-8")
    proj = tmp_path / "proj" / ".solsarch"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "memory.json").write_text('{"x": 1}', encoding="utf-8")
    if not with_cli:
        monkeypatch.setattr(render_diagram.shutil, "which", lambda name: None)
    args = [str(tmp_path / "r.md"), "--evidence", str(tmp_path / "e.json"), "--placements", str(tmp_path / "p.json"),
            "--research", str(tmp_path / "none.json"), "--name", "My Shop", "--project-root", str(tmp_path / "proj"),
            "--out-dir", str(tmp_path / "out")] + (["--force"] if force else [])
    return po.main(args)


def test_zip_layout_without_image_cli(tmp_path, monkeypatch, capsys):
    assert build_zip(tmp_path, monkeypatch) == 0
    path = capsys.readouterr().out.strip()
    assert path.endswith("my-shop-architecture.zip")
    names = set(zipfile.ZipFile(path).namelist())
    for expected in ("my-shop/README.txt", "my-shop/report.md", "my-shop/report.html",
                     "my-shop/diagrams/infrastructure.mmd", "my-shop/images/README.txt",
                     "my-shop/data/evidence.json", "my-shop/data/placements.json", "my-shop/data/memory.json"):
        assert expected in names, expected
    html = zipfile.ZipFile(path).read("my-shop/report.html").decode("utf-8")
    assert '<pre class="mermaid">' in html and "mermaid.min.js" in html


def test_zip_includes_svg_images_when_cli_present(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_diagram.shutil, "which", lambda name: "mmdc")

    def fake_run(cmd, **kw):
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write("<svg/>")
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(render_diagram.subprocess, "run", fake_run)
    assert build_zip(tmp_path, monkeypatch, with_cli=True) == 0
    names = set(zipfile.ZipFile(capsys.readouterr().out.strip()).namelist())
    assert "my-shop/images/infrastructure.svg" in names
    assert "my-shop/images/README.txt" not in names


def test_zip_is_reproducible(tmp_path, monkeypatch, capsys):
    build_zip(tmp_path, monkeypatch)
    first = open(capsys.readouterr().out.strip(), "rb").read()
    build_zip(tmp_path, monkeypatch)
    second = open(capsys.readouterr().out.strip(), "rb").read()
    assert first == second


def test_failing_report_is_not_packaged_unless_forced(tmp_path, monkeypatch, capsys):
    bad = make_report(setup("monolith")[1]).replace("Web verification: not performed", "nothing")
    assert build_zip(tmp_path, monkeypatch, report=bad) == 1
    assert not (tmp_path / "out" / "my-shop-architecture.zip").exists()
    assert build_zip(tmp_path, monkeypatch, report=bad, force=True) == 0


def test_extract_diagrams_names_by_heading():
    text = "## 4.1 Cloud infrastructure\n" + DIAGRAM + "\n## 4.2 Cloud infrastructure\n" + DIAGRAM
    assert [n for n, _ in po.extract_diagrams(text)] == ["4-1-cloud-infrastructure", "4-2-cloud-infrastructure"]


def test_html_escapes_content():
    html = po.markdown_to_html("Use <script>alert(1)</script> here\n")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_missing_input_exit_2(tmp_path):
    assert po.main([str(tmp_path / "no.md"), "--evidence", str(tmp_path / "e.json"), "--name", "x"]) == 2


def test_leftover_template_note_fails():
    ev, pl = setup()
    rep = make_report(pl).replace("## Diagrams", "## Diagrams\n(Delete this note.) Instructions here\n")
    assert any("instruction note" in m for m in errors(rep, ev, pl, {"entries": []}))
