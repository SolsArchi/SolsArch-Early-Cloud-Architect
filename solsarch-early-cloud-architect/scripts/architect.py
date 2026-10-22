#!/usr/bin/env python3
"""The principal-architect review: what a senior architect checks after reading the code and the plan.

Standard library only, no model. Everything here is a fixed rule over the scan, the plan and the user's
profile, so the same inputs give the same review whichever AI agent runs the skill. It produces:

  archetype      microservice, monolith, serverless, static, data or greenfield, with the evidence
  units          the separately deployable parts of the system (each gets its own service, health check, port)
  rules          DR-01 to DR-13: design rules that are met, bent (with the accepted risk), violated or not applicable
  capacity       a labelled traffic estimate and the sizing it implies (Little's Law, burst factor, vCPU per user)
  profile        the user's optional answers (traffic tier, account type, compliance, region, high availability)

Usage: python architect.py --repo REPO      print the review for a scanned and planned repository
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from structure_scanner import normalise_path, service_of  # noqa: E402

PROFILE_FILE = "profile.json"
TRAFFIC_TIERS = ("prototype", "growth", "enterprise")
ACCOUNT_TYPES = ("standard", "free_trial", "free_tier", "pay_as_you_go")
LEAN_ACCOUNTS = ("free_trial", "free_tier")
HA_MODES = ("multi_az", "single_az")
PROFILE_DEFAULTS = {"traffic_tier": "prototype", "account_type": "standard", "high_availability": "multi_az",
                    "compliance": [], "region": None, "monthly_budget": None, "risk_appetite": "balanced",
                    "business_driver": None, "existing_cicd": None, "rto_minutes": None, "rpo_minutes": None,
                    "daily_requests": None, "peak_concurrent_users": None}
COMPLIANCE_FRAMEWORKS = ("soc2", "hipaa", "pci-dss", "gdpr", "iso27001", "nist-csf", "fedramp", "eu-ai-act")
RISK_APPETITES = ("conservative", "balanced", "aggressive")

# Estimated users and requests per day by tier. These are labelled assumptions, never measurements.
TIER_TRAFFIC = {"prototype": {"dau": 1000, "requests_per_user": 40, "label": "Prototype (under 1,000 daily users)"},
                "growth": {"dau": 50000, "requests_per_user": 40, "label": "Growth (10,000 to 100,000 daily users)"},
                "enterprise": {"dau": 1000000, "requests_per_user": 40, "label": "Enterprise (over 1,000,000 daily users)"}}
BACKEND = ("FastAPI", "Django", "Flask", "Express", "NestJS", "Ruby on Rails", "Spring Boot", "Laravel", "Gin", "Fastify",
           "Sinatra", "Axum", "Actix Web")
QUEUES = ("Celery", "BullMQ", "RQ", "Dramatiq", "Sidekiq")
DEFAULT_PORT = {"FastAPI": 8000, "Django": 8000, "Flask": 5000, "Express": 3000, "NestJS": 3000, "Next.js": 3000,
                "Ruby on Rails": 3000, "Spring Boot": 8080, "Laravel": 8000, "Gin": 8080, "Fastify": 3000,
                "Sinatra": 4567, "Axum": 3000, "Actix Web": 8080}
HEALTH_RE = re.compile(r"^/(?:api/)?(?:v\d+/)?(?:health|healthz|healthcheck|health-check|ready|readyz|live|livez|ping|status)/?$", re.I)
SANDBOX_LIBS = ("e2b", "e2b-code-interpreter", "pyodide", "jupyter-client", "jupyter_client", "ipykernel", "RestrictedPython",
                "docker", "dockerode", "vm2", "isolated-vm", "judge0", "piston", "code-interpreter", "langchain-experimental")
SERVERLESS_FILES = ("serverless.yml", "serverless.yaml", "template.yaml", "template.yml", "sam.yaml", "wrangler.toml",
                    "netlify.toml", "vercel.json", "firebase.json", "host.json", "function.json")
EMBEDDED_STATE = ("SQLite", "LMDB", "ChromaDB", "FAISS", "LanceDB", "Meilisearch")
NOSQL_NATIVE = re.compile(r"firestore|datastore|dynamodb|bigtable|cosmos db for nosql|keyspaces|workers kv", re.I)
MONGO_COMPATIBLE = re.compile(r"atlas|mongodb|documentdb|cosmos db for mongodb|mongo", re.I)
SQL_FAMILY = {"PostgreSQL": "postgresql", "MySQL": "mysql"}
STATUSES = ("met", "bent", "violated", "not_applicable")


# ------------------------------------------------------------------ profile
def profile_path(repo):
    return Path(repo) / ".solsarch" / PROFILE_FILE


def load_profile(repo):
    profile = dict(PROFILE_DEFAULTS)
    try:
        saved = json.loads(profile_path(repo).read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            profile.update({k: v for k, v in saved.items() if k in PROFILE_DEFAULTS})
    except (OSError, ValueError):
        pass
    return profile


def validate_profile_value(key, value):
    """Return the cleaned value, or raise ValueError with a message the user can act on."""
    if key not in PROFILE_DEFAULTS:
        raise ValueError("unknown profile field '%s'; fields: %s" % (key, ", ".join(sorted(PROFILE_DEFAULTS))))
    text = str(value).strip()
    if text.lower() in ("", "none", "null", "unset"):
        return [] if key == "compliance" else None
    if key == "traffic_tier":
        if text.lower() not in TRAFFIC_TIERS:
            raise ValueError("traffic_tier must be one of: %s" % ", ".join(TRAFFIC_TIERS))
        return text.lower()
    if key == "account_type":
        if text.lower() not in ACCOUNT_TYPES:
            raise ValueError("account_type must be one of: %s" % ", ".join(ACCOUNT_TYPES))
        return text.lower()
    if key == "high_availability":
        if text.lower() not in HA_MODES:
            raise ValueError("high_availability must be one of: %s" % ", ".join(HA_MODES))
        return text.lower()
    if key == "risk_appetite":
        if text.lower() not in RISK_APPETITES:
            raise ValueError("risk_appetite must be one of: %s" % ", ".join(RISK_APPETITES))
        return text.lower()
    if key == "compliance":
        items = [x.strip().lower() for x in text.split(",") if x.strip()]
        bad = [x for x in items if x not in COMPLIANCE_FRAMEWORKS]
        if bad:
            raise ValueError("unknown compliance framework: %s; choose from: %s" % (", ".join(bad), ", ".join(COMPLIANCE_FRAMEWORKS)))
        return sorted(set(items))
    if key in ("monthly_budget", "rto_minutes", "rpo_minutes", "daily_requests", "peak_concurrent_users"):
        try:
            number = float(text)
        except ValueError:
            raise ValueError("%s must be a number" % key)
        if number < 0:
            raise ValueError("%s cannot be negative" % key)
        return int(number) if number == int(number) else number
    if key == "region" and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", text.lower()):
        raise ValueError("region looks invalid: '%s'" % text)
    return text.lower() if key == "region" else text


def save_profile(repo, profile):
    target = profile_path(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((json.dumps({k: profile[k] for k in sorted(profile)}, indent=2) + chr(10)).encode("utf-8"))
    return target


def lean(profile):
    return profile.get("account_type") in LEAN_ACCOUNTS


def single_az(profile):
    return lean(profile) or profile.get("high_availability") == "single_az"


# ------------------------------------------------------------------ units
def _compose_services(root):
    """[{name, image, build}] from compose files in the repository, by simple line reading."""
    found = []
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file() or not re.match(r"^(?:docker-)?compose(?:\.[\w-]+)*\.ya?ml$", path.name, re.I):
            continue
        if any(part in ("node_modules", ".git", "tests", "test", "fixtures") for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        in_services, current = False, None
        for line in lines:
            if re.match(r"^services:\s*$", line):
                in_services = True
                continue
            if in_services and re.match(r"^\S", line):
                in_services = False
            if not in_services:
                continue
            m = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
            if m:
                current = {"name": m.group(1), "image": None, "build": None, "file": path.relative_to(root).as_posix()}
                found.append(current)
                continue
            if current:
                m = re.match(r"^\s{4}image:\s*[\"']?([^\s\"'#]+)", line)
                if m:
                    current["image"] = m.group(1)
                m = re.match(r"^\s{4}build:\s*[\"']?([^\s\"'#]*)", line)
                if m:
                    current["build"] = m.group(1) or "."
    return found


def _dockerfile_for(folder, dockerfiles):
    for df in dockerfiles:
        parent = df["file"].rsplit("/", 1)[0] if "/" in df["file"] else "."
        if parent == folder:
            return df
    return None


ENTRY_PATTERNS = (("FastAPI", re.compile(r"^(\w+)\s*(?::\s*\w+\s*)?=\s*FastAPI\(", re.M)), ("Flask", re.compile(r"^(\w+)\s*=\s*Flask\(", re.M)),
                  ("Starlette", re.compile(r"^(\w+)\s*=\s*Starlette\(", re.M)), ("Sanic", re.compile(r"^(\w+)\s*=\s*Sanic\(", re.M)))
ENTRY_SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "tests", "test", "docs", "migrations", ".solsarch", "site-packages"}
ENTRY_PREFER = ("main", "app", "api", "server", "asgi", "wsgi", "run")


def find_entrypoint(root, folder, frameworks):
    """Where the application object is created, as a module path a process manager can start (for example src.api:app).
    Looked up in the files, never guessed: returns None when no file creates one."""
    base = Path(root) / folder if folder != "." else Path(root)
    wanted = [p for n, p in ENTRY_PATTERNS if n in frameworks] or [p for n, p in ENTRY_PATTERNS]
    if "Django" in frameworks:
        for path in sorted(base.rglob("wsgi.py"), key=lambda p: len(p.parts)):
            if not any(part in ENTRY_SKIP for part in path.relative_to(base).parts[:-1]):
                module = ".".join(path.relative_to(base).with_suffix("").parts)
                return {"kind": "wsgi", "target": "%s:application" % module, "file": path.relative_to(base).as_posix(), "line": 1, "verified": True}
    candidates = []
    if base.is_dir():
        for path in base.rglob("*.py"):
            rel = path.relative_to(base)
            if len(rel.parts) > 5 or any(part in ENTRY_SKIP for part in rel.parts[:-1]):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rx in wanted:
                m = rx.search(text)
                if m:
                    line = text.count("\n", 0, m.start()) + 1
                    rank = (len(rel.parts), 0 if path.stem in ENTRY_PREFER else 1, rel.as_posix())
                    candidates.append((rank, rel, m.group(1), line))
                    break
    if candidates:
        _, rel, var, line = sorted(candidates)[0]
        module = ".".join(rel.with_suffix("").parts)
        return {"kind": "asgi", "target": "%s:%s" % (module, var), "file": rel.as_posix(), "line": line, "verified": True}
    package = base / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except ValueError:
            scripts = {}
        if "start" in scripts:
            return {"kind": "npm", "target": "npm start", "file": "package.json", "line": 1, "verified": True}
    return None


def unit_flags(scan, folder):
    """Streaming, scheduler and environment variables that belong to the files under a unit's folder."""
    def inside(path):
        return folder == "." or path.startswith(folder + "/")
    evidence = scan.get("signal_evidence", {})
    streaming = any(inside(e["file"]) for e in evidence.get("streaming", []) if e.get("note") != "import only")
    scheduler = any(inside(e["file"]) for e in evidence.get("scheduler", []))
    names = [v["name"] for v in scan.get("env_vars", []) if any(inside(f) for f in v["files"])]
    return streaming, scheduler, names


