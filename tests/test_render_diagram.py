import subprocess

import render_diagram as rd

GOOD = '# T\n\n```mermaid\nflowchart TD\n  a["A"] --> b["B"]\n```\n\n```mermaid\nsequenceDiagram\n  A->>B: hi\n```\n'


def test_missing_mmdc_exits_3(tmp_path, monkeypatch, capsys):
    f = tmp_path / "d.md"
    f.write_text(GOOD, encoding="utf-8")
    monkeypatch.setattr(rd.shutil, "which", lambda name: None)
    assert rd.main([str(f)]) == 3
    assert "mermaid.live" in capsys.readouterr().err


def test_invalid_diagram_exits_1_before_rendering(tmp_path, monkeypatch):
    f = tmp_path / "d.md"
    f.write_text("```mermaid\nflowchart TD\n  a[Bad (x)]\n```\n", encoding="utf-8")
    monkeypatch.setattr(rd.shutil, "which", lambda name: "mmdc")
    called = []
    monkeypatch.setattr(rd.subprocess, "run", lambda *a, **k: called.append(1))
    assert rd.main([str(f)]) == 1
    assert not called


def test_renders_each_block(tmp_path, monkeypatch, capsys):
    f = tmp_path / "d.md"
    f.write_text(GOOD, encoding="utf-8")
    monkeypatch.setattr(rd.shutil, "which", lambda name: "mmdc")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(rd.subprocess, "run", fake_run)
    assert rd.main([str(f), "--format", "png", "--out-dir", str(tmp_path / "img")]) == 0
    assert len(seen) == 2 and seen[0][0] == "mmdc"
    out = capsys.readouterr().out.split()
    assert out[0].endswith("d-1.png") and out[1].endswith("d-2.png")


def test_render_failure_exits_1(tmp_path, monkeypatch):
    f = tmp_path / "d.mmd"
    f.write_text('flowchart TD\n  a["A"]\n', encoding="utf-8")
    monkeypatch.setattr(rd.shutil, "which", lambda name: "mmdc")
    monkeypatch.setattr(rd.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, "", "boom"))
    assert rd.main([str(f)]) == 1


def test_unreadable_file_exits_2(tmp_path):
    assert rd.main([str(tmp_path / "nope.md")]) == 2
