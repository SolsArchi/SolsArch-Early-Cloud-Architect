import json
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import write

import ast_scanner
import build_diagrams
import recommend

CATALOG = recommend.load_catalog()
BUILDERS = {"saas": helpers.build_saas, "rag": helpers.build_demo, "terraform": helpers.build_terraform}


def scan_of(builder):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "app-repo"
        root.mkdir()
        builder(root)
        return ast_scanner.scan(root)


def plan_of(scan, cloud="aws", mode="server"):
    return recommend.build_plan(scan, CATALOG, cloud, mode)


def keys(plan):
    return {d["key"] for d in plan["decisions"]}


class CatalogTests(unittest.TestCase):
    def test_every_need_covers_every_cloud_and_mode(self):
        for need, clouds in CATALOG["services"].items():
            if need in ("secrets_lean", "ingress_function_url"):  # AWS-only cheaper variants; other clouds use the normal need
                continue
            for cloud in ("aws", "azure", "gcp"):
                options = clouds[cloud]
                for mode in CATALOG["modes"]:
                    entry = options.get(mode) or options.get("any")
                    self.assertTrue(entry, "%s/%s/%s" % (need, cloud, mode))
                    for field in ("service", "short", "why"):
                        self.assertTrue(entry[field].strip(), "%s/%s %s" % (need, cloud, field))
                    for alt in entry.get("alternatives", []):
                        self.assertTrue(alt["service"] and alt["reason"])

    def test_cloud_domains_are_https_vendor_hosts(self):
        for info in CATALOG["clouds"].values():
            self.assertTrue(info["domains"])
            self.assertTrue(all("." in d and "/" not in d for d in info["domains"]))