def detect_units(root, scan, plan):
    """The separately deployable parts: one per service folder that has a framework, a worker library or a Dockerfile."""
    groups = {}
    for comp in scan.get("components", []):
        for ev in comp.get("evidence", []):
            if ev.get("test_file"):
                continue
            groups.setdefault(service_of(ev["file"]), {}).setdefault(comp["name"], []).append(ev)
    dockerfiles = scan.get("infrastructure", {}).get("dockerfiles", [])
    for df in dockerfiles:
        folder = df["file"].rsplit("/", 1)[0] if "/" in df["file"] else "."
        groups.setdefault(service_of(df["file"]) if "/" in df["file"] else ".", {})
    endpoints = (scan.get("structure") or {}).get("backend", {}).get("endpoints", [])
    data_keys = [d["key"] for d in plan["decisions"] if d["layer"] == "data"]
    languages = scan.get("languages", {})
    project = scan.get("root") or "app"
    units = []
    for folder in sorted(groups):
        names = groups[folder]
        frameworks = [n for n in names if n in BACKEND or n == "Next.js"]
        queue_libs = [n for n in names if n in QUEUES]
        spa = [n for n in ("React", "Vue") if n in names]
        df = _dockerfile_for(folder, dockerfiles) if folder != "." else next((d for d in dockerfiles if "/" not in d["file"]), None)
        if not (frameworks or queue_libs or spa or df):
            continue
        if "Next.js" in frameworks:
            kind = "web"
        elif any(f in BACKEND for f in frameworks):
            kind = "api"
        elif queue_libs:
            kind = "worker"
        elif spa and not df:
            kind = "static"
        else:
            kind = "service"
        base = project if folder == "." else folder.split("/")[-1]
        port = None
        if df and df.get("exposed_ports"):
            port = int(re.match(r"\d+", str(df["exposed_ports"][0])).group(0)) if re.match(r"\d+", str(df["exposed_ports"][0])) else None
        if port is None and frameworks:
            port = DEFAULT_PORT.get(frameworks[0])
        health, source = "/", "default"
        for ep in endpoints:
            if ep["method"] in ("GET", "ANY") and HEALTH_RE.match(normalise_path(ep["path"])) and \
                    (folder == "." or ep["service"] == folder or ep["file"].startswith(folder + "/")):
                health, source = ep["path"] if ep["path"].startswith("/") else "/" + ep["path"], "detected"
                break
        evidence = []
        for lst in names.values():
            for ev in lst[:1]:
                text = "%s:%s" % (ev["file"], ev["line"])
                if text not in evidence:
                    evidence.append(text)
        langs = sorted({lang for lang in languages if any(ev["file"].endswith(ext) for lst in names.values() for ev in lst
                                                          for ext in {"python": (".py", "requirements.txt"), "javascript": (".js", "package.json"),
                                                                      "typescript": (".ts", "package.json")}.get(lang, ()))})
        units.append({
            "name": re.sub(r"[^a-z0-9-]+", "-", base.lower()).strip("-") or "app", "folder": folder, "kind": kind,
            "frameworks": frameworks + queue_libs + spa, "languages": langs, "port": port if kind != "worker" else None,
            "health_path": health if kind in ("api", "web", "service") else None, "health_source": source if kind in ("api", "web", "service") else None,
            "dockerfile": df["file"] if df else None, "runs_as_root": bool(df and df.get("runs_as_root")),
            "uses": data_keys if kind in ("api", "worker", "service") else [],
            "evidence": evidence[:4],
        })
        entry = find_entrypoint(root, folder, frameworks) if kind in ("api", "service", "worker", "web") else None
        streaming_flag, scheduler_flag, env_names = unit_flags(scan, folder)
        units[-1].update({"entrypoint": entry, "streaming": streaming_flag, "scheduler": scheduler_flag, "env_vars": env_names})
    loose = [v["name"] for v in scan.get("env_vars", []) if not any(v["name"] in u["env_vars"] for u in units)]
    for unit in units:
        if unit["kind"] in ("api", "service", "worker") and loose:
            unit["env_vars"] = sorted(set(unit["env_vars"]) | set(loose))
    seen, unique = {}, []
    for unit in units:
        n = seen.get(unit["name"], 0)
        seen[unit["name"]] = n + 1
        if n:
            unit["name"] = "%s-%d" % (unit["name"], n + 1)
        unique.append(unit)
    return unique, _compose_services(root)


