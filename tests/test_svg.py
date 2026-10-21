import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import helpers

import ast_scanner
import recommend
import render_svg

CATALOG = recommend.load_catalog()
NS = {"s": "http://www.w3.org/2000/svg"}


def plan_for(builder, cloud="aws", mode="server"):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app-repo"
        root.mkdir()
        builder(root)
        return recommend.build_plan(ast_scanner.scan(root), CATALOG, cloud, mode)


class SvgTests(unittest.TestCase):
    def test_every_glyph_used_exists_and_is_valid_xml(self):
        for key, glyph in render_svg.KEY_ICON.items():
            self.assertIn(glyph, render_svg.GLYPHS, key)
        for name, inner in render_svg.GLYPHS.items():
            ET.fromstring('<svg xmlns="http://www.w3.org/2000/svg">%s</svg>' % inner)

    def test_every_combination_gives_valid_svg_with_an_icon_per_service(self):
        for repo, builder in (("saas", helpers.build_saas), ("demo", helpers.build_demo)):
            for cloud in CATALOG["clouds"]:
                for mode in CATALOG["modes"]:
                    with self.subTest(repo=repo, cloud=cloud, mode=mode):
                        plan = plan_for(builder, cloud, mode)
                        for render in (render_svg.infrastructure, render_svg.service_sheet):
                            svg = render(plan)
                            if svg is None:  # a data-only provider can have nothing to draw for this repository
                                continue
                            root = ET.fromstring(svg)
                            self.assertTrue(root.attrib["viewBox"])
                            self.assertTrue(root.find("s:title", NS).text)
                            brand = render_svg.palette(cloud)["fill"]
                            self.assertIn(brand, svg)
                        if render_svg.service_sheet(plan) is None:
                            continue
                        sheet = ET.fromstring(render_svg.service_sheet(plan))
                        cards = [t.text for t in sheet.iter("{http://www.w3.org/2000/svg}text")]
                        for decision in plan["decisions"]:
                            self.assertIn(decision["short"][:28], cards)

    def test_infrastructure_names_every_zone_service(self):
        plan = plan_for(helpers.build_demo)
        svg = render_svg.infrastructure(plan)
        for key in ("edge", "ingress", "compute_web", "compute_api", "database", "vector", "egress", "secrets", "observability", "registry"):
            decision = next(d for d in plan["decisions"] if d["key"] == key)
            for word in decision["short"].split():  # long names wrap onto two lines
                self.assertIn(word, svg)
        self.assertIn("OpenAI API", svg)
        self.assertIn("Public zone", svg)
        self.assertIn("VPC private network", svg)

    def test_serverless_uses_function_icon_on_aws_and_azure_only(self):
        for cloud, expected in (("aws", True), ("azure", True), ("gcp", False)):
            plan = plan_for(helpers.build_saas, cloud, "serverless")
            decision = next(d for d in plan["decisions"] if d["key"] == "compute_web")
            self.assertEqual(render_svg.icon_for(decision, "serverless") == "function", expected, cloud)

    def test_deterministic_and_escaped(self):
        plan = plan_for(helpers.build_demo)
        self.assertEqual(render_svg.infrastructure(plan), render_svg.infrastructure(plan))
        plan["decisions"][0]["short"] = 'A & B <script>"x"'
        ET.fromstring(render_svg.infrastructure(plan))
        ET.fromstring(render_svg.service_sheet(plan))
        self.assertNotIn("<script>", render_svg.service_sheet(plan))

    def test_nothing_to_draw_returns_none(self):
        plan = plan_for(helpers.build_terraform)
        self.assertIsNone(render_svg.infrastructure(plan))
        self.assertIsNone(render_svg.service_sheet(plan))

    def test_long_names_are_wrapped_not_cut_off(self):
        plan = plan_for(helpers.build_demo)
        svg = render_svg.infrastructure(plan)
        self.assertIn("Application Load", svg)
        self.assertIn("Balancer", svg)
        self.assertNotIn("Load...", svg)

    def test_pictures_reach_the_report_and_the_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "shop"
            repo.mkdir()
            helpers.build_demo(repo)
            out = helpers.pipeline(repo, "azure", "server")
            report = (repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")
            self.assertIn("![Cloud infrastructure with service icons](diagrams/infrastructure-icons.svg)", report)
            self.assertIn("![Services chosen, with icons](diagrams/service-cards.svg)", report)
            architecture = (repo / "docs" / "solsarch-architecture.md").read_text(encoding="utf-8")
            self.assertIn("diagrams/infrastructure-icons.svg", architecture)
            for name in ("infrastructure-icons.svg", "service-cards.svg"):
                self.assertTrue((repo / "docs" / "diagrams" / name).exists())
            with zipfile.ZipFile(out["zip"]) as archive:
                names = set(archive.namelist())
                self.assertEqual({"diagrams/infrastructure-icons.svg", "diagrams/service-cards.svg"} - names, set())
                viewer = archive.read("diagrams/index.html").decode("utf-8")
                ET.fromstring(archive.read("diagrams/infrastructure-icons.svg"))
            self.assertIn('<img src="infrastructure-icons.svg"', viewer)

    def test_rerun_with_nothing_to_draw_removes_old_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "shop"
            repo.mkdir()
            helpers.build_demo(repo)
            helpers.pipeline(repo)
            for path in repo.iterdir():
                if path.is_file():
                    path.unlink()
            for sub in ("app", "web"):
                for path in (repo / sub).iterdir():
                    path.unlink()
            (repo / "Dockerfile").unlink() if (repo / "Dockerfile").exists() else None
            helpers.write(repo, "README.md", "# empty\n")
            helpers.pipeline(repo)
            self.assertEqual(list((repo / "docs" / "diagrams").glob("*.svg")), [])


if __name__ == "__main__":
    unittest.main()
