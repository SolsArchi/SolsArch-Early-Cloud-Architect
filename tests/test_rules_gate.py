"""Company rules shape the plan, the generated code and the quality gate, from documents placed beside the code."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import SCRIPTS, write
import qa_gates
import rules

SCRIPT = SCRIPTS / "solsarch.py"
POLICY = """# Acme cloud standards

- Our approved clouds are AWS, Azure and Google Cloud only.
- All customer data must stay in the EU.
- Databases must not be publicly accessible.
- Every resource must carry these tags: owner, cost-center.
- Do not use NAT gateways.
- All data must be encrypted at rest and in transit.
- Audit logs must be retained for 90 days.
- Nobody may deploy on Fridays.
"""


class Base(unittest.TestCase):
    ENV = {"SOLSARCH_OFFLINE": "1", "SOLSARCH_SKIP_VALIDATE": "1", "SOLSARCH_TODAY": "2026-10-23", "SOLSARCH_NO_HISTORY": "1"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        write(self.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\n")
        write(self.repo, "app/main.py", 'from fastapi import FastAPI\napp = FastAPI()\n\n@app.get("/health")\ndef health():\n    return {"ok": True}\n')
        write(self.repo, "Dockerfile", 'FROM python:3.12-slim\nEXPOSE 8000\nCMD ["uvicorn", "app.main:app"]\n')
        self.env = dict(os.environ, **self.ENV)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(self.repo)], capture_output=True, text=True, env=self.env)
        try:
            return done.returncode, json.loads(done.stdout)
        except ValueError:
            return done.returncode, {"raw": done.stdout, "err": done.stderr}

    def build(self, cloud="aws", compute="server", costs=True):
        self.run_cli("scan")
        code, plan = self.run_cli("plan", "--cloud", cloud, "--compute", compute)
        if code != 0:
            return code, plan
        self.run_cli("evidence")
        self.run_cli("iac")
        if costs:
            self.run_cli("costs", "--offline")
        self.run_cli("contract")
        return 0, plan

    def iac(self, name):
        return (self.repo / ".solsarch" / "iac" / name).read_text(encoding="utf-8")


class PlanShaping(Base):
    def test_cloud_outside_the_approved_list_is_refused_with_alternatives(self):
        write(self.repo, "policies/standards.md", POLICY)
        self.run_cli("scan")
        code, out = self.run_cli("plan", "--cloud", "hetzner", "--compute", "server")
        self.assertEqual(code, 1)
        self.assertIn("company rules", out["message"])
        self.assertEqual(out["compliant_clouds"], ["aws", "azure", "gcp"])
        self.assertTrue(out["violations"][0]["rule"].startswith("R-allowed-clouds-"))

    def test_expired_exception_stops_protecting_the_choice(self):
        write(self.repo, "policies/standards.md", POLICY)
        write(self.repo, "policies/rules.json", json.dumps({"exceptions": [{"rule": "allowed_clouds", "reason": "pilot", "owner": "cto", "expires": "2026-12-31"}]}))
        self.run_cli("scan")
        code, out = self.run_cli("plan", "--cloud", "hetzner", "--compute", "server")
        self.assertEqual(code, 0, out)
        self.env["SOLSARCH_TODAY"] = "2027-01-02"
        code, out = self.run_cli("plan", "--cloud", "hetzner", "--compute", "server")
        self.assertEqual(code, 1)

    def test_denied_serverless_blocks_a_serverless_plan(self):
        write(self.repo, "policies/standards.md", "Serverless is not allowed here.\n")
        self.run_cli("scan")
        code, out = self.run_cli("plan", "--cloud", "aws", "--compute", "serverless")
        self.assertEqual(code, 1)
        self.assertIn("serverless", out["message"])

    def test_no_rules_documents_changes_nothing(self):
        code, out = self.build()
        self.assertEqual(code, 0)
        self.assertNotIn("company_rules", out)
        self.assertFalse((self.repo / ".solsarch" / "rules.json").exists())
        self.assertNotIn("company_tags", "".join(p.read_text(encoding="utf-8") for p in (self.repo / ".solsarch" / "iac" / "terraform").glob("*.tf")))

    def test_conflicting_cloud_rules_are_reported(self):
        write(self.repo, "policies/a.json", json.dumps({"allowed_clouds": ["aws"], "denied_clouds": ["aws"]}))
        data = rules.build(self.repo)
        self.assertEqual(data["constraints"]["compliant_clouds"], [])
        self.assertTrue(data["conflicts"])


class GeneratedFiles(Base):
    def setUp(self):
        super().setUp()
        write(self.repo, "policies/standards.md", POLICY)

    def test_region_budget_and_tags_reach_the_generated_code(self):
        code, plan = self.build("aws")
        self.assertEqual(code, 0, plan)
        data = json.loads((self.repo / ".solsarch" / "rules.json").read_text(encoding="utf-8"))
        self.assertEqual(data["resolved_region"]["region"], "eu-central-1")
        self.assertIn("eu-central-1", self.iac("terraform/variables.tf"))
        self.assertIn("merge(var.company_tags", self.iac("terraform/providers.tf"))
        self.assertIn('"cost-center"', self.iac("terraform/company-tags.tf"))
        self.assertIn('"owner"', self.iac("terraform/company-tags.tf"))

    def test_gcp_gets_lowercase_labels_and_a_european_region(self):
        code, _ = self.build("gcp")
        self.assertEqual(code, 0)
        self.assertIn("europe-west3", self.iac("terraform/variables.tf") + self.iac("terraform/terraform.tfvars.example"))
        text = "".join(p.read_text(encoding="utf-8") for p in (self.repo / ".solsarch" / "iac" / "terraform").glob("*.tf"))
        self.assertIn("labels = merge(var.company_tags", text)

    def test_log_retention_is_raised_to_the_rule(self):
        self.build("aws")
        self.assertRegex(self.iac("terraform/variables.tf"), r'variable "log_retention_days" \{[^}]*default\s*=\s*90')

    def test_retention_rounds_up_to_a_value_cloudwatch_accepts(self):
        write(self.repo, "policies/standards.md", "Audit logs must be retained for 45 days.\n")
        self.build("aws")
        self.assertRegex(self.iac("terraform/variables.tf"), r'variable "log_retention_days" \{[^}]*default\s*=\s*60')

    def test_output_is_identical_on_a_second_run(self):
        self.build("aws")
        first = json.loads((self.repo / ".solsarch" / "iac-manifest.json").read_text(encoding="utf-8"))["digest"]
        self.run_cli("iac")
        second = json.loads((self.repo / ".solsarch" / "iac-manifest.json").read_text(encoding="utf-8"))["digest"]
        self.assertEqual(first, second)

    def test_naming_prefix_reaches_the_tfvars_example(self):
        write(self.repo, "policies/standards.md", "Resource names must be prefixed with 'acme-'.\n")
        self.build("aws")
        self.assertRegex(self.iac("terraform/terraform.tfvars.example"), r'project_name\s*=\s*"acme-')


class Gate(Base):
    def setUp(self):
        super().setUp()
        write(self.repo, "policies/standards.md", POLICY)
        self.build("aws")
        self.result = qa_gates.evaluate(self.repo)
        self.by_kind = {}
        data = rules.load(self.repo)
        ids = {r["id"]: r["kind"] for r in data["rules"]}
        for item in self.result["rule_results"]:
            self.by_kind[ids[item["rule"]]] = item

    def test_gate_eight_exists_with_one_check_per_rule(self):
        gate = next(g for g in self.result["gates"] if g["gate"] == 8)
        self.assertEqual(gate["name"], "Company rules")
        self.assertEqual(len(gate["checks"]), len(rules.active(rules.load(self.repo))))

    def test_rules_the_design_meets_pass(self):
        for kind in ("allowed_clouds", "data_residency", "required_tags", "no_public_database", "log_retention_min_days"):
            self.assertEqual(self.by_kind[kind]["status"], "PASS", self.by_kind[kind])

    def test_a_rule_the_design_breaks_fails_and_names_the_evidence(self):
        item = self.by_kind["denied_services"]
        self.assertEqual(item["status"], "FAIL")
        self.assertIn("nat_gateway", item["detail"])

    def test_a_blocking_failure_blocks_deploy_readiness(self):
        write(self.repo, "policies/standards.md", "Databases must not be publicly accessible.\nAll customer data must stay in the United States.\n")
        self.run_cli("profile", "--set", "region=eu-central-1")
        self.build("aws")
        result = qa_gates.evaluate(self.repo)
        self.assertFalse(result["deploy_ready"])
        self.assertTrue(any("data residency" in b["name"] for b in result["blockers"]))

    def test_a_user_set_region_outside_the_rule_fails(self):
        self.run_cli("profile", "--set", "region=us-east-1")
        self.build("aws")
        result = qa_gates.evaluate(self.repo)
        item = next(r for r in result["rule_results"] if r["rule"].startswith("R-data-residency"))
        self.assertEqual(item["status"], "FAIL")
        self.assertIn("us-east-1", item["detail"])

    def test_finish_writes_the_rules_report_and_policy(self):
        code, out = self.run_cli("finish")
        self.assertEqual(code, 0, out)
        report = (self.repo / "docs" / "solsarch-rules.md").read_text(encoding="utf-8")
        self.assertIn("policies/standards.md", report)
        self.assertIn("Attached but not enforced", report)
        self.assertIn("Fridays", report)
        rego = (self.repo / ".solsarch" / "policy" / "solsarch-company.rego").read_text(encoding="utf-8")
        self.assertIn("package solsarch.company", rego)
        self.assertIn("R-denied-services-", rego)
        import zipfile
        names = zipfile.ZipFile(self.repo / "docs" / "solsarch-output.zip").namelist()
        self.assertTrue(any(n.endswith("solsarch-rules.md") for n in names))
        self.assertTrue(any(n.endswith("policy/solsarch-company.rego") for n in names))
        self.assertTrue(any(n.endswith("adr/0001-compute-api.md") or "/adr/0001-" in n for n in names))
        adr = sorted((self.repo / "docs" / "adr").glob("0*.md"))
        self.assertTrue(adr)
        first = adr[0].read_text(encoding="utf-8")
        self.assertIn("## Company rules that apply", first)
        self.assertIn("policies/standards.md", first)
        self.run_cli("adr")
        self.assertEqual(first, adr[0].read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("opa"), "opa is not installed")
    def test_generated_policy_is_valid_rego(self):
        self.run_cli("finish")
        done = subprocess.run(["opa", "check", str(self.repo / ".solsarch" / "policy" / "solsarch-company.rego")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class Compare(Base):
    def test_compare_marks_clouds_the_rules_block(self):
        write(self.repo, "policies/standards.md", "We must not use Azure.\n")
        self.run_cli("scan")
        code, out = self.run_cli("compare")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["blocked_by_company_rules"], ["azure"])
        table = (self.repo / "docs" / "solsarch-cloud-comparison.md").read_text(encoding="utf-8")
        self.assertIn("Company rules", table)
        self.assertIn("blocked: azure is not allowed", table)


class RunCommand(Base):
    def test_run_without_a_cloud_picks_one_the_rules_allow(self):
        write(self.repo, "policies/standards.md", "We must not use AWS.\n")
        code, out = self.run_cli("run")
        self.assertEqual(code, 0, out)
        self.assertNotEqual(out["steps"]["plan"]["cloud"], "aws")
        self.assertIn("company_rules", out)

    def test_rules_flag_reads_documents_outside_the_repo(self):
        with tempfile.TemporaryDirectory() as other:
            write(other, "std.md", "Our approved clouds are Azure only.\n")
            code, out = self.run_cli("run", "--rules", str(Path(other) / "std.md"))
            self.assertEqual(code, 0, out)
            self.assertEqual(out["steps"]["plan"]["cloud"], "azure")


if __name__ == "__main__":
    unittest.main()