# ------------------------------------------------------------------ archetype
def detect_archetype(root, scan, units, compose):
    reasons = []
    files = {p.name.lower() for p in Path(root).rglob("*") if p.is_file() and len(p.parts) - len(Path(root).parts) <= 3
             and not any(part in ("node_modules", ".git", ".solsarch") for part in p.parts)}
    serverless = sorted(f for f in SERVERLESS_FILES if f in files)
    deployables = [u for u in units if u["kind"] in ("api", "web", "worker", "service")]
    components = scan.get("components", [])
    only_data_platform = components and all(c["category"] in ("database", "vector", "search") for c in components)
    if serverless:
        archetype = "serverless"
        reasons.append("serverless configuration found: %s" % ", ".join(serverless))
    elif len(deployables) >= 2 or len(compose) >= 3:
        archetype = "microservice"
        reasons.append("%d separately deployable units%s" % (len(deployables), "; compose defines %d services" % len(compose) if compose else ""))
    elif deployables:
        archetype = "monolith"
        reasons.append("one deployable unit: %s" % deployables[0]["name"])
    elif units and all(u["kind"] == "static" for u in units):
        archetype = "static"
        reasons.append("only a built front end was found")
    elif only_data_platform:
        archetype = "data"
        reasons.append("only data-store libraries were found")
    else:
        archetype = "greenfield"
        reasons.append("no deployable unit was found yet")
    if scan.get("stats", {}).get("files_scanned", 0) <= 12 and archetype in ("monolith", "greenfield"):
        reasons.append("a small repository, so the design stays minimal")
        if archetype == "monolith":
            archetype = "greenfield"
    return {"name": archetype, "reasons": reasons}


