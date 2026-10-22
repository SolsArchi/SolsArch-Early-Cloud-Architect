"""Regression for a streaming FastAPI + MongoDB + Redis + scheduler app on an always-free AWS account.

Three outside reviews scored this design 43 to 58 out of 100 because the first version chose DocumentDB, ElastiCache, a NAT
gateway, API Gateway and Secrets Manager, none of which has a free allowance. These tests hold the fixed design to above 85.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import write as w

SCRIPT = Path(__file__).resolve().parent.parent / "solsarch-early-cloud-architect" / "scripts" / "solsarch.py"
FORBIDDEN = ("aws_nat_gateway", "aws_docdb_cluster", "aws_elasticache_replication_group", "aws_elasticache_cluster",
             "aws_secretsmanager_secret", "aws_lb", "aws_apigatewayv2_api", "aws_vpc")


def build_repo(root):
    w(root, "DBAI/requirements.txt", "fastapi\nuvicorn\nlanggraph\nlangchain-groq\nmotor\npymongo\nredis\napscheduler\nmcp\nsse-starlette\npython-dotenv\n")
    w(root, "DBAI/src/api.py", """import os
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from motor.motor_asyncio import AsyncIOMotorClient
import redis
app = FastAPI()
scheduler = AsyncIOScheduler()
GROQ = os.environ["GROQ_API_KEY"]
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
MONGO = os.getenv("CHAT_MONGODB_URL")
@app.on_event("startup")
async def start():
    scheduler.start()
@app.get("/health")
def health():
    return {"ok": True}
@app.post("/chat")
async def chat():
    async def gen():
        yield "data: hello\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
""")
    w(root, "DBAI/.env.example", "GROQ_API_KEY=\nREDIS_URL=\nCHAT_MONGODB_URL=\n")
    w(root, "frontend/package.json", '{"dependencies":{"next":"14.2.0","react":"18","@copilotkit/react-core":"1"}}')
    w(root, "frontend/lib/sse.ts", "export const stream = () => new EventSource('/api/chat')\n")


class FreeTierServerlessScore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.tmp.name) / "dbai"
        build_repo(cls.repo)
        cls.env = dict(os.environ, SOLSARCH_OFFLINE="1", PYTHONIOENCODING="utf-8")
        for args in (["scan"], ["profile", "--set", "account_type=free_tier"], ["plan", "--cloud", "aws", "--compute", "serverless"],
                     ["facts", "skip", "--reason", "test run"], ["finish"]):
            cls.run_step(args)
        state = cls.repo / ".solsarch"
        # terraform may not be installed where the tests run, so record the result a passing validate would write
        (state / "validation.json").write_text(json.dumps({"verdict": "passed", "failed": [], "skipped": []}), encoding="utf-8")
        gate = subprocess.run([sys.executable, "-I", str(SCRIPT), "gate", "--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        cls.gate = json.loads(gate.stdout)
        cls.plan = json.loads((state / "plan.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads((state / "iac-manifest.json").read_text(encoding="utf-8"))

    @classmethod
    def run_step(cls, args):
        done = subprocess.run([sys.executable, "-I", str(SCRIPT)] + args + ["--repo", str(cls.repo)], capture_output=True, text=True, env=cls.env)
        if done.returncode not in (0, 1):
            raise AssertionError("%s failed: %s" % (args, done.stdout[-400:] + done.stderr[-400:]))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def services(self):
        return {d["key"]: d["service"] for d in self.plan["decisions"]}

    def test_readiness_is_above_the_threshold(self):
        self.assertGreater(self.gate["readiness"]["score"], 85, self.gate["readiness"]["parts"])

    def test_no_contradiction_between_plan_code_and_costs(self):
        row = next(c for c in self.gate["checks"] if c["check"] == "Plan, code and cost report agree")
        self.assertEqual(row["status"], "pass", row["items"])

    def test_free_services_are_chosen(self):
        s = self.services()
        self.assertEqual(s["database_mongo"], "MongoDB Atlas")
        self.assertIn("Function URL", s["ingress"])
        self.assertIn("Parameter Store", s["secrets"])
        self.assertIn("EventBridge Scheduler", s["scheduler"])
        for key in ("cache", "egress", "network"):
            self.assertNotIn(key, s)

    def test_no_billable_resource_without_a_free_allowance(self):
        self.assertEqual(sorted(set(self.manifest["resource_types"]) & set(FORBIDDEN)), [])
        self.assertTrue(self.manifest["streaming_compatible"])
        self.assertTrue(self.manifest["has_scheduler"])

    def test_dockerfile_uses_the_detected_entrypoint_and_the_adapter(self):
        found = sorted((self.repo / ".solsarch" / "iac" / "docker").glob("dbai/Dockerfile*"))
        self.assertTrue(found)
        text = found[0].read_text(encoding="utf-8")
        self.assertIn("src.api:app", text)
        self.assertIn("aws-lambda-adapter", text)
        self.assertIn("AWS_LWA_INVOKE_MODE=response_stream", text)

    def test_every_required_variable_is_declared_for_injection(self):
        names = set(self.manifest["injected_env_vars"])
        self.assertTrue({"GROQ_API_KEY", "REDIS_URL", "CHAT_MONGODB_URL"} <= names, names)


class FreeTierRule(unittest.TestCase):
    def test_paid_services_on_an_always_free_account_are_a_violation(self):
        import architect
        plan = {"cloud": "aws", "decisions": [{"key": "egress", "service": "NAT Gateway", "provider": "aws"}]}
        rule = architect.free_tier_rule(plan, {"account_type": "free_tier"})
        self.assertEqual(rule["status"], "violated")
        self.assertEqual(architect.free_tier_rule(plan, {"account_type": "standard"})["status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
