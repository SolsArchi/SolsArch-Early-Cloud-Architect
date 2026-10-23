"""Every hosting provider, in both compute models, scored with the seven quality gates on a streaming FastAPI + MongoDB app.

A combination that can work must have no failing gate. A combination that cannot work (a document database app on a platform with
no document database, an always-on container on an always-free account) must say so by failing the gate that names the reason, and
must not be reported as ready. The expected failures are listed here with their reason, so a new failure shows up as a test error.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import architect
import ast_scanner
import providers
import qa_gates
import recommend
import solsarch
import static_check
from test_eval_score import build_repo

# provider -> (gate checks that are expected to fail, why). Matched on the account type given.
DATA_ONLY = ("neon", "mongodbatlas")  # databases, not application hosts: nothing to deploy the app to
EXPECTED = {
    ("firebase", None): ({"A3", "A4"}, "Firestore is not a MongoDB-compatible service, so the app's Mongo driver cannot run on it."),
    ("aws", "free_tier", "server"): ({"K2"}, "Always-on containers, a load balancer and a NAT gateway bill by the hour; an always-free account has no allowance for them."),
    ("azure", "free_tier", "server"): ({"K2"}, "Same reason on Azure."),
    ("gcp", "free_tier", "server"): ({"K2"}, "Same reason on Google Cloud."),
}


def expected_failures(provider, account, mode):
    out = set()
    for key, (ids, _why) in EXPECTED.items():
        if key[0] == provider and (len(key) == 2 or (key[1], key[2]) == (account, mode)):
            out |= ids
    return out


class Matrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = {k: os.environ.get(k) for k in ("SOLSARCH_OFFLINE", "SOLSARCH_TODAY")}
        os.environ["SOLSARCH_OFFLINE"] = "1"
        os.environ["SOLSARCH_TODAY"] = "2026-10-23"
        cls.tmp = Path(tempfile.mkdtemp())
        cls.snapshot = Path(__file__).parent / "fixtures" / "prices_snapshot_aws_us_east_1.json"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def evaluate(self, provider, mode, account):
        repo = self.tmp / ("%s_%s_%s" % (provider, mode, account))
        repo.mkdir()
        build_repo(repo)
        scan = ast_scanner.scan(repo)
        state = repo / ".solsarch"
        state.mkdir()
        shutil.copy(self.snapshot, state / "prices.json")
        profile = dict(architect.PROFILE_DEFAULTS, account_type=account)
        plan = recommend.build_plan(scan, recommend.load_catalog(), provider, mode, [], profile)
        design = architect.review(repo, scan, plan, profile)
        for name, data in (("scan", scan), ("plan", plan), ("design", design), ("profile", profile)):
            solsarch.write_json(state / (name + ".json"), data)
        solsarch.build_costs(repo, online=False)
        solsarch.build_iac(repo)
        solsarch.build_contract(repo)
        solsarch.write_json(state / "static-check.json", static_check.run(repo))
        result = qa_gates.evaluate(repo)
        shutil.rmtree(repo, ignore_errors=True)
        return result

    def failing(self, result):
        return {c["id"] for g in result["gates"] for c in g["checks"] if c["status"] == qa_gates.FAIL}

    def test_every_provider_and_mode(self):
        for account in ("free_tier", "standard"):
            for provider in providers.hosting_ids():
                if provider in DATA_ONLY:  # used next to another cloud (--also), which the free-tier DBAI test covers
                    continue
                for mode in ("server", "serverless"):
                    with self.subTest(provider=provider, mode=mode, account=account):
                        result = self.evaluate(provider, mode, account)
                        failing = self.failing(result)
                        self.assertEqual(failing - expected_failures(provider, account, mode), set(),
                                         "unexpected failing checks (score %s): %s" % (result["raw_score"], result["contradictions"][:3]))
                        if not failing:
                            self.assertEqual(result["blockers"], [])
                            self.assertEqual(result["contradictions"], [])

    def test_expected_failures_are_really_failing(self):
        for (provider, account, mode), (ids, _why) in ((k, v) for k, v in EXPECTED.items() if len(k) == 3):
            with self.subTest(provider=provider, account=account, mode=mode):
                self.assertTrue(ids <= self.failing(self.evaluate(provider, mode, account)))

    def test_a_failing_combination_is_never_ready_to_deploy(self):
        result = self.evaluate("aws", "server", "free_tier")
        self.assertFalse(result["deploy_ready"])
        self.assertTrue(result["blockers"])

    def test_a_working_combination_scores_high_without_failures(self):
        result = self.evaluate("gcp", "serverless", "free_tier")
        self.assertEqual(self.failing(result), set())
        self.assertGreaterEqual(result["raw_score"], 80)


if __name__ == "__main__":
    unittest.main()