# ------------------------------------------------------------------ capacity
def capacity_model(scan, units, profile, archetype):
    """A labelled traffic estimate and the sizing it implies. Every figure is an assumption unless the user supplied it."""
    tier = profile.get("traffic_tier") or "prototype"
    base = TIER_TRAFFIC[tier]
    streaming = (scan.get("signals") or {}).get("streaming", "none") == "used"
    has_ai = any(c["category"] == "ai" for c in scan.get("components", []))
    given_daily = profile.get("daily_requests")
    daily = float(given_daily) if given_daily else float(base["dau"] * base["requests_per_user"])
    burst = 15.0 if (streaming or has_ai) else 8.0
    avg_complexity = 2.0
    avg_latency = avg_complexity * (0.55 if streaming else 0.35)
    peak_rps = daily / 86400.0 * burst
    peak_concurrent = int(math.ceil(peak_rps * avg_latency))
    given_concurrent = profile.get("peak_concurrent_users")
    if given_concurrent:
        peak_concurrent = int(given_concurrent)
    response_bytes = 40000 if (streaming or has_ai) else 5000
    vcpu_needed = min(max(peak_concurrent * 0.05, 0.25), 64.0)
    unit_vcpu = 1.0 if vcpu_needed > 1 else 0.5
    minimum = 1 if single_az(profile) else 2
    app_units = [u for u in units if u["kind"] in ("api", "web", "worker", "service")] or [{"name": "app"}]
    replicas = max(minimum, int(math.ceil(vcpu_needed / unit_vcpu / max(1, len(app_units)))))
    replicas = min(replicas, 20)
    db_rps = peak_rps * 1.5
    if db_rps < 50:
        db_class = "small"
    elif db_rps < 500:
        db_class = "medium"
    else:
        db_class = "large"
    return {
        "source": "user-provided" if (given_daily or given_concurrent) else "assumption",
        "tier": tier, "tier_label": base["label"], "confidence": "high" if given_daily else "low",
        "note": ("Figures come from the traffic you provided." if given_daily else
                 "[ESTIMATE] No traffic data was provided. These numbers are planning assumptions for the %s tier, not measurements." % tier),
        "daily_requests": int(daily), "burst_factor": burst, "avg_latency_seconds": round(avg_latency, 2),
        "peak_rps": round(peak_rps, 2), "peak_concurrent": peak_concurrent, "db_rps": round(db_rps, 2),
        "bandwidth_gb_per_day": round(daily * response_bytes / 1073741824.0, 3),
        "sizing": {"unit_vcpu": unit_vcpu, "unit_memory_gb": unit_vcpu * 2, "replicas_per_unit": replicas, "units": len(app_units),
                   "db_class": db_class, "cache_gb": 1 if peak_rps < 100 else 4, "storage_gb": 50 if tier == "prototype" else 500},
        "archetype": archetype,
    }