class PlanTests(unittest.TestCase):
    def test_deterministic(self):
        scan = scan_of(helpers.build_demo)
        first, second = plan_of(scan), plan_of(json.loads(json.dumps(scan)))
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["fingerprint"], plan_of(scan, "azure")["fingerprint"])
        self.assertNotEqual(first["fingerprint"], plan_of(scan, "aws", "serverless")["fingerprint"])

    def test_every_combination_is_complete_and_draws(self):
        for name, builder in BUILDERS.items():
            scan = scan_of(builder)
            for cloud in CATALOG["clouds"]:
                for mode in CATALOG["modes"]:
                    with self.subTest(repo=name, cloud=cloud, mode=mode):
                        plan = plan_of(scan, cloud, mode)
                        self.assertEqual(len(keys(plan)), len(plan["decisions"]))
                        for d in plan["decisions"]:
                            self.assertTrue(d["service"].strip() and d["why"].strip() and d["reason"].strip())
                            self.assertTrue(d["evidence"], d["key"])
                            self.assertIn(d["confidence"], ("confirmed", "inferred"))
                        services = [q["service"] for q in plan["verify_queue"]]
                        self.assertEqual(len(services), len(set(services)))
                        covered = {k for q in plan["verify_queue"] for k in q["keys"]}
                        self.assertEqual(covered, keys(plan) - {"vector"})
                        diagrams = build_diagrams.build(plan, scan)
                        self.assertEqual(len(diagrams), len({d["id"] for d in diagrams}))
                        for diagram in diagrams:
                            self.assertTrue(diagram["what"] and diagram["read"] and diagram["limits"])

    def test_saas_server_plan(self):
        plan = plan_of(scan_of(helpers.build_saas))
        self.assertEqual(keys(plan), {"compute_web", "compute_worker", "database", "cache", "network", "ingress",
                                      "edge", "secrets", "registry", "observability"})
        by = {d["key"]: d for d in plan["decisions"]}
        self.assertEqual(by["compute_web"]["service"], "Amazon ECS on AWS Fargate")
        self.assertEqual(by["database"]["service"], "Amazon Aurora PostgreSQL")
        self.assertIn("BullMQ", by["cache"]["why"])
        self.assertEqual(plan["external_services"], [])

    def test_rag_plan_folds_sqlite_and_chroma_into_postgres(self):
        plan = plan_of(scan_of(helpers.build_demo))
        by = {d["key"]: d for d in plan["decisions"]}
        self.assertEqual(by["database"]["short"], "Aurora PostgreSQL")
        self.assertIn("SQLite", by["database"]["replaces"])
        self.assertEqual(by["vector"]["short"], "pgvector")
        self.assertIn("ChromaDB", by["vector"]["replaces"])
        self.assertEqual(by["secrets"]["confidence"], "confirmed")
        self.assertEqual([x["name"] for x in plan["external_services"]], ["OpenAI API"])
        self.assertIn("egress", by)
        self.assertTrue(any("streaming import" in q for q in plan["open_questions"]))

    def test_serverless_changes_services_and_adds_caveats(self):
        scan = scan_of(helpers.build_demo)
        by = {d["key"]: d for d in plan_of(scan, "aws", "serverless")["decisions"]}
        self.assertEqual(by["compute_api"]["short"], "Lambda")
        self.assertEqual(by["ingress"]["short"], "API Gateway")
        self.assertEqual(by["database"]["short"], "Aurora Serverless v2")
        self.assertNotIn("registry", by)
        self.assertTrue(any("streaming" in c for c in by["compute_api"]["caveats"]))
        self.assertTrue(any("adapter" in c and "Amazon ECS on AWS Fargate" in c for c in by["compute_api"]["caveats"]))
        gcp = {d["key"]: d for d in plan_of(scan, "gcp", "serverless")["decisions"]}
        self.assertFalse(any("adapter" in c for c in gcp["compute_api"]["caveats"]))
        saas = {d["key"]: d for d in plan_of(scan_of(helpers.build_saas), "gcp", "serverless")["decisions"]}
        self.assertTrue(any("handler" in c for c in saas["compute_worker"]["caveats"]))
        self.assertEqual(saas["compute_web"]["short"], "Cloud Run")
        self.assertEqual(saas["compute_web"]["service"], "Cloud Run (scales to zero)")

    def test_each_cloud_uses_its_own_products(self):
        scan = scan_of(helpers.build_demo)
        expected = {"aws": "Aurora PostgreSQL", "azure": "PostgreSQL Flexible Server", "gcp": "Cloud SQL PostgreSQL"}
        for cloud, short in expected.items():
            by = {d["key"]: d for d in plan_of(scan, cloud)["decisions"]}
            self.assertEqual(by["database"]["short"], short)
            self.assertEqual(by["database"]["cloud"], cloud)

    def test_other_engines(self):
        def build(root):
            write(root, "requirements.txt", "django\npymongo\nconfluent-kafka\npika\nmeilisearch\nminio\nvllm\nflask-login\n")
            write(root, "Dockerfile", "FROM python:3.12\nUSER app\n")
        plan = plan_of(scan_of(build), "gcp")
        by = {d["key"]: d for d in plan["decisions"]}
        self.assertEqual(by["database_mongo"]["short"], "MongoDB Atlas")
        self.assertEqual({"queue_kafka", "queue_rabbitmq", "search", "storage", "gpu"} - keys(plan), set())
        self.assertIn("Meilisearch", by["search"]["replaces"])

    def test_vector_service_when_database_cannot_hold_vectors(self):
        def build(root):
            write(root, "requirements.txt", "fastapi\npymongo\nchromadb\n")
        by = {d["key"]: d for d in plan_of(scan_of(build), "azure")["decisions"]}
        self.assertEqual(by["vector"]["short"], "AI Search")

    def test_no_compute_repo_asks_instead_of_guessing(self):
        plan = plan_of(scan_of(helpers.build_terraform))
        self.assertFalse(any(k.startswith("compute_") for k in keys(plan)))
        self.assertNotIn("ingress", keys(plan))
        self.assertTrue(any("No application framework" in q for q in plan["open_questions"]))
        self.assertEqual(build_diagrams.build(plan, scan_of(helpers.build_terraform)), [])

    def test_static_front_end_only(self):
        def build(root):
            write(root, "package.json", '{"dependencies": {"react": "18"}}')
        plan = plan_of(scan_of(build), "azure")
        self.assertEqual({d["key"] for d in plan["decisions"] if d["layer"] == "compute"}, {"static_site"})
        self.assertEqual(next(d for d in plan["decisions"] if d["key"] == "static_site")["short"], "Static Web Apps")

    def test_invalid_choices(self):
        scan = scan_of(helpers.build_saas)
        with self.assertRaises(ValueError):
            recommend.build_plan(scan, CATALOG, "oracle", "server")
        with self.assertRaises(ValueError):
            recommend.build_plan(scan, CATALOG, "aws", "lambda")

    def test_test_file_evidence_does_not_create_decisions(self):
        def build(root):
            write(root, "requirements.txt", "fastapi\n")
            write(root, "tests/test_db.py", "import sqlite3\n")
        self.assertNotIn("database", keys(plan_of(scan_of(build))))

    def test_all_evidence_cites_real_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app-repo"
            root.mkdir()
            helpers.build_demo(root)
            plan = plan_of(ast_scanner.scan(root))
            for d in plan["decisions"]:
                for e in d["evidence"]:
                    if e["kind"] == "file":
                        self.assertTrue((root / e["file"]).exists(), e)
                        self.assertGreaterEqual(e["line"], 1)


if __name__ == "__main__":
    unittest.main()
