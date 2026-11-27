"""The benchmark kit has 20 fixtures in five categories, deterministic metrics and a compare step that accepts outside results."""
import json
import tempfile
import unittest
from pathlib import Path

import benchmark


class Benchmark(unittest.TestCase):
    def test_twenty_fixtures_four_in_each_of_five_categories(self):
        rows = benchmark.all_fixtures()
        self.assertEqual(len(rows), 20)
        for category in benchmark.CATEGORIES:
            self.assertEqual(sum(1 for c, _, _ in rows if c == category), 4, category)
        self.assertEqual(len({name for _, name, _ in rows}), 20)

    def test_fixtures_hold_no_secret_like_value(self):
        for _, name, files in benchmark.all_fixtures():
            for relative, content in files.items():
                self.assertNotRegex(content, r"AKIA[0-9A-Z]{16}|sk_live_|-----BEGIN", (name, relative))

    def test_one_fixture_runs_and_the_metrics_do_not_change_between_runs(self):
        first = benchmark.run(only={"sf1-flask-api"})
        second = benchmark.run(only={"sf1-flask-api"})
        self.assertTrue(first["results"][0]["ok"])
        for result in (first, second):
            for row in result["results"]:
                row.pop("seconds")
        self.assertEqual(first, second)
        row = first["results"][0]
        for key in benchmark.METRIC_FIELDS:
            self.assertIn(key, row)

    def test_compare_puts_results_side_by_side_and_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as d:
            ours = {"tool": "solsarch", "results": [{"fixture": "a", "quality_score": 80}], "summary": {"completed": 1, "mean_quality_score": 80.0}}
            theirs = {"tool": "manual", "results": [{"fixture": "a", "quality_score": 60}, {"fixture": "b", "quality_score": 55}], "summary": {"completed": 2, "mean_quality_score": 57.5}}
            paths = []
            for name, data in (("ours.json", ours), ("theirs.json", theirs)):
                (Path(d) / name).write_text(json.dumps(data), encoding="utf-8")
                paths.append(str(Path(d) / name))
            table = benchmark.compare(paths)
            self.assertIn("| a | 80 | 60 |", table)
            self.assertIn("| b |  | 55 |", table)
            (Path(d) / "bad.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                benchmark.compare([str(Path(d) / "bad.json")])

    def test_summary_counts_completed_runs_only(self):
        rows = [{"ok": True, "category": "ai_agentic", "quality_score": 80, "deploy_ready": False, "blockers": 0, "contradictions": 0, "ledger_grounded_share": 0.3, "verified_claims_without_proof": 0},
                {"ok": False, "category": "ai_agentic"}]
        summary = benchmark.summarise(rows)
        self.assertEqual((summary["fixtures"], summary["completed"], summary["mean_quality_score"]), (2, 1, 80.0))


if __name__ == "__main__":
    unittest.main()
