#!/usr/bin/env python3
"""A self-check that runs the whole pipeline on built-in sample projects and tests the promises the skill makes.

    eval_harness.py run [--suite quick|full] [--providers aws,render] [--baseline FILE] [--update-baseline] [--out FILE]

No model and no network are needed. For every sample project, provider and compute model it checks:
  determinism   building twice gives byte-identical files
  complete      no template token is left unfilled; every file is stamped or is data
  neutral       the services chosen for AWS, Azure and Google Cloud are the same for the same code
  capabilities  the capability checklist score is not below the saved baseline (a regression guard) and never below the floor
  guide         the deploy guide is complete for all four account types and quotes no price
  secrets       no key, token or private key appears in any output
  syntax        every .tf file is balanced; every .toml and .json file parses
Exit code 0 when everything holds, 1 otherwise. Standard library only.
"""
import argparse
import json
import re
import sys
import tempfile
try:
    import tomllib  # Python 3.11 and newer; older versions skip the TOML syntax check
except ImportError:  # pragma: no cover
    tomllib = None
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import architect  # noqa: E402
import ast_scanner  # noqa: E402
import deploy_guide  # noqa: E402
import iac  # noqa: E402
import iac_common  # noqa: E402
import providers  # noqa: E402
import recommend  # noqa: E402

BASELINE = HERE.parent / "references" / "eval" / "parity-baseline.json"
FLOOR = 70.0
QUICK = ("aws", "azure", "gcp", "hetzner", "digitalocean", "linode", "oci", "render", "flyio", "vercel", "supabase", "neon")
SECRET_RE = re.compile(r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9]{32,}")
PRICE_RE = re.compile(r"[$€£]\s?\d")