# ------------------------------------------------------------------ rules
def _rule(rule_id, name, status, detail, action="", evidence=()):
    return {"id": rule_id, "name": name, "status": status, "detail": detail, "action": action, "evidence": list(evidence)}


def design_rules(scan, plan, units, compose, profile, archetype):
    by = {d["key"]: d for d in plan["decisions"]}
    names = {c["name"] for c in scan.get("components", [])}
    rules = []
    paas = plan.get("provider_kind") in ("paas", "edge")
    has_compute = any(k.startswith("compute_") for k in by) or "static_site" in by

    # DR-01 ingress
    if not has_compute:
        rules.append(_rule("DR-01", "Public traffic reaches compute through a managed front door", "not_applicable", "There is no application compute in this plan."))
    elif "edge" in by and "ingress" not in by:
        rules.append(_rule("DR-01", "Public traffic reaches compute through a managed front door", "violated",
                           "A CDN or edge service is planned with no load balancer or built-in ingress behind it.",
                           "Add a load balancer between the edge service and private compute, or use the vendor's private-origin feature."))
    elif "ingress" in by:
        built = by["ingress"].get("builtin")
        rules.append(_rule("DR-01", "Public traffic reaches compute through a managed front door", "met",
                           "%s receives public traffic%s." % (by["ingress"]["service"], " (built into the platform)" if built else ""),
                           "", [by["ingress"]["service"]]))
    else:
        rules.append(_rule("DR-01", "Public traffic reaches compute through a managed front door", "bent",
                           "%s has no catalogued load balancer." % plan["cloud_name"], "Put a managed load balancer or the platform router in front of compute."))

    # DR-02 egress
    external = plan.get("external_services", [])
    if not external:
        rules.append(_rule("DR-02", "Private compute has an explicit outbound path", "not_applicable", "The scan found no third-party API calls that need one."))
    elif "egress" in by:
        rules.append(_rule("DR-02", "Private compute has an explicit outbound path", "met",
                           "Calls to %s leave through %s." % (", ".join(x["name"] for x in external), by["egress"]["service"]), "",
                           [by["egress"]["service"]]))
    elif paas:
        rules.append(_rule("DR-02", "Private compute has an explicit outbound path", "met",
                           "The platform's shared outbound addresses are used; compute is not in a private network."))
    else:
        rules.append(_rule("DR-02", "Private compute has an explicit outbound path", "bent",
                           "%s has no catalogued NAT service." % plan["cloud_name"],
                           "Give private compute a NAT or gateway for calls to %s." % ", ".join(x["name"] for x in external)))

    # DR-03 decompose
    app_units = [u for u in units if u["kind"] in ("api", "web", "worker", "service")]
    if len(app_units) + (1 if any(u["kind"] == "static" for u in units) else 0) <= 1 and len(compose) < 3:
        rules.append(_rule("DR-03", "Separately scaling parts are separate deployable units", "not_applicable",
                           "One deployable unit was found (%s)." % archetype["name"]))
    else:
        compute_keys = [k for k in by if k.startswith("compute_")]
        status = "met" if len(app_units) >= 2 else "bent"
        rules.append(_rule("DR-03", "Separately scaling parts are separate deployable units", status,
                           "%d units are planned as separate services: %s." % (len(app_units), ", ".join(u["name"] for u in app_units)),
                           "" if status == "met" else "Check docker-compose for services that should scale on their own signals.",
                           [u["folder"] for u in app_units][:4]))
        if compose and len(compose) > len(app_units) + len(plan["decisions"]):
            rules[-1]["detail"] += " Compose also defines %d other services (data stores and tools)." % (len(compose) - len(app_units))

    # DR-04 sandbox
    sandbox = sorted(n for n in names if n in SANDBOX_LIBS) or sorted(
        c["name"] for c in scan.get("components", []) if c["name"].lower() in {x.lower() for x in SANDBOX_LIBS})
    if sandbox:
        rules.append(_rule("DR-04", "Untrusted code runs in an isolated sandbox", "violated",
                           "%s suggests the application runs user-supplied code, which needs isolation." % ", ".join(sandbox),
                           "Run code execution on hardened isolation (gVisor or microVM instances, or the vendor's sandboxed code service) in a separate subnet with no route to data stores."))
    else:
        rules.append(_rule("DR-04", "Untrusted code runs in an isolated sandbox", "not_applicable", "No code-execution library was found."))

    # DR-05 state
    embedded = sorted(n for n in names if n in EMBEDDED_STATE)
    if not embedded:
        rules.append(_rule("DR-05", "Stateless compute keeps state in managed services", "not_applicable", "No embedded database or vector store was found."))
    else:
        replaced = [d for d in plan["decisions"] if d.get("replaces")]
        moved = [d["short"] for d in replaced]
        if moved or "vector" in by:
            rules.append(_rule("DR-05", "Stateless compute keeps state in managed services", "met",
                               "%s moves to %s." % (", ".join(embedded), ", ".join(moved) or by["vector"]["short"]), "", embedded))
        else:
            rules.append(_rule("DR-05", "Stateless compute keeps state in managed services", "violated",
                               "%s keeps state on the container's disk, which is lost when containers are replaced." % ", ".join(embedded),
                               "Move the state to a managed database or object storage, or use compute with persistent block storage."))

    # DR-06 vectors
    if "vector" in by:
        embedded_vec = by["vector"]["short"] == "pgvector"
        rules.append(_rule("DR-06", "The vector store is an explicit decision", "met",
                           ("Embeddings use pgvector inside %s, so there is no extra system." % by.get("database", {}).get("short", "the database"))
                           if embedded_vec else "%s is the vector store." % by["vector"]["service"], "", [by["vector"]["service"]]))
    else:
        rules.append(_rule("DR-06", "The vector store is an explicit decision", "not_applicable", "No vector search was found."))

    # DR-07 streaming
    streaming = (scan.get("signals") or {}).get("streaming", "none")
    if streaming == "none":
        rules.append(_rule("DR-07", "Streaming survives every hop", "not_applicable", "No streaming or WebSocket code was found."))
    else:
        rules.append(_rule("DR-07", "Streaming survives every hop", "bent",
                           "Streaming code was %s. Every hop needs response buffering off, an idle timeout of at least 300 seconds and WebSocket upgrade support." %
                           ("used in a route" if streaming == "used" else "imported"),
                           "The generated infrastructure code sets these values on the load balancer and proxy; confirm them on any CDN in front."))

    # DR-08 secrets and identity
    secret_findings = [f for f in scan.get("findings", []) if f["id"] == "SEC-005" and any(not e.get("test_file") for e in f["evidence"])]
    if "secrets" in by and not secret_findings:
        rules.append(_rule("DR-08", "Credentials live in a secrets manager, not in code", "met",
                           "%s holds credentials; the scan found none in code." % by["secrets"]["service"], "", [by["secrets"]["service"]]))
    elif secret_findings:
        rules.append(_rule("DR-08", "Credentials live in a secrets manager, not in code", "violated",
                           "The scan found credential-like values in code (%d location(s))." % len(secret_findings[0]["evidence"]),
                           "Rotate them, then move them to %s." % (by["secrets"]["service"] if "secrets" in by else "a secrets manager"),
                           ["%s:%s" % (e["file"], e["line"]) for e in secret_findings[0]["evidence"][:3]]))
    else:
        rules.append(_rule("DR-08", "Credentials live in a secrets manager, not in code", "bent",
                           "%s has no catalogued secrets manager." % plan["cloud_name"], "Use environment variables set by the platform, and rotate them on a schedule."))

    # DR-09 data driver compatibility
    mongo_driver = "MongoDB" in names
    sql_driver = [SQL_FAMILY[n] for n in names if n in SQL_FAMILY]
    db_choice = by.get("database_mongo")
    compat_notes = []
    status, detail, action = "not_applicable", "No database driver that constrains the data service was found.", ""
    if mongo_driver:
        if db_choice and NOSQL_NATIVE.search(db_choice["service"]) and not MONGO_COMPATIBLE.search(db_choice["service"]):
            status, detail = "violated", "%s is not MongoDB-compatible, but the code uses a MongoDB driver." % db_choice["service"]
            action = "Use a MongoDB-compatible service (MongoDB Atlas, Amazon DocumentDB or Cosmos DB for MongoDB), or budget a rewrite of the data layer."
        elif db_choice:
            status, detail = "met", "%s speaks the MongoDB wire protocol, so the existing driver keeps working." % db_choice["service"]
        else:
            status, detail = "bent", "The code uses a MongoDB driver but this provider has no MongoDB-compatible service in the catalogue."
            action = "Use MongoDB Atlas next to this provider (--also mongodbatlas)."
    elif sql_driver:
        sql = by.get("database") or by.get("database_mysql")
        if sql and NOSQL_NATIVE.search(sql["service"]):
            status, detail = "violated", "%s cannot run code that uses a %s driver." % (sql["service"], sql_driver[0])
        elif sql:
            status, detail = "met", "%s is a relational service, matching the %s driver." % (sql["service"], sql_driver[0])
        else:
            status, detail = "bent", "The code uses a %s driver but this provider has no managed relational service in the catalogue." % sql_driver[0]
            action = "Add a managed PostgreSQL next to this provider (--also neon or supabase)."
    rules.append(_rule("DR-09", "The data service matches the application's database driver", status, detail, action))

    # DR-10 declared but unused
    unused = []
    if "python" in scan.get("languages", {}):
        for comp in scan.get("components", []):
            real = [e for e in comp.get("evidence", []) if not e.get("test_file")]
            if real and all(e.get("via") == "manifest" for e in real) and comp["category"] in ("cache", "queue", "search", "vector"):
                unused.append(comp["name"])
    if unused:
        rules.append(_rule("DR-10", "Only technologies the code uses are provisioned", "bent",
                           "%s is declared in a manifest but no import of it was found in the Python code." % ", ".join(sorted(unused)),
                           "Confirm the code uses it before provisioning; remove the dependency if not."))
    else:
        rules.append(_rule("DR-10", "Only technologies the code uses are provisioned", "met", "Every provisioned technology is imported or configured in the code."))

    # DR-11 lean design for a free-trial account
    if lean(profile):
        deferred = [k for k in ("edge", "identity") if k in by]
        rules.append(_rule("DR-11", "A free-trial account gets the smallest correct design", "bent",
                           "Lean mode is on: one availability zone, one outbound path and no cache, queue or search the code does not use.",
                           "Add %s and zone redundancy before production." % (", ".join(by[k]["short"] for k in deferred) or "a CDN and zone redundancy")))
    else:
        rules.append(_rule("DR-11", "A free-trial account gets the smallest correct design", "not_applicable", "The account type is standard."))
    return rules


