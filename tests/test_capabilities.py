"""Sourced facts: a value is used only while its quote is on record, matches its hash and is fresh."""
import json
import os
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import capabilities

PAGE = "Pricing\nThe free tier includes one million requests and 400,000 GB-seconds per month.\nOther text that is long enough to be a sentence here."
REC = {"id": "t.free", "provider": "aws", "topic": "t", "value": 1000000, "unit": "requests", "source": "https://example.com/p",
       "pattern": "free tier includes", "must_contain": ["one million requests"], "when": ["aws", "serverless"]}


class Records(unittest.TestCase):
    def setUp(self):
        os.environ["SOLSARCH_TODAY"] = "2026-10-23"

    def tearDown(self):
        os.environ.pop("SOLSARCH_TODAY", None)

    def test_unread_record_is_unsourced_and_gives_no_value(self):
        records = {"t.free": dict(REC)}
        self.assertEqual(capabilities.value("t.free", records=records), (None, "unsourced"))

    def test_refresh_reads_the_quote_and_verifies(self):
        new = capabilities.refresh_one(dict(REC), fetch=lambda url: PAGE)
        self.assertIn("one million requests", new["quote"])
        self.assertEqual(capabilities.status(new), "verified")
        self.assertEqual(capabilities.value("t.free", records={"t.free": new}), (1000000, "verified"))

    def test_page_that_no_longer_says_it_is_changed(self):
        good = capabilities.refresh_one(dict(REC), fetch=lambda url: PAGE)
        again = capabilities.refresh_one(good, fetch=lambda url: "The free tier includes five hundred thousand requests every month for everyone.")
        self.assertEqual(capabilities.status(again), "changed")
        self.assertIsNone(capabilities.value("t.free", records={"t.free": again})[0])

    def test_unreadable_page_keeps_the_quote_but_is_not_trusted(self):
        good = capabilities.refresh_one(dict(REC), fetch=lambda url: PAGE)
        down = capabilities.refresh_one(good, fetch=lambda url: "")
        self.assertEqual(capabilities.status(down), "unreadable")
        self.assertIsNone(capabilities.value("t.free", records={"t.free": down})[0])

    def test_old_record_is_stale(self):
        good = dict(capabilities.refresh_one(dict(REC), fetch=lambda url: PAGE), retrieved="2026-08-01")
        self.assertEqual(capabilities.status(good), "stale")
        self.assertIsNone(capabilities.value("t.free", records={"t.free": good})[0])

    def test_edited_quote_fails_the_hash(self):
        good = capabilities.refresh_one(dict(REC), fetch=lambda url: PAGE)
        good["quote"] = good["quote"].replace("one million", "ten million")
        self.assertEqual(capabilities.status(good), "changed")


class Shipped(unittest.TestCase):
    def setUp(self):
        os.environ["SOLSARCH_TODAY"] = "2026-10-23"

    def tearDown(self):
        os.environ.pop("SOLSARCH_TODAY", None)

    def test_every_record_names_an_https_source_and_required_phrases(self):
        records = capabilities.load()
        self.assertGreaterEqual(len(records), 20)
        for rid, rec in records.items():
            with self.subTest(record=rid):
                self.assertTrue(rec["source"].startswith("https://"))
                self.assertTrue(rec["must_contain"])
                self.assertTrue(rec["when"])

    def test_every_recorded_quote_holds_its_required_phrases_and_hash(self):
        for rid, rec in capabilities.load().items():
            if rec.get("quote"):
                with self.subTest(record=rid):
                    self.assertEqual(rec["sha256"], capabilities.digest(rec["quote"]))
                    self.assertTrue(all(p.lower() in rec["quote"].lower() for p in rec["must_contain"]))

    def test_lambda_allowance_is_verified_on_the_recorded_day(self):
        self.assertEqual(capabilities.value("aws.lambda.free_requests"), (1000000, "verified"))
        self.assertEqual(capabilities.value("aws.lambda.free_gb_seconds"), (400000, "verified"))

    def test_relevant_follows_cloud_mode_and_signals(self):
        plan = {"cloud": "aws", "compute_model": "serverless", "decisions": [{"key": "scheduler"}, {"key": "secrets"}], "profile": {"account_type": "free_tier"}}
        ids = set(capabilities.relevant(capabilities.load(), plan, {"streaming": "used"}))
        self.assertIn("aws.lambda.free_requests", ids)
        self.assertIn("aws.lambda.max_timeout_seconds", ids)
        self.assertIn("aws.scheduler.free_invocations", ids)
        self.assertIn("aws.ssm.standard_parameters_free", ids)
        self.assertFalse([i for i in ids if i.startswith(("azure", "gcp"))])
        self.assertNotIn("aws.lambda.max_timeout_seconds", set(capabilities.relevant(capabilities.load(), plan, {})))


class SuppliedQuotes(unittest.TestCase):
    def setUp(self):
        os.environ["SOLSARCH_TODAY"] = "2026-10-23"

    def tearDown(self):
        os.environ.pop("SOLSARCH_TODAY", None)

    def test_add_checks_the_phrases_and_verifies(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                capabilities.add(t, "gcp.cloudrun.free_requests", "Cloud Run is free for a while.")
            new = capabilities.add(t, "gcp.cloudrun.free_requests", "The first 2 million requests per month are free.", "https://cloud.google.com/run/pricing")
            self.assertEqual(capabilities.status(new), "verified")
            self.assertEqual(capabilities.value("gcp.cloudrun.free_requests", t), (2000000, "verified"))
            self.assertTrue((Path(t) / ".solsarch" / "capabilities.json").exists())

    def test_add_rejects_a_source_that_is_not_https(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError):
                capabilities.add(t, "gcp.cloudrun.free_requests", "The first 2 million requests per month are free.", "http://x")


if __name__ == "__main__":
    unittest.main()
