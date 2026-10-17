import base64
import json
import xml.etree.ElementTree as ET
import zipfile

import icon_diagram as idg
import package_report as po
import render_diagram
from helpers import evidence_for, make_report, placements_for


def svg_for(fixture, cloud, model, **kw):
    return idg.render(placements_for(fixture, cloud, model), evidence_for(fixture), **kw)


def test_valid_xml_for_every_fixture_cloud_model():
    for fixture in ("saas", "rag", "monolith"):
        for cloud in ("aws", "azure", "gcp"):
            for model in ("server", "serverless"):
                ET.fromstring(svg_for(fixture, cloud, model))


def test_every_drawable_placement_appears_with_its_service_name():
    pl = placements_for("rag", "aws", "server")
    svg = idg.render(pl, evidence_for("rag"))
    for p in pl["placements"]:
        if p["category"] in idg.CATEGORY:
            assert ("%s: %s" % (p["id"], p["service"])) in svg


def test_deterministic():
    assert svg_for("saas", "aws", "server") == svg_for("saas", "aws", "server")


def test_vendor_colour_per_cloud():
    assert "#FF9900" in svg_for("saas", "aws", "server")
    assert "#0078D4" in svg_for("saas", "azure", "server")
    assert "#4285F4" in svg_for("saas", "gcp", "server")


def test_function_glyph_for_serverless_compute():
    assert idg.GLYPHS["function"] in svg_for("saas", "aws", "serverless")
    assert idg.GLYPHS["function"] not in svg_for("saas", "aws", "server")


def test_external_llm_node_only_for_real_apis():
    rag = svg_for("rag", "aws", "server")
    assert "OpenAI API (external)" in rag
    assert "LangChain" not in rag
    assert "(external)" not in svg_for("monolith", "aws", "server")


def test_escapes_service_text():
    pl = {"cloud": "aws", "compute_model": "server", "placements": [
        {"id": "P01", "category": "Backend compute", "service": "A <b> & C", "evidence_ids": ["E001"]}]}
    svg = idg.render(pl, None)
    ET.fromstring(svg)
    assert "<b>" not in svg


def test_empty_or_unknown_input_raises():
    for pl in ({"cloud": "aws", "placements": []}, {"cloud": "oracle", "placements": []}):
        try:
            idg.render(pl, None)
            assert False
        except ValueError:
            pass


def test_icon_dir_override_by_service_slug(tmp_path):
    raw = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
    (tmp_path / "application-load-balancer.svg").write_bytes(raw)
    svg = svg_for("saas", "aws", "server", icon_dir=str(tmp_path))
    assert base64.b64encode(raw).decode() in svg
    ET.fromstring(svg)


def test_cli(tmp_path):
    pl = tmp_path / "p.json"
    pl.write_text(json.dumps(placements_for("saas", "aws", "server")), encoding="utf-8")
    out = tmp_path / "o" / "d.svg"
    assert idg.main([str(pl), "--out", str(out)]) == 0
    ET.fromstring(out.read_text(encoding="utf-8"))
    assert idg.main([str(tmp_path / "none.json"), "--out", str(out)]) == 2


def test_package_embeds_icon_svg_in_zip_and_html(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(render_diagram.shutil, "which", lambda name: None)
    ev, pl = evidence_for("saas"), placements_for("saas", "aws", "server")
    (tmp_path / "e.json").write_text(json.dumps(ev), encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps(pl), encoding="utf-8")
    report = make_report(pl).replace("### Infrastructure", "### Infrastructure\n![Icons](images/architecture-icons.svg)\n")
    (tmp_path / "r.md").write_text(report, encoding="utf-8")
    args = [str(tmp_path / "r.md"), "--evidence", str(tmp_path / "e.json"), "--placements", str(tmp_path / "p.json"),
            "--research", str(tmp_path / "none.json"), "--name", "x", "--out-dir", str(tmp_path)]
    assert po.main(args) == 0
    z = zipfile.ZipFile(capsys.readouterr().out.strip())
    assert "x/images/architecture-icons.svg" in z.namelist()
    html = z.read("x/report.html").decode("utf-8")
    assert '<figure class="icons"><svg' in html
    ET.fromstring(z.read("x/images/architecture-icons.svg"))