def eol_rule(root):
    """DR-12: no runtime, framework or base image the code names is past end of life."""
    import eol_check
    found = eol_check.check(root)
    dead, soon = found["end_of_life"], found["ending_soon"]
    name = "Runtimes and base images are still supported"
    if dead:
        detail = "; ".join("%s %s (%s:%d, ended %s)" % (r["name"], r["version"], r["file"], r["line"], r["eol"]) for r in dead[:6])
        return _rule("DR-12", name, "violated", detail + ".", "Move to a supported release (latest: %s)." % ", ".join(sorted({"%s %s" % (r["name"], r["latest"]) for r in dead if r.get("latest")})[:4]),
                     ["%s:%d" % (r["file"], r["line"]) for r in dead])
    if soon:
        detail = "; ".join("%s %s ends %s (%s:%d)" % (r["name"], r["version"], r["eol"], r["file"], r["line"]) for r in soon[:6])
        return _rule("DR-12", name, "bent", detail + ".", "Plan the upgrade before the date.", ["%s:%d" % (r["file"], r["line"]) for r in soon])
    if not found["findings"]:
        return _rule("DR-12", name, "not_applicable", "No pinned runtime or image version was found in the repository.")
    return _rule("DR-12", name, "met", "%d pinned version(s) checked against end-of-life dates fetched %s." % (len(found["findings"]), found["fetched"]))


