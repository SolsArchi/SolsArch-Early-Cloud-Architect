"""The shared architecture contract ties plan decisions, generated resources and cost lines together by id."""
import json
import unittest

import contract
from test_eval_score import FreeTierServerlessScore as Pipeline


class Classification(unittest.TestCase):
    def test_billable_types_map_to_decisions_on_every_cloud(self):
        cases = {"aws_nat_gateway": "egress", "azurerm_nat_gateway": "egress", "google_compute_router_nat": "egress",
                 "aws_lb": "ingress", "azurerm_application_gateway": "ingress", "aws_lambda_function_url": "ingress",
                 "aws_docdb_cluster": "database_mongo", "azurerm_cosmosdb_account": "database_mongo",
                 "aws_rds_cluster": "database", "google_sql_database_instance": "database", "aws_elasticache_replication_group": "cache",
                 "aws_secretsmanager_secret": "secrets", "aws_ssm_parameter": "secrets", "aws_scheduler_schedule": "scheduler",
                 "google_cloud_run_v2_service": "compute", "azurerm_container_app": "compute", "aws_lambda_function": "compute"}
        for rtype, key in cases.items():
            self.assertEqual(contract.classify(rtype)[0], key, rtype)

    def test_a_resource_the_plan_did_not_ask_for_has_no_decision(self):
        self.assertIsNone(contract.decision_for("aws_nat_gateway", {"compute_api", "ingress"}))
        self.assertEqual(contract.decision_for("aws_nat_gateway", {"egress"}), "egress")
        self.assertEqual(contract.decision_for("aws_lambda_function", {"compute_api"}), "compute_api")
        self.assertEqual(contract.decision_for("aws_ecr_repository", {"compute_api"}), "compute_api")

    def test_resource_ids_are_stable(self):
        self.assertEqual(contract.resource_id("ingress", "aws"), "res.ingress.aws")

    def test_terraform_resources_are_parsed(self):
        found = contract.parse_resources({"a.tf": 'resource "aws_vpc" "main" {\n}\nresource "aws_s3_bucket" "x" {\n}\n', "b.txt": 'resource "x" "y" {}'})
        self.assertEqual([(r["type"], r["name"]) for r in found], [("aws_vpc", "main"), ("aws_s3_bucket", "x")])


class OnTheFreeTierRepo(unittest.TestCase):
    """Runs the same pipeline as the readiness test, then checks the contract it wrote."""

    @classmethod
    def setUpClass(cls):
        cls.run_step = classmethod(Pipeline.run_step.__func__)
        Pipeline.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.contract = json.loads((self.repo / ".solsarch" / "contract.json").read_text(encoding="utf-8"))

    def test_contract_is_written_by_finish(self):
        self.assertEqual(self.contract["plan_fingerprint"], self.plan["fingerprint"])
        for part in ("application", "constraints", "architecture", "validation", "evaluation"):
            self.assertIn(part, self.contract)

    def test_every_billable_resource_traces_to_a_decision(self):
        self.assertEqual([r["type"] for r in contract.untraced(self.contract)], [])

    def test_every_billable_decision_has_a_cost_entry(self):
        self.assertEqual(contract.unpriced_decisions(self.contract), [])

    def test_decision_ids_include_the_supplying_provider(self):
        ids = {d["id"] for d in self.contract["architecture"]["decisions"]}
        self.assertIn("res.database_mongo.mongodbatlas", ids)
        self.assertIn("res.ingress.aws", ids)

    def test_contract_is_identical_when_rebuilt(self):
        first = (self.repo / ".solsarch" / "contract.json").read_text(encoding="utf-8")
        contract.write(self.repo)
        self.assertEqual(first, (self.repo / ".solsarch" / "contract.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
