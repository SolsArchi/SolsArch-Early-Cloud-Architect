import re
import shutil
import zipfile
from pathlib import Path

import brand
import helpers

ROOT = Path(__file__).resolve().parent.parent
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 50" width="100" height="50"><rect width="100" height="50"/></svg>'


def test_stamp_adds_corner_mark_below_the_drawing():
    out = brand.stamp_svg(SVG)
    assert brand.MARKER in out and "data:image/png;base64," in out
    assert 'viewBox="0 0 100 %d"' % (50 + brand.STRIP) in out
    assert 'height="%g"' % (50 + brand.STRIP) in out
    assert out.rstrip().endswith("</svg>")


def test_stamp_is_idempotent_and_ignores_non_svg():
    once = brand.stamp_svg(SVG)
    assert brand.stamp_svg(once) == once
    assert brand.stamp_svg("not an svg") == "not an svg"
    assert brand.stamp_svg("<svg><rect/></svg>") == "<svg><rect/></svg>"


def test_stamp_handles_mmdc_style_root():
    mmdc = '<svg id="m" width="100%" xmlns="http://www.w3.org/2000/svg" viewBox="-8 -8 216.5 120" style="max-width: 216px;"><g/></svg>'
    out = brand.stamp_svg(mmdc)
    assert 'viewBox="-8 -8 216.5 %g"' % (120 + brand.STRIP) in out
    assert 'width="100%"' in out


def test_cli_stamps_in_place(tmp_path):
    target = tmp_path / "d.svg"
    target.write_text(SVG, encoding="utf-8")
    assert brand.main([str(target)]) == 0
    assert brand.MARKER in target.read_text(encoding="utf-8")
    assert brand.main([]) == 2
    assert brand.main([str(tmp_path / "missing.svg")]) == 2


def test_embedded_mark_is_a_small_valid_png():
    data = brand.png_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) < 20000


def test_every_generated_svg_and_viewer_carries_the_mark(tmp_path):
    repo = tmp_path / "p"
    shutil.copytree(helpers.FIXTURES / "saas", repo)
    out = helpers.pipeline(str(repo))
    svgs = list((repo / "docs" / "diagrams").glob("*.svg"))
    assert len(svgs) >= 2
    for svg in svgs:
        assert brand.MARKER in svg.read_text(encoding="utf-8"), svg.name
    assert (repo / "docs" / "diagrams" / "solsarch-logo.png").is_file()
    assert "solsarch-logo.png" in (repo / "docs" / "solsarch-architecture.md").read_text(encoding="utf-8")
    z = zipfile.ZipFile(out["zip"])
    assert "solsarch-mark" in z.read("report.html").decode("utf-8") or "SolsArch" in z.read("report.html").decode("utf-8")
    assert "solsarch-mark" in z.read("diagrams/index.html").decode("utf-8")
    assert brand.MARKER in z.read("images/architecture-icons.svg").decode("utf-8")


def test_html_report_puts_mark_on_each_mermaid_diagram():
    import package_report
    md = "# T\n\n```mermaid\nflowchart TD\n  a[\"A\"] --> b[\"B\"]\n```\n"
    page = package_report.build_html("T", md)
    assert 'class="solsarch-figure"' in page and 'class="solsarch-mark"' in page


def test_full_size_logos_live_outside_the_skill_and_are_never_installed(tmp_path):
    assert (ROOT / "assets" / "branding" / "solsarch-logo-mark.png").is_file()
    skill = ROOT / "solsarch-early-cloud-architect"
    assert not [p for p in skill.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
    import subprocess
    import sys
    target = tmp_path / "skill.zip"
    done = subprocess.run([sys.executable, str(ROOT / "install.py"), "--zip", str(target)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    names = zipfile.ZipFile(target).namelist()
    assert names and not [n for n in names if re.search(r"\.(png|jpe?g|gif|webp)$", n, re.I) or "branding" in n]
    assert any(n.endswith("scripts/brand.py") for n in names)