FREE_TIER_TRAPS = (("database_mongo", "DocumentDB", "DocumentDB nodes are billed by the hour"),
                   ("cache", "ElastiCache", "ElastiCache nodes are billed by the hour"),
                   ("egress", "NAT", "A NAT gateway costs about $32 a month before any traffic"),
                   ("secrets", "Secrets Manager", "Secrets Manager charges for every secret each month"),
                   ("ingress", "API Gateway", "API Gateway charges per request and cuts streaming responses"))


def free_tier_rule(plan, profile):
    """DR-13: an always-free account must not be given services that bill by the hour or the month."""
    name = "An always-free account only gets services that have a free allowance"
    if (profile or {}).get("account_type") != "free_tier":
        return _rule("DR-13", name, "not_applicable", "The account type is not always-free.")
    own = [d for d in plan["decisions"] if d.get("provider", plan["cloud"]) == plan["cloud"]]
    hits = [msg + " (" + d["service"] + ")" for key, word, msg in FREE_TIER_TRAPS for d in own
            if d["key"] == key and word.lower() in d["service"].lower()] if plan["cloud"] == "aws" else []
    if hits:
        return _rule("DR-13", name, "violated", "; ".join(hits) + ".", "Use the free alternative the plan lists, or choose a paid account type.")
    return _rule("DR-13", name, "met", "No service in the plan bills by the hour or the month without a free allowance.")


def review(root, scan, plan, profile=None):
    profile = profile or dict(PROFILE_DEFAULTS)
    units, compose = detect_units(root, scan, plan)
    archetype = detect_archetype(root, scan, units, compose)
    rules = design_rules(scan, plan, units, compose, profile, archetype)
    rules.append(eol_rule(root))
    rules.append(free_tier_rule(plan, profile))
    counts = {s: sum(1 for r in rules if r["status"] == s) for s in STATUSES}
    return {"archetype": archetype, "units": units, "compose_services": compose, "rules": rules, "rule_counts": counts,
            "capacity": capacity_model(scan, units, profile, archetype["name"]), "profile": profile,
            "lean": lean(profile), "single_az": single_az(profile)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Principal-architect review of a scanned and planned repository")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    try:
        scan = json.loads((repo / ".solsarch" / "scan.json").read_text(encoding="utf-8"))
        plan = json.loads((repo / ".solsarch" / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.stderr.write("error: run the scan and plan steps first" + chr(10))
        return 2
    sys.stdout.write(json.dumps(review(repo, scan, plan, load_profile(repo)), indent=2) + chr(10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
