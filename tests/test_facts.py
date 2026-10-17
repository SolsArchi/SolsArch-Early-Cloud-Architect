import json
import tempfile
import unittest
from pathlib import Path

import helpers

import facts

GOOD_URL = "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AWS_Fargate.html"
GOOD_QUOTE = "AWS Fargate is a technology that you can use with Amazon ECS to run containers"


class FactsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "app-repo"
        self.repo.mkdir()
        helpers.build_demo(self.repo)
        helpers.cli("scan", "--repo", self.repo)
        helpers.cli("plan", "--repo", self.repo, "--cloud", "aws", "--compute", "server")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, **overrides):
        args = dict(key="compute_api", url=GOOD_URL, quote=GOOD_QUOTE, verdict="confirmed")
        args.update(overrides)
        argv = ["facts", "add", "--repo", self.repo]
        for name, value in args.items():
            if value is not None:
                argv += ["--" + name, value]
        return helpers.cli(*argv)

    def test_accepts_a_good_record_and_covers_shared_service(self):
        code, out = self.add()
        self.assertEqual((code, out["status"]), (0, "recorded"))
        self.assertEqual(sorted(out["covers"]), ["compute_api", "compute_web"])
        data = json.loads((self.repo / ".solsarch" / "facts.json").read_text(encoding="utf-8"))
        self.assertEqual({f["key"] for f in data["facts"]}, {"compute_api", "compute_web"})
        self.assertEqual(len(data["facts"][0]["date"]), 10)

    def test_rejects_non_vendor_or_insecure_urls(self):
        for url in ("https://example.com/fargate", "http://docs.aws.amazon.com/x", "https://aws.amazon.com.evil.com/x",
                    "https://notaws.amazon.com.example.org/", "docs.aws.amazon.com/x"):
            with self.subTest(url=url):
                code, out = self.add(url=url)
                self.assertEqual((code, out["status"]), (1, "rejected"))
                self.assertIn("vendor", out["message"])

    def test_rejects_wrong_cloud_domain(self):
        code, out = self.add(url="https://learn.microsoft.com/azure/container-apps/overview")
        self.assertEqual(code, 1)

    def test_rejects_weak_quotes(self):
        self.assertEqual(self.add(quote="short")[0], 1)
        code, out = self.add(quote="This sentence is long enough but talks about something unrelated")
        self.assertEqual(code, 1)
        self.assertIn("mention the service", out["message"])

    def test_rejects_pointer_sentences(self):
        for quote in ("For AWS Fargate, see Amazon ECS documentation.",
                      "See the AWS Fargate documentation for more detail about running tasks",
                      "Learn more about AWS Fargate and how it works on the next page"):
            with self.subTest(quote=quote):
                code, out = self.add(quote=quote)
                self.assertEqual(code, 1)
                self.assertIn("pointer", out["message"])
        self.assertEqual(self.add(quote="AWS Fargate removes the need to provision and manage servers for containers")[0], 0)

    def test_accepts_subdomains_of_vendor_domains(self):
        self.assertTrue(facts.host_allowed("https://docs.cloud.google.com/run/docs", ["cloud.google.com"]))
        code, out = helpers.cli("facts", "queue", "--repo", self.repo)
        self.assertIn("solsarch.py facts add", out["how"])
        self.assertIn("subdomains", out["how"])

    def test_changed_and_unavailable_need_a_note(self):
        for verdict in ("changed", "unavailable"):
            code, out = self.add(verdict=verdict)
            self.assertEqual(code, 1)
            self.assertIn("--note", out["message"])
            self.assertEqual(self.add(verdict=verdict, note="Renamed to something else")[0], 0)

    def test_rejects_unknown_key_and_verdict(self):
        self.assertEqual(self.add(key="nope")[0], 1)
        self.assertEqual(self.add(verdict="maybe")[0], 1)

    def test_queue_shrinks_and_lists_each_service_once(self):
        code, out = helpers.cli("facts", "queue", "--repo", self.repo, "--limit", "50")
        before = [q["service"] for q in out["pending"]]
        self.assertEqual(len(before), len(set(before)))
        self.assertTrue(all(q["query"].startswith(q["service"]) and q["domains"] for q in out["pending"]))
        self.add()
        after = helpers.cli("facts", "queue", "--repo", self.repo, "--limit", "50")[1]["pending"]
        self.assertEqual(len(after), len(before) - 1)
        self.assertEqual(len(helpers.cli("facts", "queue", "--repo", self.repo, "--limit", "2")[1]["pending"]), 2)

    def test_skip_is_recorded_and_cleared_by_a_real_check(self):
        code, out = helpers.cli("facts", "skip", "--repo", self.repo, "--reason", "no web search tool")
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(facts.load_facts(self.repo)["skipped"]["reason"], "no web search tool")
        self.add()
        self.assertIsNone(facts.load_facts(self.repo)["skipped"])

    def test_changing_the_plan_drops_checks_for_services_no_longer_used(self):
        self.add()
        helpers.cli("plan", "--repo", self.repo, "--cloud", "azure", "--compute", "server")
        self.assertEqual(facts.load_facts(self.repo)["facts"], [])

    def test_keeping_the_plan_keeps_the_checks(self):
        self.add()
        helpers.cli("plan", "--repo", self.repo, "--cloud", "aws", "--compute", "server")
        self.assertEqual(len(facts.load_facts(self.repo)["facts"]), 2)

    def test_needs_a_plan(self):
        with tempfile.TemporaryDirectory() as empty:
            code, out = helpers.cli("facts", "queue", "--repo", empty)
            self.assertEqual((code, out["status"]), (2, "error"))

    def test_token_rules(self):
        self.assertEqual(facts.significant_tokens("Cloud Run (minimum 1 instance)"), ["run"])
        self.assertIn("fargate", facts.significant_tokens("Amazon ECS on AWS Fargate"))
        self.assertTrue(facts.host_allowed("https://docs.aws.amazon.com/x", ["aws.amazon.com", "docs.aws.amazon.com"]))
        self.assertTrue(facts.host_allowed("https://aws.amazon.com/x", ["aws.amazon.com"]))
        self.assertFalse(facts.host_allowed("https://evilaws.amazon.com.example/x", ["aws.amazon.com"]))


if __name__ == "__main__":
    unittest.main()
