#!/usr/bin/env python3
"""Company rules: build them from the documents beside the code, constrain the plan with them, and check the result.

    rules.py ingest --repo REPO [--rules PATH ...]     read the documents, write .solsarch/rules.json
    rules.py show --repo REPO                          the rules in force, where each came from, what could not be understood

The rules come from json, yaml, md, txt, html, docx or pdf files (see rules_ingest.py and rules_extract.py). They shape the plan
before anything is generated (allowed clouds, region, budget, compliance, recovery objectives), they add tags, a name prefix and
log retention to the generated code, and the quality gate checks every rule against the result. A rule is never silently dropped:
a sentence that was not understood is listed as not enforced. Standard library only; the same files always give the same output.
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import providers  # noqa: E402
import rules_extract  # noqa: E402
import rules_ingest  # noqa: E402

RULES_FILE = "rules.json"
SOURCES_FILE = "rules-sources.json"
SCHEMA = 1
PASS, FAIL, WARNING, NOT_VERIFIED, NA = "PASS", "FAIL", "WARNING", "NOT_VERIFIED", "N/A"
PROVIDER_WORD = {"aws": "AWS", "azure": "Azure", "gcp": "Google", "hetzner": "Hetzner", "ovhcloud": "OVHcloud", "scaleway": "Scaleway",
                 "upcloud": "UpCloud", "exoscale": "Exoscale", "digitalocean": "DigitalOcean", "vultr": "Vultr", "linode": "Linode"}
CLOUDWATCH_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653)


def today():
    return os.environ.get("SOLSARCH_TODAY") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def state(repo):
    return Path(repo) / ".solsarch"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes((json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + chr(10)).encode("utf-8"))


# ---------------------------------------------------------------- building the rule set
def set_sources(repo, paths):
    """Remember where the rules documents are, so later steps read the same files."""
    clean = []
    for item in paths or []:
        p = Path(item)
        clean.append(str(p if p.is_absolute() else (Path.cwd() / p).resolve()))
    write_json(state(repo) / SOURCES_FILE, {"paths": sorted(set(clean))})
    return clean


def stored_sources(repo):
    data = read_json(state(repo) / SOURCES_FILE)
    return list((data or {}).get("paths") or [])


def apply_overrides(rules, overrides):
    """Mark rules ignored or excepted. An exception with a past expiry date no longer protects the rule."""
    current = today()
    notes = []
    for rule in rules:
        rule["status"] = "active"
        for ov in overrides:
            if ov["rule"] not in (rule["id"], rule["kind"]):
                continue
            expires = ov.get("expires") or ""
            expired = False
            if expires:
                try:
                    expired = date.fromisoformat(expires) < date.fromisoformat(current)
                except ValueError:
                    notes.append("exception for %s has an expiry that is not a date (YYYY-MM-DD): %s" % (ov["rule"], expires))
                    expired = True
            if ov.get("ignore"):
                rule["status"] = "ignored"
            elif expired:
                rule["exception_expired"] = expires
                notes.append("the exception for %s expired on %s, so the rule applies again" % (rule["id"], expires))
            else:
                rule["status"] = "excepted"
                rule["exception"] = {"reason": ov.get("reason", ""), "owner": ov.get("owner", ""), "expires": expires}
    return notes


def implied(rules):
    """Rules a named compliance framework brings with it. They are advisory: a mapping to checks, not a certification."""
    table = rules_extract.frameworks()
    out, seen = [], set()
    for rule in rules:
        if rule["kind"] != "compliance" or rule.get("status") != "active":
            continue
        for fw in rule["value"]:
            for kind in table.get(fw, {}).get("implies", []):
                key = (fw, kind)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"id": "R-implied-%s-%s" % (fw, kind.replace("_", "-")), "kind": kind, "value": True, "level": "advisory", "origin": "framework",
                            "status": "active", "source": {"file": rule["source"]["file"], "locator": rule["source"]["locator"],
                                                           "sentence": "%s names %s" % (rule["id"], table[fw]["name"]), "sha256": rule["source"]["sha256"]}})
    return out


def merge(rules):
    """The effective constraints from the active rules, plus any pair of rules that cannot both hold."""
    act = [r for r in rules if r.get("status") == "active"]
    by = {}
    for r in act:
        by.setdefault(r["kind"], []).append(r)
    constraints, conflicts = {}, []
    if by.get("allowed_clouds"):
        allowed = set(by["allowed_clouds"][0]["value"])
        for r in by["allowed_clouds"][1:]:
            allowed &= set(r["value"])
        constraints["allowed_clouds"] = sorted(allowed)
        if not allowed:
            conflicts.append({"rules": [r["id"] for r in by["allowed_clouds"]], "message": "the allowed-cloud rules have no cloud in common"})
    denied = sorted({c for r in by.get("denied_clouds", []) for c in r["value"]})
    if denied:
        constraints["denied_clouds"] = denied
    remaining = providers.hosting_ids()
    if "allowed_clouds" in constraints:
        remaining = [c for c in remaining if c in constraints["allowed_clouds"]]
    remaining = [c for c in remaining if c not in denied]
    constraints["compliant_clouds"] = remaining
    if (by.get("allowed_clouds") or by.get("denied_clouds")) and not remaining:
        conflicts.append({"rules": [r["id"] for k in ("allowed_clouds", "denied_clouds") for r in by.get(k, [])],
                          "message": "no cloud or platform is left after the allowed and denied cloud rules"})
    if by.get("budget_monthly"):
        constraints["budget_monthly"] = min(r["value"] for r in by["budget_monthly"])
    for kind in ("rpo_minutes", "rto_minutes"):
        if by.get(kind):
            constraints[kind] = min(r["value"] for r in by[kind])
    if by.get("log_retention_min_days"):
        constraints["log_retention_min_days"] = max(r["value"] for r in by["log_retention_min_days"])
    if by.get("log_retention_max_days"):
        constraints["log_retention_max_days"] = min(r["value"] for r in by["log_retention_max_days"])
    lo, hi = constraints.get("log_retention_min_days"), constraints.get("log_retention_max_days")
    if lo and hi and lo > hi:
        conflicts.append({"rules": [r["id"] for k in ("log_retention_min_days", "log_retention_max_days") for r in by.get(k, [])],
                          "message": "logs must be kept at least %d days and at most %d days" % (lo, hi)})
    if by.get("required_tags"):
        constraints["required_tags"] = sorted({t for r in by["required_tags"] for t in r["value"]})
    prefixes = sorted({r["value"] for r in by.get("naming_prefix", [])})
    if prefixes:
        constraints["naming_prefix"] = prefixes[0]
        if len(prefixes) > 1:
            conflicts.append({"rules": [r["id"] for r in by["naming_prefix"]], "message": "two different name prefixes are required: %s" % ", ".join(prefixes)})
    if by.get("compliance"):
        constraints["compliance"] = sorted({c for r in by["compliance"] for c in r["value"]})
    models = sorted({r["value"] for r in by.get("compute_model", [])})
    if models:
        constraints["compute_model"] = models[0]
    denied_services = sorted({s for r in by.get("denied_services", []) for s in r["value"]})
    if denied_services:
        constraints["denied_services"] = denied_services
    if "serverless" in denied_services and constraints.get("compute_model") == "serverless":
        conflicts.append({"rules": [r["id"] for k in ("denied_services", "compute_model") for r in by.get(k, [])], "message": "one rule requires serverless and another forbids it"})
    for kind in ("data_residency", "allowed_regions"):
        if by.get(kind):
            constraints[kind] = [r["value"] for r in by[kind]]
    for kind in ("no_public_database", "no_public_storage", "encryption_at_rest", "encryption_in_transit", "customer_managed_keys", "ha_multi_az"):
        if by.get(kind):
            constraints[kind] = True
    return constraints, conflicts


def build(repo, extra=None, write=True):
    """Read the documents, extract the rules, apply exceptions and write .solsarch/rules.json. Returns the data, or None if there are no rules files."""
    paths = list(extra) if extra else stored_sources(repo)
    docs, problems = rules_ingest.read_all(repo, paths)
    if not docs and not paths:
        return None
    result = rules_extract.extract(docs)
    rules = result["rules"]
    notes = apply_overrides(rules, result["overrides"])
    rules = rules + implied(rules)
    rules.sort(key=lambda r: (r["kind"], r["id"]))
    constraints, conflicts = merge(rules)
    data = {"schema": SCHEMA, "files": result["files"], "rules": rules, "not_enforced": result["not_enforced"],
            "problems": sorted(set(problems + result["problems"] + notes)), "conflicts": conflicts, "constraints": constraints,
            "resolved_region": (read_json(state(repo) / RULES_FILE) or {}).get("resolved_region")}
    if write:
        write_json(state(repo) / RULES_FILE, data)
    return data


def load(repo):
    return read_json(state(repo) / RULES_FILE)


def ensure(repo, extra=None):
    """Rebuild from the documents when there are any (so an edit to a rules file is always seen); else return what was saved."""
    if extra:
        set_sources(repo, extra)
    data = build(repo)
    return data if data is not None else load(repo)


def active(data):
    return [r for r in (data or {}).get("rules", []) if r.get("status") == "active"]


# ---------------------------------------------------------------- shaping the plan
def violations(data, cloud, compute, overlays=()):
    """Reasons this cloud and compute model break a rule, before anything is planned."""
    if not data:
        return []
    c = data.get("constraints", {})
    out = []
    used = [cloud] + [o for o in overlays if o]
    for r in active(data):
        k = r["kind"]
        if k == "allowed_clouds":
            for p in used:
                if p not in r["value"]:
                    out.append({"rule": r["id"], "level": r["level"], "message": "%s is not in the approved clouds (%s)" % (p, ", ".join(r["value"]))})
        elif k == "denied_clouds":
            for p in used:
                if p in r["value"]:
                    out.append({"rule": r["id"], "level": r["level"], "message": "%s is not allowed by the company rules" % p})
        elif k == "denied_services" and "serverless" in r["value"] and compute == "serverless":
            out.append({"rule": r["id"], "level": r["level"], "message": "serverless is not allowed by the company rules"})
        elif k == "compute_model" and r["value"] != compute:
            out.append({"rule": r["id"], "level": "advisory" if r["level"] == "advisory" else r["level"], "message": "the rules ask for %s compute" % r["value"]})
    out.sort(key=lambda x: (x["rule"], x["message"]))
    return [dict(v, compliant_clouds=c.get("compliant_clouds", [])) for v in out]


def blocking(vs):
    return [v for v in vs if v["level"] != "advisory"]


def pick_region(data, cloud):
    """A region that satisfies every residency and region rule for this cloud, or None."""
    if not data:
        return None, None
    table = rules_extract.regions_table()
    word = PROVIDER_WORD.get(cloud)
    if not word:
        return None, None
    rules = [r for r in active(data) if r["kind"] in ("data_residency", "allowed_regions")]
    if not rules:
        return None, None

    def fits(region):
        geos = rules_extract.geos_for_region(region)
        if geos is None:
            return False
        for r in rules:
            if r["kind"] == "data_residency" and not (geos & set(r["value"])):
                return False
            if r["kind"] == "allowed_regions" and region not in r["value"]:
                return False
        return True

    candidates = []
    for r in rules:
        if r["kind"] == "allowed_regions":
            candidates += [(x, r["id"]) for x in r["value"]]
    for r in rules:
        if r["kind"] == "data_residency":
            for geo in r["value"]:
                default = table["defaults"].get(geo, {}).get(cloud)
                if default:
                    candidates.append((default, r["id"]))
    for region, rid in candidates:
        row = next((row for row in table["regions"] if re.search(row["re"], region)), None)
        if row and row["label"].startswith(word) and fits(region):
            return region, rid
    return None, None


def resolve(repo, cloud):
    """Record the region the rules pick for this cloud, so the profile (and so the generated code) uses it."""
    data = load(repo)
    if not data:
        return None
    region, rid = pick_region(data, cloud)
    data["resolved_region"] = {"cloud": cloud, "region": region, "rule": rid} if region else None
    write_json(state(repo) / RULES_FILE, data)
    return data["resolved_region"]


def overlay(repo, profile):
    """Fill the profile from the rules. A value the user set is kept unless a rule is stricter. Returns (profile, list of changes)."""
    data = load(repo)
    if not data:
        return profile, []
    c = data.get("constraints", {})
    changes = []
    p = dict(profile)
    if c.get("budget_monthly") is not None and not any(r.get("currency") not in (None, "USD") for r in active(data) if r["kind"] == "budget_monthly"):
        budget = c["budget_monthly"]
        if p.get("monthly_budget") in (None, 0) or float(p["monthly_budget"]) > budget:
            p["monthly_budget"] = int(budget) if float(budget) == int(budget) else budget
            changes.append("monthly_budget = %s" % p["monthly_budget"])
    for kind in ("rto_minutes", "rpo_minutes"):
        if c.get(kind) is not None and (p.get(kind) in (None, 0) or p[kind] > c[kind]):
            p[kind] = c[kind]
            changes.append("%s = %s" % (kind, c[kind]))
    if c.get("compliance"):
        merged = sorted(set(p.get("compliance") or []) | {x for x in c["compliance"] if x in ("soc2", "hipaa", "pci-dss", "gdpr", "iso27001", "nist-csf", "fedramp", "eu-ai-act", "dora", "nis2")})
        if merged != sorted(p.get("compliance") or []):
            p["compliance"] = merged
            changes.append("compliance = %s" % ", ".join(merged))
    res = data.get("resolved_region")
    if res and res.get("region") and not p.get("region"):
        p["region"] = res["region"]
        changes.append("region = %s (from %s)" % (res["region"], res["rule"]))
    return p, changes


# ---------------------------------------------------------------- generated files
def _closing(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _hcl(value):
    return json.dumps(value)


def apply_to_files(files, data, provider):
    """Add the required tags, name prefix and log retention to the generated Terraform. Returns (files, notes)."""
    out = dict(files)
    notes = []
    c = (data or {}).get("constraints", {})
    tf_paths = sorted(p for p in out if p.endswith(".tf") and p.startswith("terraform/") and "/bootstrap/" not in p)
    tags = c.get("required_tags")
    if tags and tf_paths:
        label = provider == "gcp"
        keys = [t.lower() for t in tags] if label else list(tags)
        body = ["variable \"company_tags\" {",
                "  description = \"Values for the %s your company rules require on every resource. Fill them in.\"" % ("labels" if label else "tags"),
                "  type        = map(string)", "  default = {"]
        width = max(len(_hcl(k)) for k in keys)
        for k in keys:
            body.append("    %s = %s" % (_hcl(k).ljust(width), _hcl("replace-with-" + re.sub(r"[^a-z0-9]+", "-", k.lower()).strip("-"))))
        body += ["  }", "}", ""]
        out["terraform/company-tags.tf"] = chr(10).join(body)
        applied = 0
        if provider == "aws":
            for path in tf_paths:
                text = out[path]
                new = text
                idx = 0
                pieces = []
                while True:
                    at = new.find("default_tags {", idx)
                    if at < 0:
                        pieces.append(new[idx:])
                        break
                    tag_at = new.find("tags = {", at)
                    brace = new.find("{", tag_at)
                    close = _closing(new, brace)
                    pieces.append(new[idx:tag_at] + "tags = merge(var.company_tags, " + new[brace:close + 1] + ")")
                    idx = close + 1
                    applied += 1
                out[path] = "".join(pieces)
        elif provider in ("azure", "gcp"):
            attr = "labels" if provider == "gcp" else "tags"
            pattern = re.compile(r"(?m)^(\s*)%s = \{" % attr)
            for path in tf_paths:
                text = out[path]
                pieces, idx = [], 0
                for m in pattern.finditer(text):
                    if m.start() < idx:
                        continue
                    brace = m.end() - 1
                    close = _closing(text, brace)
                    if close < 0:
                        continue
                    pieces.append(text[idx:m.start()] + m.group(1) + "%s = merge(var.company_tags, " % attr + text[brace:close + 1] + ")")
                    idx = close + 1
                    applied += 1
                pieces.append(text[idx:])
                out[path] = "".join(pieces)
        if provider in ("aws", "azure", "gcp") and not applied:
            notes.append("the required %s are declared in terraform/company-tags.tf but no resource block takes them" % ("labels" if label else "tags"))
        elif provider not in ("aws", "azure", "gcp"):
            notes.append("the required tags are declared in terraform/company-tags.tf; this provider's resources need `tags = var.company_tags` added where the provider supports tags")
    prefix = c.get("naming_prefix")
    if prefix:
        path = "terraform/terraform.tfvars.example"
        if path in out:
            out[path] = re.sub(r'(?m)^(project_name\s*=\s*")(?!%s)' % re.escape(prefix), lambda m: m.group(1) + prefix, out[path])
    lo, hi = c.get("log_retention_min_days"), c.get("log_retention_max_days")
    if lo or hi:
        for path in tf_paths:
            text = out[path]
            m = re.search(r'variable "log_retention_days" \{.*?default\s*=\s*(\d+)', text, re.S)
            if not m:
                continue
            current = int(m.group(1))
            target = current
            if lo and target < lo:
                target = next((d for d in CLOUDWATCH_DAYS if d >= lo), lo) if provider == "aws" else lo
            if hi and target > hi:
                target = max([d for d in CLOUDWATCH_DAYS if d <= hi] or [hi]) if provider == "aws" else hi
            if target != current:
                out[path] = text[:m.start(1)] + str(target) + text[m.end(1):]
    return out, notes


# ---------------------------------------------------------------- checking
def _blocks(text):
    """(resource type, body) for each resource block in a Terraform file."""
    out = []
    for m in re.finditer(r'(?m)^resource "([a-z0-9_]+)" "[^"]+" \{', text):
        close = _closing(text, m.end() - 1)
        out.append((m.group(1), text[m.end() - 1:close + 1 if close > 0 else len(text)]))
    return out


def scan_regions(iac):
    """(region ids found, unknown count). The us-east-1 provider alias that CloudFront needs for its certificate is ignored."""
    found = {}
    for path, text in sorted(iac.items()):
        if not path.endswith((".tf", ".tfvars.example", ".yml", ".yaml", ".json", ".toml")) or "/bootstrap/" in path:
            continue
        text = re.sub(r'provider "aws" \{\s*alias\s*=\s*"us_east_1".*?\n\}\n', "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*(#|//).*$", "", text)
        for token in re.findall(r'"([a-z][a-z0-9-]{1,40})"', text):
            if rules_extract.geos_for_region(token) is not None:
                found.setdefault(token, path)
    return found


PUBLIC_DB = re.compile(r"publicly_accessible\s*=\s*true|public_network_access_enabled\s*=\s*true|ipv4_enabled\s*=\s*true|0\.0\.0\.0/0|public_access\s*=\s*true")
DB_TYPE = re.compile(r"db_instance|rds_cluster|docdb|elasticache|redshift|sql_database|sql_server|mysql|postgres|redis|cosmosdb|mongo|database")
PUBLIC_STORAGE = re.compile(r"public-read|allUsers|allAuthenticatedUsers|block_public_acls\s*=\s*false|restrict_public_buckets\s*=\s*false|"
                            r"allow_blob_public_access\s*=\s*true|allow_nested_items_to_be_public\s*=\s*true|public_access_prevention\s*=\s*\"inherited\"")
STORAGE_TYPE = re.compile(r"s3_bucket|storage_bucket|storage_account|storage_container|object_storage|spaces_bucket|bucket")
SERVICE_MARK = {"encryption_at_rest": re.compile(r"storage_encrypted\s*=\s*true|\bencrypted\s*=\s*true|kms_key_id|kms_key_arn|sse_algorithm|server_side_encryption|"
                                                 r"encryption_at_rest|infrastructure_encryption|customer_managed_encryption|encryption_configuration|encryption\s*\{"),
                "encryption_in_transit": re.compile(r"redirect-to-https|redirect_http_to_https|https_only|min_tls_version|minimum_tls_version|ssl_enforcement|"
                                                    r"require_ssl|force_ssl|https_traffic_only|enable_https_traffic_only|ssl_mode|HTTPS|protocol\s*=\s*\"https\"|tls_policy|ssl_policy")}


def _status(rule, ok, detail, evidence=()):
    if ok is None:
        return NOT_VERIFIED, detail, list(evidence)
    if ok:
        return PASS, detail, list(evidence)
    return (WARNING if rule["level"] == "advisory" else FAIL), detail, list(evidence)


def check(rule, plan, iac, costs, design, profile, manifest):
    """(status, detail, evidence) for one active rule against the plan and the generated files."""
    k, v = rule["kind"], rule["value"]
    cloud = plan["cloud"]
    used = sorted({cloud} | {d.get("provider") for d in plan.get("decisions", []) if d.get("provider")})
    text_all = "\n".join(t for p, t in sorted(iac.items()) if p.endswith(".tf") or p.endswith((".yaml", ".yml", ".toml", ".json")))
    has_iac = bool(iac)
    if k == "allowed_clouds":
        bad = [p for p in used if p not in v]
        return _status(rule, not bad, "Providers in the plan: %s." % ", ".join(used) if not bad else "%s are not in the approved clouds (%s)." % (", ".join(bad), ", ".join(v)))
    if k == "denied_clouds":
        bad = [p for p in used if p in v]
        return _status(rule, not bad, "No denied cloud is used." if not bad else "The plan uses %s, which the rules forbid." % ", ".join(bad))
    if k == "preferred_clouds":
        return PASS if cloud in v else WARNING, ("Uses the preferred cloud %s." % cloud) if cloud in v else "%s is not one of the preferred clouds (%s)." % (cloud, ", ".join(v)), []
    if k in ("data_residency", "allowed_regions"):
        regions = dict(scan_regions(iac))
        if profile.get("region"):
            regions.setdefault(str(profile["region"]).lower(), "profile")
        if not regions:
            return NOT_VERIFIED, "No region id appears in the generated files, so where the data lives cannot be checked. Set the region in the profile.", []
        bad, unknown = [], []
        for region, where in sorted(regions.items()):
            geos = rules_extract.geos_for_region(region)
            if geos is None:
                unknown.append(region)
            elif k == "data_residency" and not (geos & set(v)):
                bad.append(region)
            elif k == "allowed_regions" and region not in v:
                bad.append(region)
        names = ", ".join(sorted(regions))
        if bad:
            return _status(rule, False, "Region(s) %s are outside what the rule allows (%s)." % (", ".join(bad), ", ".join(v)), [regions[b] for b in bad])
        known = [r for r in regions if r not in unknown]
        if not known:
            return NOT_VERIFIED, "The regions found (%s) are not in the region table." % names, []
        return PASS, "Regions used: %s; all inside %s." % (", ".join(sorted(known)), ", ".join(v)), []
    if k == "denied_services":
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        names = " ".join(d.get("service", "") for d in plan.get("decisions", []) if d.get("provider", cloud) == cloud)
        hits, ev = [], []
        for key in v:
            term, res = rules_extract.SERVICE_TERMS[key]
            in_plan = re.search(term, names, re.I)
            in_files = next((p for p, t in sorted(iac.items()) if re.search(res, t) and not p.endswith(("README.md",))), None)
            if in_plan or in_files:
                hits.append(key)
                ev.append(in_files or "plan")
        return _status(rule, not hits, "None of %s is used." % ", ".join(v) if not hits else "Used although forbidden: %s." % ", ".join(hits), ev)
    if k == "no_public_database":
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        bad = [p for p, t in sorted(iac.items()) if p.endswith(".tf") for typ, body in _blocks(t) if DB_TYPE.search(typ) and PUBLIC_DB.search(body)]
        return _status(rule, not bad, "No database resource is open to the internet." if not bad else "A database resource allows public access.", bad)
    if k == "no_public_storage":
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        bad = [p for p, t in sorted(iac.items()) if p.endswith(".tf") for typ, body in _blocks(t) if STORAGE_TYPE.search(typ) and PUBLIC_STORAGE.search(body)]
        extra = [p for p, t in sorted(iac.items()) if p.endswith(".tf") and re.search(r"google_storage_bucket_iam_\w+.*?allUsers|aws_s3_bucket_policy.*?\"Principal\"\s*:\s*\"\*\"", t, re.S) and p not in bad]
        bad += extra
        return _status(rule, not bad, "No storage resource is open to the internet." if not bad else "A storage resource allows public access.", bad)
    if k in ("encryption_at_rest", "encryption_in_transit"):
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        mark = next((p for p, t in sorted(iac.items()) if SERVICE_MARK[k].search(t)), None)
        if mark:
            return PASS, "An explicit setting appears in the generated code.", [mark]
        return WARNING, "No explicit setting appears in the generated code. Most services encrypt by default; confirm it for your provider.", []
    if k == "customer_managed_keys":
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        mark = next((p for p, t in sorted(iac.items()) if re.search(r"aws_kms_key\b|google_kms_crypto_key|azurerm_key_vault_key|customer_managed_key", t)), None)
        return _status(rule, bool(mark), "A customer-managed key is created." if mark else "No customer-managed key is created by the generated code.", [mark] if mark else [])
    if k == "required_tags":
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        tag_file = iac.get("terraform/company-tags.tf", "")
        label = cloud == "gcp"
        missing = [t for t in v if (t.lower() if label else t) not in tag_file and '"%s"' % t not in text_all]
        if missing:
            return _status(rule, False, "Required tag(s) missing: %s." % ", ".join(missing), [])
        used_count = text_all.count("var.company_tags")
        if used_count >= 1 and cloud in ("aws", "azure", "gcp"):
            return PASS, "Every required tag is declared and merged into the resources (%d place(s))." % used_count, ["terraform/company-tags.tf"]
        return WARNING, "The tags are declared in terraform/company-tags.tf but not attached to every resource for this provider; add `tags = var.company_tags`.", ["terraform/company-tags.tf"]
    if k == "naming_prefix":
        tfvars = iac.get("terraform/terraform.tfvars.example", "")
        m = re.search(r'(?m)^project_name\s*=\s*"([^"]*)"', tfvars)
        if not m:
            return NOT_VERIFIED, "No project_name to check; apply the prefix %s yourself." % v, []
        return _status(rule, m.group(1).startswith(v), "project_name is %s." % m.group(1) if m.group(1).startswith(v) else "project_name %s does not start with %s." % (m.group(1), v), ["terraform/terraform.tfvars.example"])
    if k == "budget_monthly":
        total = ((costs or {}).get("totals") or {}).get("monthly")
        if not (costs and costs.get("lines")) or total is None:
            return NOT_VERIFIED, "No priced estimate exists to compare with the budget.", []
        cur = rule.get("currency") or "USD"
        if cur != "USD":
            return WARNING, "The budget is in %s but the estimate is in USD (%.2f); compare them yourself." % (cur, total), []
        return _status(rule, total <= v, "Estimate %.2f against a budget of %s." % (total, v))
    if k in ("rpo_minutes", "rto_minutes"):
        if not has_iac:
            return NOT_VERIFIED, "No generated files to check.", []
        mark = next((p for p, t in sorted(iac.items()) if re.search(r"backup_retention_period|point_in_time|pitr|backup_enabled|backup\s*\{|backup_policy|automated_backup", t)), None)
        if mark:
            return PASS, "Automated backups are configured; the %s of %d minutes is a design target that only a restore test proves." % ("RPO" if k.startswith("rpo") else "RTO", v), [mark]
        return WARNING, "The %s objective is recorded, but the generated code has no backup setting to meet it." % ("RPO" if k.startswith("rpo") else "RTO"), []
    if k == "ha_multi_az":
        ok = (profile.get("high_availability") != "single_az") and not (design or {}).get("single_az")
        return _status(rule, ok, "The design spreads across zones." if ok else "The design is single-zone (lean account or an explicit choice).")
    if k in ("log_retention_min_days", "log_retention_max_days"):
        found = [int(x) for t in iac.values() for x in re.findall(r'variable "log_retention_days" \{.*?default\s*=\s*(\d+)', t, re.S)]
        found += [int(x) for t in iac.values() for x in re.findall(r"retention_in_days\s*=\s*(\d+)", t)]
        if not found:
            return NOT_VERIFIED, "No log retention setting appears in the generated code.", []
        ok = min(found) >= v if k.endswith("min_days") else max(found) <= v
        return _status(rule, ok, "Log retention in the code: %s days." % ", ".join(str(x) for x in sorted(set(found))))
    if k == "compliance":
        return NA, "Checked through the rules it implies (%s); a certification needs an assessor." % ", ".join(v), []
    if k == "compute_model":
        return _status(rule, plan["compute_model"] == v, "The plan uses %s compute." % plan["compute_model"])
    if k == "iac_tool":
        ok = any(t in ("terraform", "opentofu") for t in v) and any(p.endswith(".tf") for p in iac)
        ok = ok or ("ansible" in v and any(p.startswith("ansible/") for p in iac))
        return _status(rule, ok, "The code is Terraform (also valid for OpenTofu)." if ok else "SolsArch writes Terraform, Kubernetes, Ansible and platform files, not %s." % ", ".join(v))
    if k == "ci_provider":
        ok = "github-actions" in v and any(p.startswith(".github/workflows/") for p in iac)
        return _status(rule, ok, "A GitHub Actions pipeline is generated." if ok else "SolsArch writes a GitHub Actions pipeline; %s needs the same steps copied over." % ", ".join(v))
    return NOT_VERIFIED, "No check is defined for %s." % k, []


# ---------------------------------------------------------------- reports
def render_markdown(data, results=None):
    lines = ["# Company rules", "",
             "Rules read from the documents beside the code. Each one shows where it came from. `extracted` rules were picked out of prose by fixed patterns, so read them; "
             "`structured` rules were written as data. Nothing here is a certification.", ""]
    if not data:
        return chr(10).join(lines + ["No rules documents were found."]) + chr(10)
    lines += ["## Documents read", "", "| File | Type | Problem |", "|---|---|---|"]
    for f in data["files"]:
        lines.append("| %s | %s | %s |" % (f["name"], f["kind"], f["problem"] or "none"))
    lines += ["", "## Rules in force", "", "| Rule | What | Level | Origin | Status | Source |", "|---|---|---|---|---|---|"]
    by_id = {r["rule"]["id"]: r for r in (results or [])}
    for r in data["rules"]:
        status = r.get("status", "active")
        if status == "active" and r["id"] in by_id:
            status = by_id[r["id"]]["status"]
        what = "%s: %s" % (r["kind"], json.dumps(r["value"], ensure_ascii=False))
        src = r["source"]
        lines.append("| %s | %s | %s | %s | %s | %s, %s: \"%s\" |" % (r["id"], what.replace("|", "/"), r["level"], r["origin"], status, src["file"], src["locator"],
                                                                      src["sentence"][:140].replace("|", "/").replace(chr(10), " ")))
    if data["conflicts"]:
        lines += ["", "## Conflicts", ""] + ["- %s (%s)" % (c["message"], ", ".join(c["rules"])) for c in data["conflicts"]]
    if data["not_enforced"]:
        lines += ["", "## Attached but not enforced", "",
                  "These sentences sound like rules but matched no check. Rewrite them as `rule: KIND = VALUE` lines or a JSON rules file to have them enforced.", ""]
        lines += ["- %s, %s: \"%s\"" % (n["file"], n["locator"], n["sentence"][:200]) for n in data["not_enforced"]]
    if data["problems"]:
        lines += ["", "## Problems reading the documents", ""] + ["- %s" % p for p in data["problems"]]
    return chr(10).join(lines) + chr(10)


REGO_HEADER = '''# Generated by SolsArch from your company rules. Run with Conftest or OPA against `terraform show -json` output.
# Rules in force: %d. Each deny message names the rule id and the document sentence it came from.
package solsarch.company

import rego.v1
'''


def rego(data):
    """An OPA policy for the rules that can be tested on a Terraform plan, so the pipeline keeps enforcing them."""
    rules = active(data)
    parts = [REGO_HEADER % len(rules)]
    for r in rules:
        rid = r["id"]
        src = r["source"]["sentence"][:120].replace('"', "'").replace(chr(92), "/")
        msg = "%s: %s" % (rid, src)
        if r["kind"] == "no_public_database":
            parts.append('deny contains msg if {\n\tsome rc in input.resource_changes\n\tregex.match("db_instance|rds_cluster|sql_database|redis|mongo|docdb", rc.type)\n'
                         '\trc.change.after.publicly_accessible == true\n\tmsg := sprintf("%%s (%%s)", ["%s", rc.address])\n}\n' % msg)
        elif r["kind"] == "required_tags":
            for tag in r["value"]:
                parts.append('deny contains msg if {\n\tsome rc in input.resource_changes\n\tregex.match("^aws_", rc.type)\n\tobject.get(rc.change.after, "tags_all", {})\n'
                             '\tnot rc.change.after.tags_all["%s"]\n\tmsg := sprintf("%%s: missing tag %s on %%s", ["%s", rc.address])\n}\n' % (tag, tag, rid))
        elif r["kind"] == "denied_services":
            for key in r["value"]:
                parts.append('deny contains msg if {\n\tsome rc in input.resource_changes\n\tregex.match("%s", rc.type)\n\tmsg := sprintf("%%s (%%s)", ["%s", rc.address])\n}\n'
                             % (rules_extract.SERVICE_TERMS[key][1].replace(chr(92), chr(92) * 2), msg))
    if len(parts) == 1:
        parts.append("# None of the current rules can be tested on a Terraform plan.\ndeny contains msg if { false; msg := \"\" }\n")
    return chr(10).join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Company rules")
    parser.add_argument("action", choices=("ingest", "show"))
    parser.add_argument("--repo", default=".")
    parser.add_argument("--rules", action="append")
    args = parser.parse_args(argv)
    if args.action == "ingest":
        if args.rules:
            set_sources(args.repo, args.rules)
        data = build(args.repo)
    else:
        data = load(args.repo) or build(args.repo)
    if data is None:
        print(json.dumps({"status": "none", "message": "No rules documents found. Put them in .solsarch/rules/, docs/policies/ or policies/, or pass --rules PATH."}))
        return 1
    print(json.dumps({"status": "ok", "rules": len(active(data)), "not_enforced": len(data["not_enforced"]), "problems": data["problems"], "conflicts": data["conflicts"],
                      "constraints": data["constraints"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