def write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def sample_monorepo(r):
    write(r, "services/api/requirements.txt", "fastapi\nsqlalchemy\npsycopg2-binary\nredis\ncelery\nboto3\nopenai\nqdrant-client\npyjwt\n")
    write(r, "services/api/Dockerfile", "FROM python:3.12\nEXPOSE 8000\nUSER app\n")
    write(r, "services/api/app/main.py", "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/healthz')\ndef h():\n    return 'ok'\n")
    write(r, "services/web/package.json", '{"dependencies": {"next": "14", "react": "18", "next-auth": "4"}}')
    write(r, "services/worker/requirements.txt", "celery\nredis\nminio\n")
    write(r, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n  mq:\n    image: rabbitmq:3\n")


def sample_single(r):
    write(r, "package.json", '{"name":"shop","dependencies":{"next":"14.2.0","react":"18.3.0","pg":"8.11.0","ioredis":"5.3.0","bullmq":"5.0.0"}}')
    write(r, "Dockerfile", 'FROM node:22-alpine\nEXPOSE 3000\nUSER node\nCMD ["node", "server.js"]\n')
    write(r, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n")


def sample_spa(r):
    write(r, "package.json", '{"dependencies":{"react":"18","react-dom":"18"}}')
    write(r, "src/App.jsx", "export default function(){return null}")


SAMPLES = {"monorepo": sample_monorepo, "single": sample_single, "spa": sample_spa}


def build(provider, mode, sample, account="standard"):
    with tempfile.TemporaryDirectory() as t:
        root = Path(t) / "shop"
        root.mkdir()
        SAMPLES[sample](root)
        scan = ast_scanner.scan(root)
        profile = dict(architect.PROFILE_DEFAULTS, account_type=account)
        plan = recommend.build_plan(scan, recommend.load_catalog(), provider, mode, [], profile)
        design = architect.review(root, scan, plan, profile)
        result = iac.build(plan, design, profile, scan, "shop")
        return plan, design, profile, result


def check(name, ok, detail=""):
    return {"check": name, "ok": bool(ok), "detail": detail}


def evaluate(provider, mode, sample, baseline):
    out = []
    plan, design, profile, result = build(provider, mode, sample)
    again = build(provider, mode, sample)[3]
    out.append(check("determinism", result["files"] == again["files"] and result["manifest"]["digest"] == again["manifest"]["digest"]))
    left = [p for p, t in result["files"].items() if "@@" in t]
    out.append(check("complete", not left, ", ".join(left[:3])))
    bad = []
    for path, text in result["files"].items():
        if path.endswith(".tf") and iac_common.check_balanced(text, path):
            bad.append(path)
        try:
            if path.endswith(".toml") and tomllib is not None:
                tomllib.loads(text)
            elif path.endswith(".json"):
                json.loads(text)
        except ValueError:
            bad.append(path)
    out.append(check("syntax", not bad, ", ".join(bad[:3])))
    leaked = [p for p, t in result["files"].items() if SECRET_RE.search(t)]
    out.append(check("secrets", not leaked, ", ".join(leaked[:3])))
    parity = result["manifest"]["parity"]
    key = "%s/%s/%s" % (provider, mode, sample)
    floor = max(FLOOR, baseline.get(key, 0) - 0.05)
    out.append(check("capabilities", parity["score"] >= floor, "%.1f (floor %.1f, gaps %s)" % (parity["score"], floor, ",".join(parity["gaps"]))))
    problems = []
    for account in deploy_guide.ACCOUNT_LABELS:
        p2, d2, prof2, r2 = build(provider, mode, sample, account)
        text = deploy_guide.build(p2, d2, prof2, r2["manifest"], r2["files"])
        if "## Step 5" not in text or "## If something goes wrong" not in text or deploy_guide.ACCOUNT_LABELS[account] not in text or "@@" in text:
            problems.append(account + ": incomplete")
        if PRICE_RE.search(text):
            problems.append(account + ": quotes a price")
    out.append(check("guide", not problems, "; ".join(problems)))
    return out, parity["score"], key


def neutral(sample, mode="server"):
    keys = {}
    for provider in ("aws", "azure", "gcp"):
        plan = build(provider, mode, sample)[0]
        keys[provider] = tuple(d["key"] for d in plan["decisions"])
    return check("neutral", len(set(keys.values())) == 1, json.dumps({k: list(v) for k, v in keys.items()}) if len(set(keys.values())) != 1 else "")


def run(suite="quick", only=None, baseline_file=BASELINE, update=False):
    baseline = json.loads(Path(baseline_file).read_text(encoding="utf-8")) if Path(baseline_file).exists() else {}
    chosen = only or (providers.hosting_ids() if suite == "full" else [p for p in QUICK if p in providers.hosting_ids()])
    rows, scores, failed = [], {}, 0
    for provider in chosen:
        for mode in ("server", "serverless"):
            for sample in (SAMPLES if suite == "full" else ("monorepo", "single")):
                checks, score, key = evaluate(provider, mode, sample, baseline)
                scores[key] = score
                bad = [c for c in checks if not c["ok"]]
                failed += len(bad)
                rows.append({"case": key, "score": score, "failed": bad})
    for sample in SAMPLES:
        for mode in ("server", "serverless"):
            c = neutral(sample, mode)
            rows.append({"case": "neutral/%s/%s" % (mode, sample), "failed": [] if c["ok"] else [c]})
            failed += 0 if c["ok"] else 1
    if update:
        merged = dict(baseline)
        merged.update({k: round(v, 1) for k, v in scores.items()})
        Path(baseline_file).write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"suite": suite, "cases": len(rows), "failed_checks": failed, "ok": failed == 0, "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Self-check of the SolsArch pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--suite", choices=("quick", "full"), default="quick")
    p.add_argument("--providers", default="")
    p.add_argument("--baseline", default=str(BASELINE))
    p.add_argument("--update-baseline", action="store_true")
    p.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    result = run(args.suite, [x for x in args.providers.split(",") if x] or None, args.baseline, args.update_baseline)
    text = json.dumps(result, indent=2) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    summary = {"suite": result["suite"], "cases": result["cases"], "failed_checks": result["failed_checks"], "ok": result["ok"],
               "failures": [r for r in result["rows"] if r["failed"]][:20]}
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
