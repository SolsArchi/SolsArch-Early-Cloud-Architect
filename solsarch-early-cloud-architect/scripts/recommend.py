#!/usr/bin/env python3
"""Turn a scan plus two user choices into a cloud plan.

Inputs: the scan from ast_scanner.py, the provider (any hosting provider in the registry, for example
aws, azure, gcp, hetzner, vercel or supabase), optional overlay providers (--also neon,cloudflare) and the
compute model (server or serverless). Output: plan.json with one decision per service, each carrying
the evidence from the code, a plain-language reason, the alternatives not chosen, and a
fixed web-verification query. The same inputs always give byte-identical output, so the
plan does not depend on which AI model runs the skill.

Standard library only. Usage:
    python recommend.py --scan scan.json --cloud aws --compute server [--output plan.json]
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import providers  # noqa: E402

CATALOG_PATH = HERE.parent / "references" / "service-catalog.json"

BACKEND_FRAMEWORKS = ("FastAPI", "Django", "Flask", "Express", "NestJS", "Ruby on Rails",
                      "Spring Boot", "Laravel", "Gin", "Fastify", "Sinatra", "Axum", "Actix Web")
QUEUE_LIBS = ("Celery", "BullMQ", "RQ", "Dramatiq", "Sidekiq")
AUTH_LIBS = ("Passport", "NextAuth", "Authlib", "JWT library")
EXTERNAL_AI = {"OpenAI SDK": "OpenAI API", "Anthropic SDK": "Anthropic API", "Groq SDK": "Groq API"}
CLOUD_KINDS_WITHOUT_FREE_CACHE = ("cloud",)
# The region each generator uses when the profile names none. The plan states it as an assumption instead of leaving it open.
REGION_DEFAULTS = {"aws": "us-east-1", "azure": "eastus", "gcp": "us-central1", "alibaba": "cn-hangzhou", "huawei": "ap-southeast-3",
                   "ibm": "us-south", "oci": "us-ashburn-1", "tencent": "ap-singapore", "digitalocean": "nyc3", "hetzner": "fsn1",
                   "linode": "us-east", "akamai": "us-east", "vultr": "ewr", "scaleway": "fr-par", "ovhcloud": "GRA11",
                   "civo": "NYC1", "upcloud": "de-fra1", "exoscale": "ch-gva-2"}
# Clouds whose generated code puts always-on containers in private subnets behind a NAT gateway, and clouds whose code always builds one.
PRIVATE_SERVER_CLOUDS = ("aws", "azure", "gcp", "alibaba", "huawei", "oci", "tencent")
NAT_ALWAYS_CLOUDS = ("alibaba", "huawei", "oci", "tencent")
EMBEDDED_VECTOR = ("ChromaDB", "FAISS", "LanceDB", "Qdrant", "Weaviate")
SEARCH_ENGINES = ("Meilisearch", "Elasticsearch", "OpenSearch")
SELF_HOSTED_AI = ("vLLM", "Ollama")
VERIFY_ORDER = ("compute_web", "compute_api", "compute_app", "compute_worker", "database", "database_mysql",
                "database_mongo", "cache", "ingress", "edge", "secrets", "vector", "search", "queue_kafka",
                "queue_rabbitmq", "storage", "static_site", "identity", "registry", "network", "egress",
                "gpu", "observability")
COST_DRIVERS = {
    "compute": {"server": "hours the containers run, plus CPU and memory size",
                "serverless": "number of requests and how long each one runs"},
    "ingress": "hours running and the amount of traffic processed",
    "edge": "number of requests and the amount of data served",
    "network": "mostly free; costs come from the services inside it",
    "egress": "an hourly charge plus every gigabyte sent out, often the largest surprise",
    "database": "instance or capacity size, stored data and backups",
    "cache": "memory size and number of nodes",
    "queue": "throughput and retained data",
    "vector": "extra database storage and memory for embeddings",
    "search": "node size and stored data",
    "storage": "stored data and request count",
    "static_site": "stored data and traffic served",
    "identity": "number of monthly active users",
    "secrets": "number of secrets and the calls that read them",
    "registry": "stored image size",
    "observability": "log volume and retention, which grows quickly if debug logging stays on",
    "gpu": "GPU hours, which dominate the bill if the machine runs all day",
}


def load_catalog(path=CATALOG_PATH):
    """The base catalogue merged with every provider in references/providers."""
    base = json.loads(Path(path).read_text(encoding="utf-8"))
    return providers.merged_catalog(base) if Path(path) == CATALOG_PATH else base


class Scan:
    """Read-only view of the scan with helpers for evidence."""

    def __init__(self, data):
        self.data = data
        self.by_name = {}
        for component in data.get("components", []):
            real = [e for e in component.get("evidence", []) if not e.get("test_file")]
            if real:  # a library used only in test files does not shape the deployment
                self.by_name.setdefault(component["name"], []).extend(real)

    def has(self, *names):
        return any(name in self.by_name for name in names)

    def evidence(self, *names, limit=3):
        items = []
        for name in names:
            items.extend(self.by_name.get(name, []))
        return items[:limit]


def loc(item):
    return "%s:%s" % (item["file"], item["line"])


def file_evidence(items):
    return [{"kind": "file", "file": i["file"], "line": i["line"], "text": loc(i)} for i in items]


def rule_evidence(text):
    return [{"kind": "rule", "text": text}]


def sentence_found(scan, names):
    parts = []
    for name in names:
        items = scan.evidence(name, limit=1)
        if items:
            verb = {"manifest": "declared", "import": "imported", "image": "run as an image"}.get(
                items[0].get("via"), "referenced")
            parts.append("%s (%s at `%s`)" % (name, verb, loc(items[0])))
    return "The scan found " + " and ".join(parts) + "." if parts else ""


def hosting_providers(catalog):
    return [pid for pid, info in catalog["clouds"].items() if info.get("kind", "cloud") != "enterprise"]


def build_plan(scan_data, catalog, cloud, mode, also=(), profile=None):
    if cloud not in catalog["clouds"]:
        raise ValueError("cloud must be one of: " + ", ".join(hosting_providers(catalog)))
    registry = catalog.get("providers", {})
    if cloud in registry and registry[cloud]["kind"] == "enterprise":
        raise ValueError("%s is an enterprise platform that SolsArch reads for structure only; choose a hosting provider"
                         % registry[cloud]["name"])
    if mode not in catalog["modes"]:
        raise ValueError("compute model must be one of: " + ", ".join(catalog["modes"]))
    overlays = []
    for pid in also or ():
        if pid == cloud or pid in overlays:
            continue
        if pid not in registry or registry[pid]["kind"] == "enterprise" or not registry[pid].get("overlay"):
            raise ValueError("%s cannot be combined with another provider; choose one of: %s"
                             % (pid, ", ".join(sorted(k for k, v in registry.items() if v["kind"] != "enterprise" and v.get("overlay")))))
        overlays.append(pid)
    scan = Scan(scan_data)
    profile = profile or {}
    lean = profile.get("account_type") in ("free_trial", "free_tier")
    always_free = profile.get("account_type") == "free_tier"
    auto_choices = []
    # An always-free account cannot pay for a database cluster or a cache node, so data goes to a provider that has a free plan.
    for needed, provider_id, label in (("MongoDB", "mongodbatlas", "MongoDB Atlas free (M0)"), ("PostgreSQL", "neon", "Neon free")):
        if (always_free and scan.has(needed) and provider_id not in overlays and provider_id != cloud
                and provider_id in registry and registry[provider_id].get("overlay")
                and registry.get(cloud, {}).get("kind", "cloud") == "cloud"):
            overlays.append(provider_id)
            auto_choices.append("%s was added because your account is always-free and %s has no free %s." % (label, catalog["clouds"][cloud]["name"], needed))
    cloud_info = catalog["clouds"][cloud]
    decisions, external, questions, gaps = [], [], [], []
    keys = set()
    provider_of = {}
    fallback_notes = {}

    def entry_for(need, provider):
        options = catalog["services"].get(need, {}).get(provider)
        if not options:
            return None, None
        if mode in options:
            return options[mode], None
        if "any" in options:
            note = None
            if need == "compute" and mode == "serverless":
                note = registry.get(provider, {}).get("gaps", {}).get("serverless")
            return options["any"], note
        other = options.get("server") or options.get("serverless")
        name = catalog["clouds"][provider]["name"]
        note = (registry.get(provider, {}).get("gaps", {}).get("serverless")
                or "%s has no %s compute in this catalogue, so its other compute service is used." % (name, mode))
        return other, note

    def pick(need):
        """The catalogue entry for a need, or None when no chosen provider offers it natively."""
        for pid in overlays:
            if need in registry[pid]["overlay"]:
                entry, note = entry_for(need, pid)
                if entry:
                    provider_of[need], fallback_notes[need] = pid, note
                    return entry
        entry, note = entry_for(need, cloud)
        if entry:
            provider_of[need], fallback_notes[need] = cloud, note
        return entry

    def gap_text(need, role):
        custom = registry.get(cloud, {}).get("gaps", {}).get(need)
        name = cloud_info["name"]
        return custom or "%s has no native service for this in the catalogue; use a partner service or run it yourself." % name

    def add(key, need, category, role, layer, evidence, intro, tech="", replaces=None, caveats=(),
            confidence="confirmed", service=None, short=None, why=None, alternatives=None, reason=None):
        entry = pick(need) if need else {}
        if need and entry is None:
            gaps.append({"key": key, "need": need, "role": role, "message": gap_text(need, role)})
            return False
        provider = provider_of.get(need, cloud) if need else cloud
        text = " ".join(part for part in (intro, why or entry.get("why", "")) if part)
        notes = [c for c in ([entry.get("caveat"), fallback_notes.get(need) if need else None] + list(caveats)) if c]
        decisions.append({
            "key": key, "category": category, "role": role, "layer": layer, "cloud": cloud, "provider": provider,
            "service": service or entry["service"], "short": short or entry["short"], "tech": tech,
            "evidence": evidence, "evidence_text": ", ".join(e["text"] for e in evidence) or "-",
            "why": text, "replaces": replaces,
            "alternatives": alternatives if alternatives is not None else entry.get("alternatives", []),
            "caveats": notes, "confidence": confidence, "need": need,
            "reason": reason or entry.get("why", ""),
            "builtin": bool(entry.get("builtin")), "partner": bool(entry.get("partner")),
            "selfmanaged": bool(entry.get("selfmanaged")),
        })
        keys.add(key)
        return True

    signals = scan_data.get("signals", {})
    streaming = signals.get("streaming", "none")
    scheduler = signals.get("scheduler", "none")

    def variant(lean_need, normal_need):
        """The cheaper catalogue entry when the account is free and this provider has one; otherwise the normal need."""
        if lean and catalog["services"].get(lean_need, {}).get(cloud):
            return lean_need
        return normal_need
    docker = scan_data.get("infrastructure", {}).get("dockerfiles", [])
    languages = scan_data.get("languages", {})

    # ---------------------------------------------------------------- compute
    backend = [n for n in BACKEND_FRAMEWORKS if scan.has(n)]
    has_next = scan.has("Next.js")
    spa = (scan.has("React") or scan.has("Vue")) and not has_next
    serverless_notes = []
    server_entry = catalog["services"].get("compute", {}).get(cloud, {}).get("server")
    if mode == "serverless" and cloud in ("aws", "azure") and (backend or has_next) and server_entry:
        switch = server_entry["service"]
        serverless_notes.append(
            "%s run on functions through an adapter layer, and large apps may need changes. If that is too much change, "
            "choose the server-based model, which uses %s." % (", ".join(backend + (["Next.js"] if has_next else [])), switch))
    if mode == "serverless" and streaming != "none":
        serverless_notes.append(
            "The scan found streaming code (%s). Serverless platforms limit streaming and run time, "
            "so test the streaming route before committing." % (
                "used in a route" if streaming == "used" else "imported but not yet used"))
    if mode == "serverless" and scheduler != "none":
        serverless_notes.append(
            "The scan found an in-process scheduler. A function is frozen between requests, so scheduled jobs would silently stop. "
            "The plan adds a managed schedule that calls the job instead.")
    if has_next:
        add("compute_web", "compute", "Compute", "Web front end (server-rendered)", "compute",
            file_evidence(scan.evidence("Next.js")), sentence_found(scan, ["Next.js"]), tech="Next.js",
            caveats=serverless_notes)
    if backend:
        add("compute_api", "compute", "Compute", "API", "compute",
            file_evidence(scan.evidence(*backend)), sentence_found(scan, backend[:2]), tech=", ".join(backend),
            caveats=serverless_notes)
    elif not has_next and not spa and (docker or languages):
        evidence = [{"kind": "file", "file": docker[0]["file"], "line": 1, "text": docker[0]["file"] + ":1"}] \
            if docker else rule_evidence("Source files in " + ", ".join(sorted(languages)))
        add("compute_app", "compute", "Compute", "Application", "compute", evidence,
            "The repository has a Dockerfile." if docker else "The repository has application code but no known web framework.",
            tech=", ".join(sorted(languages)), caveats=serverless_notes, confidence="inferred")
    has_compute = any(k.startswith("compute_") for k in keys)
    if spa:
        placed = add("static_site", "static_site", "Static hosting", "Front end (built static files)", "compute",
                     file_evidence(scan.evidence("React", "Vue")), sentence_found(scan, [n for n in ("React", "Vue") if scan.has(n)]),
                     tech="React or Vue", confidence="inferred")
        if not placed and not has_compute and cloud_info.get("kind", "cloud") == "cloud":
            # no static hosting here: the files are served by a small web server container, which is a compute decision of its own
            if add("compute_app", "compute", "Compute", "Static site server", "compute",
                   file_evidence(scan.evidence("React", "Vue")), "This cloud has no static website hosting, so a small nginx container serves the built files.",
                   tech="nginx", confidence="inferred"):
                has_compute = True
            questions.append("%s has no static website hosting, so this single-page app would be served by a small container on the infrastructure written "
                             "for it. A static host costs far less: add one with --also cloudflare, or choose Netlify, Vercel or Cloudflare as the platform."
                             % cloud_info["name"])
    workers = [n for n in QUEUE_LIBS if scan.has(n)]
    if workers and has_compute:
        add("compute_worker", "compute", "Compute", "Background worker", "compute",
            file_evidence(scan.evidence(*workers)), sentence_found(scan, workers[:2]) + " Jobs run outside the web request.",
            tech=", ".join(workers),
            caveats=(["Workers become queue-triggered functions, which needs a small handler change in the worker code."]
                     if mode == "serverless" else []))

    # ---------------------------------------------------------------- data stores
    pg = scan.has("PostgreSQL")
    sqlite = scan.has("SQLite")
    mysql = scan.has("MySQL")
    mongo = scan.has("MongoDB")
    if pg or (sqlite and not mysql):
        evidence = file_evidence(scan.evidence("PostgreSQL", "SQLite"))
        if pg:
            intro = sentence_found(scan, ["PostgreSQL"])
        else:
            intro = sentence_found(scan, ["SQLite"]) + (
                " SQLite keeps data in a file on the container's disk, which is lost or splits apart when containers"
                " are replaced or run in more than one copy (SEC-003).")
        replaces = ("SQLite file database at `%s`" % loc(scan.evidence("SQLite", limit=1)[0])) if sqlite else None
        add("database", "db_postgres", "Database", "Relational database", "data", evidence, intro,
            tech="PostgreSQL" if pg else "SQLite", replaces=replaces)
    if mysql:
        add("database_mysql", "db_mysql", "Database", "Relational database (MySQL)", "data",
            file_evidence(scan.evidence("MySQL")), sentence_found(scan, ["MySQL"]), tech="MySQL",
            replaces=("SQLite file database at `%s`" % loc(scan.evidence("SQLite", limit=1)[0])) if sqlite else None)
    if mongo:
        add("database_mongo", "db_mongo", "Database", "Document database", "data",
            file_evidence(scan.evidence("MongoDB")), sentence_found(scan, ["MongoDB"]), tech="MongoDB")
    embedded = [n for n in EMBEDDED_VECTOR if scan.has(n)]
    if embedded or scan.has("pgvector"):
        names = embedded or ["pgvector"]
        if "database" in keys:
            db = next(d for d in decisions if d["key"] == "database")
            add("vector", None, "Vector search", "Embeddings inside the main database", "data",
                file_evidence(scan.evidence(*names)),
                sentence_found(scan, names[:2]) + " The same PostgreSQL can store and search embeddings with the "
                "pgvector extension, so there is no extra database to run, back up or secure.",
                tech=", ".join(names), service="pgvector extension on " + db["service"], short="pgvector",
                reason="Embeddings live in the same PostgreSQL through the pgvector extension, so there is no extra database to run.",
                replaces=("%s, which stores vectors in files or memory of the container" % ", ".join(embedded))
                if embedded else None,
                alternatives=([{"service": pick("vector_service")["service"],
                                "reason": "A separate vector service scales on its own but adds another system to run and secure."}]
                              if pick("vector_service") else []),
                why="")
            decisions[-1]["provider"] = db["provider"]
        else:
            add("vector", "vector_service", "Vector search", "Vector search", "data",
                file_evidence(scan.evidence(*names)), sentence_found(scan, names[:2]), tech=", ".join(names))
    cache_names = [n for n in ("Redis", "Memcached") if scan.has(n)]
    if (cache_names or workers) and always_free and cloud_info.get("kind", "cloud") in CLOUD_KINDS_WITHOUT_FREE_CACHE and not any(
            "cache" in registry[o]["overlay"] for o in overlays):
        gaps.append({"key": "cache", "need": "cache", "role": "Cache and job queue broker" if workers else "Cache",
                     "message": "%s has no always-free Redis. Every cache node is billed by the hour, so none is created. Either let the code run without it "
                                "(many apps fall back to memory), or create a free Redis at a provider such as Upstash and put its address in REDIS_URL." % cloud_info["name"]})
        if workers:
            questions.append("A job queue library (%s) needs a broker, and an always-free account has no free one here. Use a free hosted Redis or choose a paid account type." % workers[0])
    elif cache_names or workers:
        broker = (" Your job queue library %s needs a Redis-compatible broker." % workers[0]) if workers else ""
        add("cache", "cache", "Cache", "Cache and job queue broker" if workers else "Cache", "data",
            file_evidence(scan.evidence(*(cache_names or workers))),
            (sentence_found(scan, cache_names[:1]) if cache_names else "") + broker,
            tech=", ".join(cache_names or workers))
    if scan.has("Kafka"):
        add("queue_kafka", "queue_kafka", "Queue", "Event streaming", "data",
            file_evidence(scan.evidence("Kafka")), sentence_found(scan, ["Kafka"]), tech="Kafka")
    if scan.has("RabbitMQ"):
        add("queue_rabbitmq", "queue_rabbitmq", "Queue", "Message broker", "data",
            file_evidence(scan.evidence("RabbitMQ")), sentence_found(scan, ["RabbitMQ"]), tech="RabbitMQ")
    engines = [n for n in SEARCH_ENGINES if scan.has(n)]
    if engines:
        add("search", "search", "Search", "Full-text search", "data", file_evidence(scan.evidence(*engines)),
            sentence_found(scan, engines[:1]), tech=", ".join(engines),
            replaces="Meilisearch, which keeps its index in files on disk" if "Meilisearch" in engines else None)
    if scan.has("MinIO"):
        add("storage", "storage", "Storage", "Object storage", "data", file_evidence(scan.evidence("MinIO")),
            sentence_found(scan, ["MinIO"]), tech="MinIO", replaces="MinIO, which you would otherwise run and back up yourself")
    hosted = [n for n in SELF_HOSTED_AI if scan.has(n)]
    if hosted:
        add("gpu", "gpu", "AI hosting", "Model hosting", "compute", file_evidence(scan.evidence(*hosted)),
            sentence_found(scan, hosted[:1]), tech=", ".join(hosted))

    data_keys = [d["key"] for d in decisions if d["layer"] == "data"]
    native_data = [d for d in decisions if d["layer"] == "data" and d["provider"] == cloud]
    if native_data:
        add("network", "network", "Network", "Private network for data stores", "network",
            rule_evidence("Design requirement: data stores must not be reachable from the internet (SEC-002)"),
            "Your design has " + ", ".join(d["short"] for d in native_data) + " inside your account.",
            confidence="inferred")

    # ---------------------------------------------------------------- services that stay external
    for sdk, label in EXTERNAL_AI.items():
        if scan.has(sdk):
            managed = pick("managed_ai")
            external.append({"key": "ai_" + label.split()[0].lower(), "name": label,
                             "role": "Language model API (stays as it is)", "evidence": file_evidence(scan.evidence(sdk)),
                             "why": ("%s Calls go to the provider's own API. Moving to %s would keep the model but require code changes, "
                                     "so it is listed as an option and not part of this plan." % (
                                         sentence_found(scan, [sdk]), managed["service"])) if managed else
                                    "%s Calls go to the provider's own API, which stays as it is." % sentence_found(scan, [sdk]),
                             "option": managed["service"] if managed else None})
    if scan.has("Pinecone"):
        external.append({"key": "vector_pinecone", "name": "Pinecone", "role": "Hosted vector database (stays as it is)",
                         "evidence": file_evidence(scan.evidence("Pinecone")),
                         "why": sentence_found(scan, ["Pinecone"]) + " It is a hosted service outside your cloud account.",
                         "option": None})
    if scan.has("Clerk"):
        external.append({"key": "auth_clerk", "name": "Clerk", "role": "Hosted sign-in (stays as it is)",
                         "evidence": file_evidence(scan.evidence("Clerk")),
                         "why": sentence_found(scan, ["Clerk"]) + " Sign-in is handled by Clerk, so no cloud sign-in service is added.",
                         "option": None})
    # Private compute reaches the internet through a NAT gateway, which bills every hour. The plan states it whenever the generated
    # network contains one, so the cost report and the quality gates see the same design the code builds.
    private_server = mode == "server" and cloud in PRIVATE_SERVER_CLOUDS
    if has_compute and ((external and native_data) or private_server or cloud in NAT_ALWAYS_CLOUDS):
        reasons = [x["evidence"][0] for x in external if x["evidence"]][:2] or rule_evidence(
            "Design requirement: always-on compute sits in private subnets (SEC-001), so it reaches the internet through a gateway")
        add("egress", "egress", "Network", "Outbound internet for private compute", "network", reasons,
            ("Your app calls " + ", ".join(x["name"] for x in external) + " from private compute.") if external else
            "Always-on containers run in private subnets, so reaching the internet (image pulls, package mirrors, model APIs) needs a gateway.",
            confidence="inferred")

    # ---------------------------------------------------------------- ingress, edge, identity, platform
    if has_compute:
        ingress_need = "ingress"
        if mode == "serverless" and catalog["services"].get("ingress_function_url", {}).get(cloud) and (
                streaming == "used" or lean):
            ingress_need = "ingress_function_url"
        add("ingress", ingress_need, "Ingress", "Public entry point", "ingress",
            rule_evidence("Design requirement: public traffic reaches private compute only through a managed front door (SEC-001)"),
            "Your design has public web traffic reaching application compute." + (
                " The scan found streaming code, which a gateway in front of the functions would buffer and cut off." if ingress_need != "ingress" and streaming == "used" else ""),
            confidence="inferred")
        if lean:
            gaps.append({"key": "edge", "need": "edge", "role": "Content delivery and firewall",
                         "message": "Deferred for the lean design (free-trial account). Add a CDN and web application firewall "
                                    "before production traffic arrives."})
        else:
            add("edge", "edge", "Edge", "Content delivery and firewall", "edge",
                rule_evidence("Design requirement: internet traffic is filtered before it reaches the application (SEC-001)"),
                "The application is reachable from the internet.", confidence="inferred")
    auth = [n for n in AUTH_LIBS if scan.has(n)]
    if auth and not scan.has("Clerk"):
        add("identity", "identity", "Identity", "Managed sign-in (optional)", "support",
            file_evidence(scan.evidence(*auth)), sentence_found(scan, auth[:1]) + " The app handles sign-in itself.",
            tech=", ".join(auth), confidence="inferred",
            caveats=["Optional: your current library keeps working. Move to a managed service if you do not want to store passwords."])
    secret_findings = [f for f in scan_data.get("findings", []) if f["id"] == "SEC-005"]
    if secret_findings and any(not e.get("test_file") for e in secret_findings[0]["evidence"]):
        add("secrets", "secrets", "Secrets", "Credential storage", "support",
            [{"kind": "file", "file": e["file"], "line": e["line"], "text": loc(e)}
             for e in secret_findings[0]["evidence"] if not e.get("test_file")][:3],
            "The scan found hardcoded credentials (values are never shown). Rotate them first, then move them here.",
            caveats=["Treat any credential that appeared in code as compromised until it is rotated."])
    else:
        add("secrets", variant("secrets_lean", "secrets"), "Secrets", "Credential storage", "support",
            rule_evidence("Design requirement: credentials stay out of code (SEC-005)"),
            "Every deployment has credentials such as database passwords.", confidence="inferred")
    if scheduler == "used" and mode == "serverless" and has_compute:
        add("scheduler", "scheduler", "Scheduler", "Scheduled jobs", "support",
            file_evidence(scan_data.get("signal_evidence", {}).get("scheduler", [])[:2]) or rule_evidence("An in-process scheduler was found"),
            "The scan found a scheduler that runs inside the application process.", confidence="confirmed")
    if mode == "server" and docker:
        add("registry", "registry", "Registry", "Container image storage", "support",
            [{"kind": "file", "file": docker[0]["file"], "line": 1, "text": docker[0]["file"] + ":1"}],
            "The repository has a Dockerfile (`%s`)." % docker[0]["file"])
    add("observability", "observability", "Observability", "Logs, metrics and alarms", "support",
        rule_evidence("Design requirement: every deployment needs logs and alarms"),
        "Failures need to be visible after deployment.", confidence="inferred")

    # ---------------------------------------------------------------- questions and costs
    assumptions = []
    if not profile.get("region"):
        default = REGION_DEFAULTS.get(cloud)
        assumptions.append({"key": "region", "value": default,
                            "text": ("Region: none was chosen, so %s is assumed. Set one with `profile --set region=...` if your users are elsewhere, "
                                     "and check data-residency rules if the app stores personal data." % default) if default else
                            "Region: none was chosen. This platform picks the region in its dashboard or CLI; choose the one closest to your users, "
                            "and check data-residency rules if the app stores personal data."})
    if streaming == "import-only":
        questions.append("The scan found a streaming import but no route that uses it. Does the app stream responses?")
    if (scan.has("LangChain") or scan.has("LlamaIndex")) and not any(x["key"].startswith("ai_") for x in external) and not hosted:
        questions.append("An AI framework was found but no model provider SDK. Which model provider does the app use?")
    if scan.has("Whisper"):
        questions.append("Whisper runs a speech model inside the app. That needs a large image and a lot of memory, so it does not fit a small function. "
                         "Use a hosted transcription API, or keep that part on always-on compute.")
    if scan.has("MCP") and mode == "serverless":
        questions.append("The app uses MCP. If tool servers are started as child processes, every cold start launches them again; test cold-start time or use remote MCP servers.")
    if scheduler != "none" and mode == "server" and profile.get("high_availability") != "single_az":
        questions.append("An in-process scheduler runs once per running copy, so with more than one copy every job runs more than once. Run one copy of that part or use a lock.")
    if (scan.has("PyTorch") or scan.has("Transformers")) and not hosted:
        questions.append("PyTorch or Transformers was found. Where do the models run? GPU hosting is not part of this plan.")
    if not has_compute and not spa:
        questions.append("No application framework or Dockerfile was found. Describe how the app runs so compute can be chosen.")
    if cloud_info.get("kind") in ("data", "gpu") and not overlays:
        questions.append("%s provides %s, not general application hosting. Choose where the application runs and combine them, "
                         "for example --cloud aws --also %s." % (cloud_info["name"], "data services" if cloud_info.get("kind") == "data"
                                                                  else "GPU and model hosting", cloud))
    costs = []
    for d in decisions:
        driver = next((COST_DRIVERS[c] for c in (d["key"], d["key"].split("_")[0]) if c in COST_DRIVERS), None)
        if isinstance(driver, dict):
            driver = driver[mode]
        if driver:
            costs.append({"service": "%s (%s)" % (d["short"], d["role"]), "driver": driver})

    queue = []
    for key in VERIFY_ORDER:
        d = next((x for x in decisions if x["key"] == key), None)
        if not d or d["key"] == "vector":
            continue
        same = next((q for q in queue if q["service"] == d["service"]), None)
        if same:
            same["keys"].append(d["key"])  # one web check covers every decision using this service
            continue
        owner = catalog["clouds"].get(d.get("provider") or cloud, cloud_info)
        queue.append({"key": d["key"], "keys": [d["key"]], "service": d["service"], "cloud": d.get("provider") or cloud,
                      "query": "%s %s official documentation" % (d["service"], owner["name"]),
                      "domains": owner["domains"]})
    plan = {
        "plan_version": 2, "project": scan_data.get("root", ""), "cloud": cloud, "cloud_name": cloud_info["name"],
        "provider_kind": cloud_info.get("kind", "cloud"), "overlays": overlays,
        "overlay_names": [catalog["clouds"][o]["name"] for o in overlays],
        "compute_model": mode, "compute_label": catalog["modes"][mode], "decisions": decisions,
        "gaps": gaps, "external_services": external, "open_questions": questions, "assumptions": assumptions, "cost_drivers": costs,
        "verify_queue": queue,
        "auto_choices": auto_choices,
        "config_inputs": [{"name": v["name"], "category": v["category"], "required": v["required"]} for v in scan_data.get("env_vars", [])],
        "profile": {"traffic_tier": profile.get("traffic_tier") or "prototype",
                    "account_type": profile.get("account_type") or "standard",
                    "high_availability": "single_az" if lean else (profile.get("high_availability") or "multi_az"),
                    "compliance": list(profile.get("compliance") or []), "region": profile.get("region")},
    }
    plan["fingerprint"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the SolsArch cloud plan from a scan")
    parser.add_argument("--scan", required=True, help="Scan JSON from ast_scanner.py")
    parser.add_argument("--cloud", required=True, choices=providers.hosting_ids())
    parser.add_argument("--also", default="", help="Comma-separated overlay providers, for example neon,cloudflare")
    parser.add_argument("--compute", required=True, choices=("server", "serverless"), dest="mode")
    parser.add_argument("--output", help="Write plan JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
        if "components" not in scan:
            raise ValueError("not output of ast_scanner.py")
        plan = build_plan(scan, load_catalog(), args.cloud, args.mode, [x for x in args.also.split(",") if x])
    except (OSError, ValueError) as error:
        sys.stderr.write("error: %s\n" % error)
        return 2
    text = json.dumps(plan, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(text.encode("utf-8"))
        sys.stdout.write(json.dumps({"written": args.output, "decisions": len(plan["decisions"]),
                                     "fingerprint": plan["fingerprint"]}) + "\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
