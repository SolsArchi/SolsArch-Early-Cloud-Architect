#!/usr/bin/env python3
"""Seven validation gates and the 100-point architecture quality score.

    qa_gates.py --repo REPO

Every check returns one of PASS, FAIL, WARNING, NOT_VERIFIED (or N/A when it does not apply). A check that could not be run
is NOT_VERIFIED and earns nothing: a missing result never turns into a pass. The checks read only files the other steps wrote,
use no network and no clock, so the same inputs give the same score.

Gates: 1 repository evidence, 2 architecture compatibility, 3 constraint enforcement, 4 infrastructure validation,
5 cost reconciliation, 6 security and reliability, 7 cross-artifact consistency.

Score areas (100 points): repository understanding 15, architecture compatibility 20, infrastructure correctness 20,
cost optimisation 15, security and compliance 15, reliability and scalability 10, evidence and consistency 5.
A score above 85 is only issued when every acceptance criterion holds; otherwise it is capped at 84 and the cap is stated.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract as contract_mod  # noqa: E402
import capabilities  # noqa: E402
import static_check  # noqa: E402
import rules as company_rules  # noqa: E402

PASS, FAIL, WARNING, NOT_VERIFIED, NA = "PASS", "FAIL", "WARNING", "NOT_VERIFIED", "N/A"
AREAS = [("repository", "Repository understanding", 15), ("architecture", "Architecture compatibility", 20),
         ("infrastructure", "Infrastructure correctness", 20), ("cost", "Cost optimisation", 15),
         ("security", "Security and compliance", 15), ("reliability", "Reliability and scalability", 10),
         ("evidence", "Evidence and consistency", 5)]
GATES = [(1, "Repository evidence"), (2, "Architecture compatibility"), (3, "Constraint enforcement"), (4, "Infrastructure validation"),
         (5, "Cost reconciliation"), (6, "Security and reliability"), (7, "Cross-artifact consistency"),
         (8, "Company rules")]
CREDIT = {PASS: 1.0, WARNING: 0.5, FAIL: 0.0, NOT_VERIFIED: 0.0}
BANDS = [(90, "Excellent", "Production candidate, subject to validation"), (85, "Strong", "Minor improvements required"),
         (70, "Conditional", "Resolve important findings"), (50, "Weak", "Architecture revision required"),
         (0, "Critical", "Regenerate architecture")]
CAP_SCORE = 84
NOT_AN_APP_HOST = ("togetherai", "snowflake", "mongodbatlas", "neon")
# Resource types that bill every hour or month whether or not anyone uses the app. None may appear on an always-free plan.
ALWAYS_BILLING = re.compile(r"aws_nat_gateway|aws_docdb|aws_elasticache|aws_lb$|aws_alb$|aws_db_instance|aws_rds_cluster|aws_mq_broker|aws_msk|"
                            r"aws_kms_key$|aws_secretsmanager_secret$|aws_wafv2_web_acl$|aws_api_gateway_rest_api|aws_apigatewayv2_api|"
                            r"azurerm_nat_gateway|azurerm_application_gateway|azurerm_redis|azurerm_cosmosdb_account|azurerm_firewall|"
                            r"google_compute_router_nat|google_redis|google_compute_forwarding_rule|google_sql_database_instance")
SECRET_WORDS = ("secret", "key", "token", "password", "credential")


def today():
    """The date used to judge how old a fact is. SOLSARCH_TODAY fixes it so a recorded snapshot can be tested later."""
    import os
    from datetime import datetime, timezone
    return os.environ.get("SOLSARCH_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


STALE_DAYS = 30


def age_days(retrieved):
    from datetime import date
    try:
        return (date.fromisoformat(today()) - date.fromisoformat(retrieved)).days
    except (TypeError, ValueError):
        return None


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def chk(cid, area, gate, name, status, detail="", evidence=(), blocker=False, criterion=None):
    return {"id": cid, "area": area, "gate": gate, "name": name, "status": status, "detail": detail,
            "evidence": list(evidence)[:8], "blocker": bool(blocker and status == FAIL), "criterion": criterion}


class Inputs:
    """Everything the checks read, loaded once."""

    def __init__(self, repo):
        self.repo = Path(repo)
        s = self.repo / ".solsarch"
        self.scan, self.plan, self.design = read(s / "scan.json"), read(s / "plan.json"), read(s / "design.json")
        self.manifest = read(s / "iac-manifest.json")
        self.costs, self.validation = read(s / "costs.json"), read(s / "validation.json")
        self.scorecard, self.facts = read(s / "ops-scorecard.json"), read(s / "facts.json")
        self.static = read(s / "static-check.json")
        self.caps = capabilities.load(repo)
        self.contract = read(s / "contract.json") or (contract_mod.build(repo) if self.plan else None)
        self.iac = {}
        root = s / "iac"
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.is_file() and ".terraform" not in p.parts and p.suffix not in (".png", ".svg", ".zip", ".pptx", ".pdf", ".ico") and p.stat().st_size < 400000:
                    try:
                        self.iac[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        pass
        docs = self.repo / "docs"
        self.docs = {}
        if docs.is_dir():
            for p in sorted(docs.glob("solsarch-*.md")):
                self.docs[p.name] = p.read_text(encoding="utf-8", errors="replace")
        self.units = [u for u in (self.design or {}).get("units", []) if u.get("kind") != "static"]
        self.rules = {r["id"]: r for r in (self.design or {}).get("rules", [])}
        cloud = (self.plan or {}).get("cloud")
        self.keys = {d["key"] for d in (self.plan or {}).get("decisions", [])}
        self.native = {d["key"]: d for d in (self.plan or {}).get("decisions", []) if d.get("provider", cloud) == cloud}
        self.profile = (self.plan or {}).get("profile", {})
        self.signals = (self.scan or {}).get("signals", {})
        self.company = company_rules.load(repo)
        self.parity = {c["id"]: c["status"] for c in ((self.manifest or {}).get("parity", {}).get("capabilities", []))}

    @property
    def free(self):
        return self.profile.get("account_type") == "free_tier"

    @property
    def serverless(self):
        return self.plan["compute_model"] == "serverless"


# ---------------------------------------------------------------- gate 1: repository evidence
def gate1(i):
    out = []
    guessed = [u["name"] for u in i.units if u["kind"] in ("api", "web", "service", "worker") and not (u.get("entrypoint") or {}).get("verified")
               and u.get("languages") and u["languages"][0] == "python"]
    if not i.units:
        out.append(chk("R1", "repository", 1, "Start command and port found in the code", NA, "No application unit to start."))
    else:
        out.append(chk("R1", "repository", 1, "Start command and port found in the code", WARNING if guessed else PASS,
                       ("Start command guessed for %s; confirm it." % ", ".join(guessed)) if guessed else
                       "Every unit has a start command found in the code or a standard one for its framework.",
                       ["%s:%s" % (u["entrypoint"]["file"], u["entrypoint"]["line"]) for u in i.units if (u.get("entrypoint") or {}).get("verified")]))
    envs = (i.scan or {}).get("env_vars")
    out.append(chk("R2", "repository", 1, "Settings and secrets the code reads are listed", NOT_VERIFIED if envs is None else (PASS if envs else NA),
                   "%d variable(s) found with file and line." % len(envs or []) if envs else "No environment variable reads were found.",
                   ["%s (%s)" % (v["name"], v["files"][0] if v.get("files") else "?") for v in (envs or [])[:6]]))
    wl = [k for k in ("streaming", "scheduler") if i.signals.get(k, "none") != "none"]
    out.append(chk("R3", "repository", 1, "Workload types are detected (requests, streaming, scheduled work, workers)",
                   PASS if "signals" in (i.scan or {}) else NOT_VERIFIED,
                   "Detected: %s." % (", ".join(wl) or "plain request handling"), [], criterion=None))
    bare = [d["short"] for d in i.plan["decisions"] if not d.get("evidence")]
    out.append(chk("R4", "repository", 1, "Every decision traces to evidence or a stated assumption", WARNING if bare else PASS,
                   ("No evidence recorded for: %s." % ", ".join(bare)) if bare else "All %d decisions cite a file, a rule or a stated requirement." % len(i.plan["decisions"]),
                   criterion="traceable"))
    return out


# ---------------------------------------------------------------- gate 2: architecture compatibility
def gate2(i):
    out = []
    ingress = i.native.get("ingress", {}).get("service", "")
    if i.signals.get("streaming", "none") == "used" and i.serverless and "ingress" in i.native:
        bad = "API Gateway" in ingress or "Application Gateway" in ingress
        out.append(chk("A1", "architecture", 2, "The entry point does not cut or buffer streaming responses", FAIL if bad else PASS,
                       "%s buffers streamed answers and ends requests after about 30 seconds." % ingress if bad else
                       "%s carries streaming answers." % ingress, [], blocker=bad, criterion="compatible"))
    else:
        out.append(chk("A1", "architecture", 2, "The entry point does not cut or buffer streaming responses", NA, "No streaming code, or not a function platform."))
    if i.signals.get("scheduler", "none") != "none" and i.serverless and i.plan["cloud"] not in NOT_AN_APP_HOST:
        has = "scheduler" in i.native
        gap = next((g for g in i.plan.get("gaps", []) if g["key"] == "scheduler"), None)
        if has:
            out.append(chk("A2", "architecture", 2, "In-process scheduled work has a cloud schedule", PASS,
                           "A managed schedule (%s) calls the job." % i.native["scheduler"]["service"], [], criterion="compatible"))
        elif gap:
            out.append(chk("A2", "architecture", 2, "In-process scheduled work has a cloud schedule", WARNING,
                           "No managed schedule exists in the catalogue for this provider, and the plan says so: %s" % gap["message"], criterion="compatible"))
        else:
            out.append(chk("A2", "architecture", 2, "In-process scheduled work has a cloud schedule", FAIL,
                           "The code starts a scheduler inside the app, which stops when the function is frozen, and the plan neither adds a schedule nor mentions it.",
                           [], blocker=True, criterion="compatible"))
    else:
        out.append(chk("A2", "architecture", 2, "In-process scheduled work has a cloud schedule", NA, "No in-process scheduler, or always-on compute."))
    r9 = i.rules.get("DR-09")
    out.append(chk("A3", "architecture", 2, "The data service matches the database driver", NA if not r9 else (FAIL if r9["status"] == "violated" else PASS),
                   r9["detail"] if r9 else "", criterion="compatible"))
    violated = [r for r in i.rules.values() if r["status"] == "violated" and r["id"] != "DR-13"]
    out.append(chk("A4", "architecture", 2, "No design rule is violated", FAIL if violated else PASS,
                   "; ".join("%s: %s" % (r["id"], r["detail"]) for r in violated) or "%d rules met or bent with a stated risk." % sum(
                       1 for r in i.rules.values() if r["status"] in ("met", "bent")), criterion="compatible"))
    heavy = [c["name"] for c in (i.scan or {}).get("components", []) if c["name"] in ("Whisper", "PyTorch", "Transformers")]
    out.append(chk("A5", "architecture", 2, "Heavy local models fit the chosen compute", WARNING if heavy and i.serverless else (PASS if heavy else NA),
                   ("%s runs a model in-process; check memory and image size on functions." % ", ".join(heavy)) if heavy and i.serverless else
                   ("Always-on compute suits %s." % ", ".join(heavy) if heavy else "No in-process model.")))
    return out


# ---------------------------------------------------------------- gate 3: constraint enforcement
def gate3(i):
    out = []
    m = i.manifest
    ok = bool(m) and m.get("provider") == i.plan["cloud"] and m.get("compute_model") == i.plan["compute_model"]
    out.append(chk("K1", "architecture", 3, "Generated code is for the chosen cloud and compute model", NOT_VERIFIED if not m else (PASS if ok else FAIL),
                   "Cloud %s, model %s." % (i.plan["cloud"], i.plan["compute_model"]) if ok else "No infrastructure code to compare." if not m else
                   "The code was generated for %s/%s but the plan says %s/%s." % (m.get("provider"), m.get("compute_model"), i.plan["cloud"], i.plan["compute_model"])))
    if i.free:
        bad = sorted({r["type"] for r in (i.contract or {}).get("architecture", {}).get("resources", []) if ALWAYS_BILLING.search(r["type"])})
        out.append(chk("K2", "architecture", 3, "An always-free account gets no service that bills by the hour or month", NOT_VERIFIED if not m else (FAIL if bad else PASS),
                       ("Generated: %s." % ", ".join(bad)) if bad else "No hourly or monthly billed resource in the generated code.", bad, blocker=True, criterion="constraints"))
    else:
        out.append(chk("K2", "architecture", 3, "An always-free account gets no service that bills by the hour or month", NA, "The account is not always-free."))
    budget = (i.design or {}).get("profile", {}).get("monthly_budget")
    if budget and i.costs and i.costs.get("lines"):
        total = i.costs["totals"]["monthly"]
        out.append(chk("K3", "cost", 3, "Monthly estimate is within the budget", PASS if total <= float(budget) * 1.1 else FAIL,
                       "Estimate %.2f against budget %s (10%% allowed)." % (total, budget), criterion="constraints"))
    elif budget:
        out.append(chk("K3", "cost", 3, "Monthly estimate is within the budget", NOT_VERIFIED, "A budget is set but no priced estimate exists."))
    else:
        out.append(chk("K3", "cost", 3, "Monthly estimate is within the budget", NA, "No monthly budget was given."))
    region = i.profile.get("region")
    if region:
        text = "\n".join(t for p, t in i.iac.items() if p.endswith((".tf", ".tfvars.example", ".yml")))
        out.append(chk("K4", "architecture", 3, "Chosen region is used", PASS if region in text else FAIL,
                       "Region %s appears in the generated code." % region if region in text else "Region %s is not used by the generated code." % region,
                       criterion="constraints"))
    else:
        out.append(chk("K4", "architecture", 3, "Chosen region is used", NA, "No region was required."))
    comp = i.profile.get("compliance") or []
    out.append(chk("K5", "security", 3, "Compliance requirements are addressed", NOT_VERIFIED if comp else NA,
                   ("Requirements listed (%s) need a human review; the tool checks encryption, logging and data placement only." % ", ".join(comp)) if comp
                   else "No compliance requirement was given."))
    return out


# ---------------------------------------------------------------- gate 4: infrastructure validation
def gate4(i):
    out = []
    v = i.validation
    stat = i.static or (static_check.run(i.repo) if i.iac else None)
    if v and v.get("failed"):
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", FAIL, "Failed: %s." % ", ".join(v["failed"][:5]), v["failed"], criterion="validated"))
    elif v and v.get("verdict", "").startswith("passed"):
        note = "" if not v.get("skipped") else " Not run: %s." % "; ".join(v["skipped"][:3])
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", PASS, "%s passed.%s" % (v.get("engine", "validators"), note), criterion="validated"))
    elif v:
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", WARNING, "Verdict %s." % v.get("verdict"), criterion="validated"))
    elif stat and stat.get("findings"):
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", FAIL,
                       "No real validator was run, and the built-in check found %d problem(s): %s" % (
                           len(stat["findings"]), "; ".join("%s: %s" % (f["file"], f["detail"]) for f in stat["findings"][:3])),
                       ["%s %s" % (f["file"], f["detail"]) for f in stat["findings"]], criterion="validated"))
    elif stat and stat.get("verdict") == "clean":
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", WARNING,
                       "terraform, hadolint and the other tools have not been run (run validate). The built-in check of %d files found nothing, "
                       "which is weaker evidence, so this earns half credit and the score cannot exceed %d." % (stat["files"], CAP_SCORE), criterion="validated"))
    else:
        out.append(chk("I1", "infrastructure", 4, "Generated code passes real validators", NOT_VERIFIED, "Nothing was checked: no infrastructure code or validator result.", criterion="validated"))
    cap = (i.manifest or {}).get("parity")
    if cap:
        s = cap["score"]
        out.append(chk("I2", "infrastructure", 4, "Capability checklist (network, secrets, logs, identity, state...)", PASS if s >= 85 else (WARNING if s >= 60 else FAIL),
                       "%.1f%% met; gaps: %s." % (s, ", ".join(cap["gaps"]) or "none")))
    else:
        out.append(chk("I2", "infrastructure", 4, "Capability checklist (network, secrets, logs, identity, state...)", NOT_VERIFIED, "No infrastructure code was generated."))
    if i.plan["cloud"] in NOT_AN_APP_HOST:
        out.append(chk("I3", "infrastructure", 4, "Every setting the code reads is carried into the deployment", NA,
                       "This provider hosts models or data, not the application, so the application's settings are set where the application runs."))
    elif not i.iac:
        out.append(chk("I3", "infrastructure", 4, "Every setting the code reads is carried into the deployment", NOT_VERIFIED, "No generated files to search."))
    else:
        text = "\n".join(i.iac.values())
        allv = (i.scan or {}).get("env_vars", [])
        need = [v["name"] for v in allv if v["required"]]
        missing = [n for n in need if n not in text]
        optional_missing = [v["name"] for v in allv if not v["required"] and v["name"] not in text]
        secret_missing = [n for n in missing if any(w in n.lower() for w in SECRET_WORDS)]
        if not allv:
            st, detail = NA, "No environment variable reads were found in the code."
        elif missing:
            st, detail = FAIL, "Required variable(s) not carried into the deployment: %s." % ", ".join(missing)
        elif optional_missing:
            st, detail = WARNING, "Optional variable(s) not carried into the deployment: %s." % ", ".join(optional_missing)
        else:
            st, detail = PASS, "All %d variable(s) the code reads appear in the generated configuration." % len(allv)
        out.append(chk("I3", "infrastructure", 4, "Every setting the code reads is carried into the deployment", st, detail, missing + optional_missing,
                       blocker=bool(secret_missing)))
    files = {p: t for p, t in i.iac.items() if re.search(r"(^|/)Dockerfile[^/]*$", p)}
    if not files:
        out.append(chk("I4", "infrastructure", 4, "Images the tool writes run as a non-root user and have a start command", NA, "No Dockerfile was generated."))
    else:
        weak = [p for p, t in files.items() if not re.search(r"^USER\s+(?!root\b)\S+", t, re.M) or not re.search(r"^(CMD|ENTRYPOINT)\b", t, re.M)]
        mine = [u["name"] for u in i.units if u.get("dockerfile") and u.get("runs_as_root")]
        out.append(chk("I4", "infrastructure", 4, "Images the tool writes run as a non-root user and have a start command", FAIL if weak else PASS,
                       ("Missing a non-root USER or a start command: %s." % ", ".join(weak)) if weak else
                       "%d generated Dockerfile(s) use a non-root user and a start command." % len(files)
                       + (" Your own Dockerfile for %s runs as root; the fix notes show the change." % ", ".join(mine) if mine else ""), weak, blocker=bool(weak)))
    return out


# ---------------------------------------------------------------- gate 5: cost reconciliation
def gate5(i):
    out = []
    c = i.costs
    if not c or "lines" not in c:
        return [chk("C1", "cost", 5, "Every billable generated resource has a cost entry", NOT_VERIFIED, "No cost report.", criterion="reconciled"),
                chk("C2", "cost", 5, "Prices are complete", NOT_VERIFIED, "No cost report."),
                chk("C3", "cost", 5, "Prices come from live vendor sources", NOT_VERIFIED, "No cost report."),
                chk("C4", "cost", 5, "Bill is shown at several request volumes", NOT_VERIFIED, "No cost report.")]
    missing = contract_mod.unpriced_decisions(i.contract) if i.contract else []
    out.append(chk("C1", "cost", 5, "Every billable generated resource has a cost entry", FAIL if missing else PASS,
                   ("No price, free note or unpriced entry for: %s." % ", ".join(missing)) if missing else "Every billable generated resource maps to a priced line, a free note or a named unpriced item.",
                   criterion="reconciled", blocker=False))
    n_un = len(c.get("unpriced", []))
    out.append(chk("C2", "cost", 5, "Prices are complete", PASS if not n_un else (WARNING if c["lines"] else NOT_VERIFIED),
                   "All %d lines are priced." % len(c["lines"]) if not n_un else "%d item(s) have no public price yet: %s." % (n_un, ", ".join(u["service"] for u in c["unpriced"][:4]))))
    live = c.get("totals", {}).get("live_share", 0)
    stale = sorted({"%s (%s days old)" % (l["label"], age_days(l["retrieved"])) for l in c["lines"]
                    if (age_days(l.get("retrieved")) is None or age_days(l["retrieved"]) > STALE_DAYS)})
    if not c["lines"]:
        st, text = NOT_VERIFIED, "Nothing was priced from a live source."
    elif live < 0.8:
        st, text = WARNING, "Only %.0f%% of the estimate uses prices read live from the vendor." % (live * 100)
    elif stale:
        st, text = WARNING, "Live prices older than %d days: %s. Run costs with --refresh." % (STALE_DAYS, "; ".join(stale[:4]))
    else:
        st, text = PASS, "%.0f%% of the estimate uses live vendor prices, all read in the last %d days (oldest %s)." % (
            live * 100, STALE_DAYS, min(l["retrieved"] for l in c["lines"]))
    out.append(chk("C3", "cost", 5, "Prices come from live vendor sources", st, text))
    out.append(chk("C4", "cost", 5, "Bill is shown at several request volumes", PASS if c.get("scenarios") else NOT_VERIFIED,
                   "Scenario table present." if c.get("scenarios") else "No priced lines to scale."))
    return out


# ---------------------------------------------------------------- gate 6: security and reliability
def gate6(i):
    out = []
    bad = [f for f in (i.scan or {}).get("findings", []) if f.get("severity") in ("critical", "high")]
    exposed = [f for f in bad if f.get("id") in ("SEC-002",)]
    out.append(chk("S1", "security", 6, "No critical or high pre-deployment security finding", FAIL if bad else PASS,
                   "%d blocking finding(s)." % len(bad) if bad else "No blocking finding in %d checked." % len((i.scan or {}).get("findings", [])),
                   ["%s %s" % (f["id"], f["title"]) for f in bad], blocker=bool(exposed), criterion="security"))
    wild = [p for p, t in i.iac.items() if p.endswith(".tf") and re.search(r'actions\s*=\s*\[\s*"\*"\s*\]|"Action"\s*:\s*"\*"|Action\s*=\s*"\*"', t)]
    out.append(chk("S2", "security", 6, "No wildcard permission in generated policies", NOT_VERIFIED if not i.iac else (FAIL if wild else PASS),
                   "Wildcard action in %s." % ", ".join(wild) if wild else "No policy grants every action.", wild))
    for cid, ids, name in (("S3", ("CAP-08", "CAP-10"), "Secrets are in a secret store and stored data is encrypted"),):
        if not i.manifest:
            out.append(chk(cid, "security", 6, name, NOT_VERIFIED, "No infrastructure code."))
        else:
            gap = [x for x in ids if i.parity.get(x) == "gap"]
            out.append(chk(cid, "security", 6, name, WARNING if gap else PASS, ("Capability gap: %s." % ", ".join(gap)) if gap else "Secrets store and encryption in place or not needed."))
    logs = [(p, t) for p, t in i.iac.items() if p.endswith(".tf")]
    groups = sum(len(re.findall(r'resource "aws_cloudwatch_log_group"', t)) for p, t in logs)
    with_ret = sum(len(re.findall(r"retention_in_days\s*=", t)) for p, t in logs)
    repos = sum(len(re.findall(r'resource "aws_ecr_repository"', t)) for p, t in logs)
    rules = sum(len(re.findall(r'resource "aws_ecr_lifecycle_policy"', t)) for p, t in logs)
    if groups or repos:
        ok = with_ret >= groups and rules >= repos
        out.append(chk("S4", "security", 6, "Logs expire and image repositories clean up after themselves", PASS if ok else FAIL,
                       "%d of %d log groups set a retention period; %d of %d image repositories have a lifecycle policy." % (
                           min(with_ret, groups), groups, min(rules, repos), repos)))
    else:
        out.append(chk("S4", "security", 6, "Logs expire and image repositories clean up after themselves", NA, "No log group resource in the generated code."))
    ci = [t for p, t in i.iac.items() if p.startswith("ci/") or ".github/workflows" in p]
    if ci:
        oidc = any("id-token: write" in t for t in ci)
        out.append(chk("S5", "security", 6, "Pipelines sign in without stored cloud keys", PASS if oidc else WARNING,
                       "The deploy pipeline uses short-lived federated credentials." if oidc else "The pipeline relies on stored cloud credentials; use federated sign-in."))
    else:
        out.append(chk("S5", "security", 6, "Pipelines sign in without stored cloud keys", NA, "No pipeline was generated."))
    for cid, ids, name in (("H1", ("CAP-05",), "Health checks use the path the code serves"), ("H2", ("CAP-12",), "Logs and monitoring"),
                           ("H3", ("CAP-07", "CAP-16"), "Backups and protection for stored data")):
        if not i.manifest:
            out.append(chk(cid, "reliability", 6, name, NOT_VERIFIED, "No infrastructure code."))
            continue
        gap = [x for x in ids if i.parity.get(x) == "gap"]
        na = all(i.parity.get(x) in (None, "not_applicable") for x in ids)
        out.append(chk(cid, "reliability", 6, name, NA if na else (WARNING if gap else PASS), ("Gap: %s." % ", ".join(gap)) if gap else "Met or handled by the platform."))
    cap = (i.design or {}).get("capacity", {})
    out.append(chk("H4", "reliability", 6, "Capacity numbers come from the user, not an assumption", PASS if cap.get("source") == "user" else WARNING,
                   "Traffic was supplied." if cap.get("source") == "user" else "Traffic is an assumption (%s confidence); give daily requests in the profile for a firmer estimate." % cap.get("confidence")))
    stream_units = [u for u in i.units if u.get("streaming")]
    if stream_units and i.manifest and i.plan["cloud"] == "aws" and i.serverless:
        tm = {m.group(1): int(m.group(2)) for p, t in i.iac.items() if p.endswith("variables.tf")
              for m in re.finditer(r'variable "(\w+)_timeout_seconds" \{[^}]*?default\s*=\s*(\d+)', t, re.S)}
        short = [n for n, v in tm.items() if v < 120 and any(n == re.sub(r"\W", "_", u["name"]) for u in stream_units)]
        out.append(chk("H5", "reliability", 6, "Streaming or long-running code has a timeout of at least 120 seconds", FAIL if short else PASS,
                       ("Timeout under 120 s for %s." % ", ".join(short)) if short else "Timeouts: %s." % ", ".join("%s=%ds" % kv for kv in sorted(tm.items())), blocker=bool(short)))
    return out


# ---------------------------------------------------------------- gate 7: consistency
def contradictions(i):
    found = []
    c = i.contract
    if not c or not i.manifest:
        return found
    for r in contract_mod.untraced(c):
        found.append("The generated code creates %s (%s) but the plan has no matching decision." % (r["type"], r["file"]))
    for k in contract_mod.unpriced_decisions(c):
        found.append("The generated code creates billable resources for %s but the cost report has no entry for it." % k)
    if i.plan["cloud"] == "aws" and i.serverless and "aws_lambda_function" in set(i.manifest.get("resource_types", [])):
        if i.signals.get("streaming") == "used" and not i.manifest.get("streaming_compatible"):
            found.append("The code streams responses but the generated entry point cannot stream.")
        if i.signals.get("scheduler") == "used" and not i.manifest.get("has_scheduler"):
            found.append("The code runs an in-process scheduler but no managed schedule was generated.")
    if i.costs:
        for l in i.costs.get("lines", []):
            if l["decision"] in ("egress", "cache", "database_mongo") and l["decision"] not in i.native and l["provider"] == i.plan["cloud"]:
                found.append("The cost report prices %s but the plan does not contain it." % l["decision"])
    return found


def gate7(i):
    out = []
    if not (i.manifest and i.contract):
        out.append(chk("X1", "infrastructure", 7, "Plan, generated code and cost report agree", NOT_VERIFIED, "No generated code to compare.", criterion="consistent"))
    else:
        found = contradictions(i)
        out.append(chk("X1", "infrastructure", 7, "Plan, generated code and cost report agree", FAIL if found else PASS,
                       "%d contradiction(s)." % len(found) if found else "Every generated resource traces to a decision and every billable decision has a cost entry.",
                       found, criterion="consistent"))
    fp = i.plan["fingerprint"]
    stamps = {"infrastructure code": (i.manifest or {}).get("plan_fingerprint"), "contract": (i.contract or {}).get("plan_fingerprint")}
    stale = [k for k, v in stamps.items() if v and v != fp]
    out.append(chk("X2", "evidence", 7, "All artifacts were built from the same plan", FAIL if stale else (PASS if any(stamps.values()) else NOT_VERIFIED),
                   ("Out of date: %s. Re-run the steps." % ", ".join(stale)) if stale else "Plan %s is stamped on every artifact." % fp, criterion="consistent"))
    if i.docs:
        text = "\n".join(i.docs.values()).lower()
        absent = [d["short"] for d in i.plan["decisions"] if d["short"].lower() not in text]
        out.append(chk("X3", "evidence", 7, "Reports name the same services as the plan", WARNING if absent else PASS,
                       ("Not named in the reports: %s." % ", ".join(absent)) if absent else "Every planned service is named in the reports."))
    else:
        out.append(chk("X3", "evidence", 7, "Reports name the same services as the plan", NOT_VERIFIED, "No report files found."))
    rel = capabilities.relevant(i.caps, i.plan, i.signals)
    if rel:
        by_state = {}
        for rid, rec in sorted(rel.items()):
            by_state.setdefault(capabilities.status(rec), []).append(rid)
        bad = {k: v for k, v in by_state.items() if k != "verified"}
        if not bad:
            ages = [capabilities.age_days(r) for r in rel.values()]
            out.append(chk("X5", "evidence", 7, "Limits and free allowances the plan relies on are sourced and fresh", PASS,
                           "%d fact(s) each tied to an official page and sentence, all read within %d days (oldest %d days)." % (len(rel), capabilities.STALE_DAYS, max(a for a in ages if a is not None))))
        else:
            hard = "unsourced" in bad or "changed" in bad
            out.append(chk("X5", "evidence", 7, "Limits and free allowances the plan relies on are sourced and fresh", NOT_VERIFIED if hard else WARNING,
                           "; ".join("%s: %s" % (k, ", ".join(v)) for k, v in sorted(bad.items())) +
                           ". Run capabilities.py refresh, or record the sentence you read with capabilities.py add."))
    f = i.facts
    if f is None:
        out.append(chk("X4", "evidence", 7, "Service names were checked against official sources", NOT_VERIFIED, "No fact check was run."))
    elif f.get("facts"):
        out.append(chk("X4", "evidence", 7, "Service names were checked against official sources", PASS, "%d fact(s) recorded with an official source." % len(f["facts"])))
    else:
        out.append(chk("X4", "evidence", 7, "Service names were checked against official sources", NOT_VERIFIED,
                       "Skipped: %s." % (f.get("skipped") or {}).get("reason", "no reason given") if f.get("skipped") else "No fact was recorded."))
    return out


# ---------------------------------------------------------------- gate 8: company rules
RULE_AREA = {"budget_monthly": "cost", "no_public_database": "security", "no_public_storage": "security", "encryption_at_rest": "security",
             "encryption_in_transit": "security", "customer_managed_keys": "security", "ha_multi_az": "reliability", "rpo_minutes": "reliability",
             "rto_minutes": "reliability", "log_retention_min_days": "reliability", "log_retention_max_days": "security", "required_tags": "infrastructure",
             "naming_prefix": "infrastructure", "iac_tool": "infrastructure", "ci_provider": "infrastructure"}


def gate8(i):
    """Every active company rule, checked against the plan and the generated files. No rules documents means no checks."""
    data = i.company
    out = []
    if not data:
        return out
    for conflict in data.get("conflicts", []):
        out.append(chk("P-conflict", "architecture", 8, "Company rules agree with each other", FAIL, "%s (%s)." % (conflict["message"], ", ".join(conflict["rules"])),
                       conflict["rules"], blocker=True, criterion="constraints"))
    for rule in company_rules.active(data):
        status, detail, evidence = company_rules.check(rule, i.plan, i.iac, i.costs, i.design, i.profile, i.manifest)
        name = "%s %s" % (rule["kind"].replace("_", " "), "" if rule["value"] is True else json.dumps(rule["value"], ensure_ascii=False)[:60])
        out.append(chk(rule["id"], RULE_AREA.get(rule["kind"], "architecture"), 8, "Company rule: " + name.strip(), status, detail, evidence,
                       blocker=(rule["level"] == "blocking"), criterion="constraints" if rule["level"] != "advisory" else None))
    return out


# ---------------------------------------------------------------- scoring
def score(checks):
    areas = []
    total = 0.0
    for key, label, weight in AREAS:
        rows = [c for c in checks if c["area"] == key and c["status"] != NA]
        if not rows:
            earned, share = float(weight), 1.0
        else:
            share = sum(CREDIT[c["status"]] for c in rows) / len(rows)
            earned = weight * share
        lost = [c for c in rows if c["status"] != PASS]
        areas.append({"area": key, "label": label, "weight": weight, "earned": round(earned, 1), "checks": len(rows),
                      "lost": [{"id": c["id"], "status": c["status"], "name": c["name"], "detail": c["detail"]} for c in lost]})
        total += earned
    return round(total), areas


def band(value):
    for floor, name, advice in BANDS:
        if value >= floor:
            return name, advice


ACCEPTANCE = [("components", "Deployment-critical components identified", lambda c, r: not any(x["id"] in ("R1", "R2") and x["status"] == FAIL for x in c)),
              ("compatible", "No unsupported service combination", lambda c, r: not any(x["criterion"] == "compatible" and x["status"] == FAIL for x in c)),
              ("validated", "Real validators ran and passed", lambda c, r: any(x["id"] == "I1" and x["status"] == PASS for x in c)),
              ("reconciled", "Every known billable resource reconciled with the cost report", lambda c, r: any(x["id"] == "C1" and x["status"] == PASS for x in c)),
              ("security", "No unresolved critical security finding", lambda c, r: not any(x["id"] == "S1" and x["status"] == FAIL for x in c)),
              ("blockers", "No critical deployment blocker", lambda c, r: not r),
              ("constraints", "Every mandatory user requirement satisfied", lambda c, r: not any(x["criterion"] == "constraints" and x["status"] == FAIL for x in c)),
              ("consistent", "No critical contradiction between artifacts", lambda c, r: not any(x["criterion"] == "consistent" and x["status"] == FAIL for x in c)),
              ("traceable", "Every major decision traceable to evidence", lambda c, r: not any(x["criterion"] == "traceable" and x["status"] != PASS for x in c))]


def evaluate(repo):
    i = Inputs(repo)
    if not (i.scan and i.plan and i.design):
        return {"verdict": "error", "message": "No scan, plan and design found. Run the scan, plan and finish steps first."}
    checks = gate1(i) + gate2(i) + gate3(i) + gate4(i) + gate5(i) + gate6(i) + gate7(i) + gate8(i)
    raw, areas = score(checks)
    blockers = [c for c in checks if c["blocker"]]
    acceptance = [{"id": k, "name": n, "met": bool(f(checks, blockers))} for k, n, f in ACCEPTANCE]
    unmet = [a for a in acceptance if not a["met"]]
    final = min(raw, CAP_SCORE) if unmet else raw
    name, advice = band(final)
    gates = []
    for gid, gname in GATES:
        rows = [c for c in checks if c["gate"] == gid]
        sts = {c["status"] for c in rows if c["status"] != NA}
        status = FAIL if FAIL in sts else (NOT_VERIFIED if NOT_VERIFIED in sts else (WARNING if WARNING in sts else (PASS if sts else NA)))
        gates.append({"gate": gid, "name": gname, "status": status, "checks": rows})
    return {"verdict": "ok", "plan_fingerprint": i.plan["fingerprint"], "score": final, "raw_score": raw,
            "capped": bool(unmet and raw > CAP_SCORE), "cap": CAP_SCORE if unmet else None, "unmet": unmet, "acceptance": acceptance,
            "band": name, "advice": advice, "areas": areas, "gates": gates,
            "blockers": [{"id": c["id"], "name": c["name"], "detail": c["detail"]} for c in blockers], "deploy_ready": not blockers and final >= 85,
            "not_verified": [{"id": c["id"], "name": c["name"], "detail": c["detail"]} for c in checks if c["status"] == NOT_VERIFIED],
            "contradictions": contradictions(i),
            "rule_results": [{"rule": c["id"], "status": c["status"], "detail": c["detail"]} for c in checks if c["gate"] == 8]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Seven validation gates and the architecture quality score")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    result = evaluate(args.repo)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["verdict"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
