"""Sentences become rules through fixed patterns; near misses do not; structured files are validated before they reach any generated file."""
import json
import os
import unittest

import helpers  # noqa: F401
import rules_extract as rx
import rules_ingest as ri

CASES = [
    ("Our approved clouds are AWS and Azure only.", "allowed_clouds", ["aws", "azure"], "blocking"),
    ("We must not use Oracle Cloud or Alibaba Cloud for any workload.", "denied_clouds", ["alibaba", "oci"], "blocking"),
    ("Teams should prefer Google Cloud where possible.", "preferred_clouds", ["gcp"], "advisory"),
    ("All customer data must stay in the EU.", "data_residency", ["eu"], "blocking"),
    ("Data must remain in the United Kingdom.", "data_residency", ["uk"], "blocking"),
    ("Workloads must run only in eu-central-1 or eu-west-1.", "allowed_regions", ["eu-central-1", "eu-west-1"], "blocking"),
    ("Databases must not be publicly accessible.", "no_public_database", True, "blocking"),
    ("Storage buckets must never be public.", "no_public_storage", True, "blocking"),
    ("Do not use NAT gateways.", "denied_services", ["nat_gateway"], "required"),
    ("Redis is prohibited.", "denied_services", ["redis"], "blocking"),
    ("All data must be encrypted at rest.", "encryption_at_rest", True, "blocking"),
    ("Traffic must be encrypted in transit with TLS.", "encryption_in_transit", True, "blocking"),
    ("Encryption must use customer-managed keys.", "customer_managed_keys", True, "blocking"),
    ("Every resource must carry these tags: owner, cost-center, environment.", "required_tags", ["cost-center", "environment", "owner"], "blocking"),
    ("Resource names must be prefixed with 'acme-'.", "naming_prefix", "acme-", "blocking"),
    ("Monthly cloud spend must not exceed $2,500 per month.", "budget_monthly", 2500.0, "blocking"),
    ("The budget is 3k USD a month.", "budget_monthly", 3000.0, "required"),
    ("RPO of 15 minutes is required.", "rpo_minutes", 15, "required"),
    ("The RTO must be 2 hours or less.", "rto_minutes", 120, "blocking"),
    ("Production must run in multiple availability zones.", "ha_multi_az", True, "blocking"),
    ("Audit logs must be retained for 90 days.", "log_retention_min_days", 90, "blocking"),
    ("Logs must be deleted after 30 days.", "log_retention_max_days", 30, "blocking"),
    ("We are subject to SOC 2 and GDPR.", "compliance", ["gdpr", "soc2"], "required"),
    ("The platform must comply with DORA.", "compliance", ["dora"], "blocking"),
    ("Developers should prefer serverless where possible.", "compute_model", "serverless", "advisory"),
    ("Terraform is the standard for infrastructure as code.", "iac_tool", ["terraform"], "required"),
    ("All pipelines must use GitHub Actions.", "ci_provider", ["github-actions"], "blocking"),
]

NEAR_MISSES = [
    "Please give us a call if you want to know more about the company.",
    "The US dollar rose against the euro today.",
    "We use Render to render the PDF reports in the browser.",
    "Welcome to the engineering handbook.",
    "Our mascot is a friendly neon sign.",
    "Meetings are held on Tuesdays.",
]


def kinds(sentence):
    return {(k, json.dumps(v if not isinstance(v, list) else sorted(v), sort_keys=True)) for k, v, _ in rx.prose_rules(sentence)}


