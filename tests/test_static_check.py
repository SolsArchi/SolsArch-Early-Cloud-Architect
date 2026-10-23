"""The built-in static check: quiet on everything the generators write, loud on real mistakes."""
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import iac
import providers
import static_check
from test_iac import build, single_app, spa
from test_matrix import monorepo


class CatchesMistakes(unittest.TestCase):
    def findings(self, files, repo="."):
        return static_check.check_terraform(files) + static_check.check_dockerfiles(files, repo)

    def checks(self, files):
        return {f["check"] for f in self.findings(files)}

    def test_unbalanced_block(self):
        self.assertIn("balanced-blocks", self.checks({"terraform/a.tf": 'resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n'}))

    def test_undeclared_variable(self):
        self.assertIn("undeclared-variable", self.checks({"terraform/a.tf": 'resource "aws_vpc" "main" {\n  cidr_block = var.cidr\n}\n'}))

    def test_declared_variable_is_fine(self):
        text = 'variable "cidr" {\n  type = string\n}\nresource "aws_vpc" "main" {\n  cidr_block = var.cidr\n}\n'
        self.assertEqual(self.findings({"terraform/a.tf": text}), [])

    def test_missing_resource_reference(self):
        text = 'resource "aws_subnet" "a" {\n  vpc_id = aws_vpc.main.id\n}\n'
        self.assertIn("missing-resource", self.checks({"terraform/a.tf": text}))

    def test_braces_inside_strings_do_not_count(self):
        text = 'resource "aws_s3_bucket" "b" {\n  bucket = "a{b"\n}\n'
        self.assertEqual(self.findings({"terraform/a.tf": text}), [])

    def test_placeholder_left_behind(self):
        self.assertIn("placeholder", self.checks({"terraform/a.tf": 'name = "@@project@@"\n'}))

    def test_dockerfile_without_start_command_or_user(self):
        found = self.checks({"docker/x/Dockerfile": "FROM python:3.12\nCOPY . .\n"})
        self.assertTrue({"dockerfile-start", "dockerfile-user"} <= found)

    def test_start_target_must_exist(self):
        with tempfile.TemporaryDirectory() as t:
            Path(t, "src").mkdir()
            Path(t, "src", "api.py").write_text("app = 1\n", encoding="utf-8")
            good = 'FROM python:3.12\nUSER 10001\nCMD ["sh","-c","uvicorn src.api:app --port 8000"]\n'
            bad = good.replace("src.api", "src.nothing")
            self.assertEqual(self.findings({"docker/x/Dockerfile": good}, t), [])
            self.assertIn("start-target", {f["check"] for f in self.findings({"docker/x/Dockerfile": bad}, t)})


class QuietOnGeneratedCode(unittest.TestCase):
    def test_every_provider_and_mode_is_clean(self):
        for provider in providers.hosting_ids():
            for mode in ("server", "serverless"):
                with self.subTest(provider=provider, mode=mode):
                    _, _, _, _, result = build(provider, mode)
                    found = static_check.check_terraform(result["files"]) + static_check.check_outputs(result["files"])
                    self.assertEqual(found, [], [(f["file"], f["detail"]) for f in found[:5]])

    def test_free_tier_lambda_design_is_clean(self):
        _, _, _, _, result = build("aws", "serverless", builder=single_app, profile={"account_type": "free_tier"})
        self.assertEqual(static_check.check_terraform(result["files"]), [])


if __name__ == "__main__":
    unittest.main()
