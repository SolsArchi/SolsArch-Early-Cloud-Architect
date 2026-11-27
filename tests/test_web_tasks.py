"""`lookups` gathers every open web lookup into one ordered, repeatable list with the search, the official sites and the recording command."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from helpers import SCRIPTS, write
import web_tasks

SCRIPT = SCRIPTS / "solsarch.py"


class Lookups(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name)
        write(cls.repo, "requirements.txt", "fastapi==0.110.0\npsycopg2-binary\nredis\n")
        write(cls.repo, "app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
        write(cls.repo, "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        cls.env = dict(os.environ, SOLSARCH_OFFLINE="1", SOLSARCH_SKIP_VALIDATE="1", SOLSARCH_TODAY="2026-10-23", SOLSARCH_NO_HISTORY="1")
        for args in (["scan"], ["plan", "--cloud", "aws", "--compute", "server"], ["finish"]):
            cls.run_cli(*args)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def run_cli(cls, *args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + list(args) + ["--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        return done.returncode, done.stdout

    def lookups(self):
        code, out = self.run_cli("lookups")
        self.assertEqual(code, 0, out)
        return json.loads(out)

    def test_every_item_says_what_to_search_where_to_look_and_how_to_record(self):
        data = self.lookups()
        self.assertGreater(data["open"], 0)
        self.assertEqual(data["open"], len(data["tasks"]))
        for t in data["tasks"]:
            self.assertTrue(t["id"].startswith("L"))
            self.assertTrue(t["search"] and t["search"][0])
            self.assertTrue(t["record"])
            self.assertTrue(t["why"])
            if t["kind"] != "end_of_life":
                self.assertTrue(t["official_domains"], t)

    def test_service_names_come_first_and_ids_are_in_order(self):
        data = self.lookups()
        ids = [t["id"] for t in data["tasks"]]
        self.assertEqual(ids, sorted(ids))
        kinds = [t["kind"] for t in data["tasks"]]
        self.assertEqual(kinds[0], "service_check")
        self.assertLessEqual(kinds.index("service_check"), kinds.index("price") if "price" in kinds else 0)

    def test_same_state_gives_the_same_list(self):
        self.assertEqual(self.lookups(), self.lookups())

    def test_recording_an_answer_removes_it_from_the_list(self):
        first = next(t for t in self.lookups()["tasks"] if t["kind"] == "service_check")
        key = first["record"].split("--key ")[1].split()[0]
        quote = "%s is a managed service offered by the provider and described in its official documentation." % first["subject"]
        domain = first["official_domains"][0]
        code, out = self.run_cli("facts", "add", "--key", key, "--url", "https://%s/service" % domain, "--quote", quote, "--verdict", "confirmed")
        self.assertEqual(code, 0, out)
        left = [t["subject"] for t in self.lookups()["tasks"] if t["kind"] == "service_check"]
        self.assertNotIn(first["subject"], left)

    def test_no_plan_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as empty:
            done = subprocess.run([sys.executable, "-I", str(SCRIPT), "lookups", "--repo", empty], capture_output=True, text=True, env=self.env)
            self.assertEqual(done.returncode, 2)
            self.assertIn("No plan found", done.stdout)

    def test_agent_tool_and_guide_list_it(self):
        import agent_tools
        self.assertIn("solsarch_lookups", agent_tools.BY_NAME)
        out = subprocess.run([sys.executable, "-I", str(SCRIPT), "guide"], capture_output=True, text=True, env=self.env).stdout
        self.assertIn('"lookups"', out)


if __name__ == "__main__":
    unittest.main()