class Prose(unittest.TestCase):
    def test_table(self):
        for sentence, kind, value, level in CASES:
            with self.subTest(sentence):
                hits = {k: v for k, v, _ in rx.prose_rules(sentence)}
                self.assertIn(kind, hits, sentence)
                got = hits[kind]
                self.assertEqual(sorted(got) if isinstance(got, list) else got, sorted(value) if isinstance(value, list) else value)
                self.assertEqual(rx.level_of(sentence), level, sentence)

    def test_near_misses_make_no_rule(self):
        for sentence in NEAR_MISSES:
            with self.subTest(sentence):
                self.assertEqual(rx.prose_rules(sentence), [], sentence)

    def test_denying_a_cloud_is_not_allowing_it(self):
        hits = {k for k, _, _ in rx.prose_rules("We must not use Oracle Cloud.")}
        self.assertEqual(hits, {"denied_clouds"})

    def test_gdpr_does_not_imply_a_region(self):
        hits = {k for k, _, _ in rx.prose_rules("We are subject to GDPR.")}
        self.assertNotIn("data_residency", hits)

    def test_sentences_that_sound_like_rules_are_listed_not_dropped(self):
        docs = [{"name": "a.md", "kind": "text", "sha256": "x", "bytes": 1, "problem": None, "data": None,
                 "text": "Nobody must deploy on Fridays.\nData must stay in the EU.\nWelcome to the handbook.\n"}]
        result = rx.extract(docs)
        self.assertEqual([r["kind"] for r in result["rules"]], ["data_residency"])
        self.assertEqual(len(result["not_enforced"]), 1)
        self.assertIn("Fridays", result["not_enforced"][0]["sentence"])
        self.assertEqual(result["not_enforced"][0]["locator"], "line 1")

    def test_rule_keeps_its_source_sentence_and_hash(self):
        docs = [{"name": "p/a.md", "kind": "text", "sha256": "x", "bytes": 1, "problem": None, "data": None, "text": "# Heading\n- Databases must not be public.\n"}]
        rule = rx.extract(docs)["rules"][0]
        self.assertEqual(rule["source"]["file"], "p/a.md")
        self.assertEqual(rule["source"]["locator"], "line 2")
        self.assertEqual(rule["origin"], "extracted")
        self.assertEqual(len(rule["source"]["sha256"]), 64)

    def test_ids_are_stable(self):
        docs = [{"name": "a.md", "kind": "text", "sha256": "x", "bytes": 1, "problem": None, "data": None, "text": "Data must stay in the EU.\n"}]
        self.assertEqual(rx.extract(docs)["rules"][0]["id"], rx.extract(docs)["rules"][0]["id"])

    def test_rule_line_syntax(self):
        docs = [{"name": "a.md", "kind": "text", "sha256": "x", "bytes": 1, "problem": None, "data": None,
                 "text": "rule: allowed_clouds = aws, gcp ; level=required ; reason=approved by security\nrule: required_tags = owner, team\n"}]
        result = rx.extract(docs)
        by = {r["kind"]: r for r in result["rules"]}
        self.assertEqual(by["allowed_clouds"]["value"], ["aws", "gcp"])
        self.assertEqual(by["allowed_clouds"]["level"], "required")
        self.assertEqual(by["allowed_clouds"]["origin"], "structured")
        self.assertEqual(by["required_tags"]["value"], ["owner", "team"])


class Structured(unittest.TestCase):
    def extract(self, data, name="r.json"):
        return rx.extract([{"name": name, "kind": "json", "sha256": "x", "bytes": 1, "problem": None, "data": data, "text": json.dumps(data)}])

    def test_rules_list_and_shorthand(self):
        result = self.extract({"rules": [{"kind": "denied_clouds", "value": ["oci"], "level": "blocking", "reason": "no contract"}],
                               "budget_monthly": 500, "required_tags": ["owner"]})
        kinds_ = {r["kind"]: r for r in result["rules"]}
        self.assertEqual(kinds_["denied_clouds"]["reason"], "no contract")
        self.assertEqual(kinds_["budget_monthly"]["value"], 500.0)
        self.assertEqual(result["problems"], [])

    def test_bad_values_are_reported_and_not_enforced(self):
        result = self.extract({"rules": [{"kind": "allowed_clouds", "value": ["atlantis"]}, {"kind": "required_tags", "value": ['x"; rm -rf /']},
                                          {"kind": "naming_prefix", "value": "a b"}, {"kind": "budget_monthly", "value": -5}, {"kind": "nope", "value": 1},
                                          {"kind": "allowed_regions", "value": ["eu west"]}, {"kind": "data_residency", "value": ["mars"]}, {"value": 1}]})
        self.assertEqual(result["rules"], [])
        self.assertEqual(len(result["problems"]), 8)

    def test_cloud_names_are_understood_in_structured_files(self):
        result = self.extract({"allowed_clouds": ["Amazon Web Services", "Google Cloud", "azure"]})
        self.assertEqual(result["rules"][0]["value"], ["aws", "azure", "gcp"])

    def test_exceptions_and_ignores_become_overrides(self):
        result = self.extract({"rules": [], "exceptions": [{"rule": "R-x", "reason": "legacy", "owner": "cto", "expires": "2099-01-01"}], "ignore": ["R-y"]})
        self.assertEqual([(o["rule"], o["ignore"]) for o in result["overrides"]], [("R-x", False), ("R-y", True)])

    def test_value_for_every_kind_is_validated(self):
        good = {"allowed_clouds": ["aws"], "denied_clouds": ["oci"], "preferred_clouds": ["gcp"], "data_residency": ["eu"], "allowed_regions": ["eu-west-1"],
                "denied_services": ["redis"], "no_public_database": True, "no_public_storage": True, "encryption_at_rest": True, "encryption_in_transit": True,
                "customer_managed_keys": True, "required_tags": ["owner"], "naming_prefix": "acme", "budget_monthly": 10, "rpo_minutes": 5, "rto_minutes": 30,
                "ha_multi_az": True, "log_retention_min_days": 30, "log_retention_max_days": 400, "compliance": ["soc2"], "compute_model": "server",
                "iac_tool": ["terraform"], "ci_provider": ["github-actions"]}
        self.assertEqual(set(good), set(rx.KINDS))
        result = self.extract(good)
        self.assertEqual(result["problems"], [])
        self.assertEqual(len(result["rules"]), len(good))


class Regions(unittest.TestCase):
    def test_known_regions(self):
        self.assertEqual(rx.geos_for_region("eu-central-1"), {"eu", "eea", "de"})
        self.assertEqual(rx.geos_for_region("eu-west-2"), {"uk"})
        self.assertEqual(rx.geos_for_region("us-east-1"), {"us"})
        self.assertEqual(rx.geos_for_region("germanywestcentral"), {"eu", "eea", "de"})
        self.assertEqual(rx.geos_for_region("europe-west3"), {"eu", "eea", "de"})
        self.assertEqual(rx.geos_for_region("fsn1"), {"eu", "eea", "de"})
        self.assertIsNone(rx.geos_for_region("middle-earth-1"))

    def test_uk_is_not_the_eu(self):
        self.assertNotIn("eu", rx.geos_for_region("eu-west-2"))
        self.assertNotIn("eu", rx.geos_for_region("europe-west2"))



class SampleFolder(unittest.TestCase):
    def test_the_shipped_example_is_read_without_problems(self):
        from pathlib import Path
        folder = Path(__file__).resolve().parent.parent / "examples" / "company-rules"
        docs, problems = ri.read_all(folder, [str(folder)])
        result = rx.extract(docs)
        self.assertEqual(problems + result["problems"], [])
        found = {r["kind"] for r in result["rules"]}
        for kind in ("allowed_clouds", "data_residency", "no_public_database", "no_public_storage", "encryption_at_rest", "encryption_in_transit",
                     "log_retention_min_days", "required_tags", "budget_monthly", "compliance", "ha_multi_az", "denied_services", "naming_prefix"):
            self.assertIn(kind, found)
        self.assertTrue(any("Fridays" in n["sentence"] for n in result["not_enforced"]))
        self.assertEqual([o["rule"] for o in result["overrides"]], ["denied_services"])


if __name__ == "__main__":
    unittest.main()
