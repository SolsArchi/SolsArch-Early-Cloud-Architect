#!/usr/bin/env python3
"""Turn a scan plus two user choices into a cloud plan.

Inputs: the scan from ast_scanner.py, the cloud (aws, azure, gcp) and the compute model
(server or serverless). Output: plan.json with one decision per service, each carrying
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
CATALOG_PATH = HERE.parent / "references" / "service-catalog.json"

BACKEND_FRAMEWORKS = ("FastAPI", "Django", "Flask", "Express", "NestJS", "Ruby on Rails",
                      "Spring Boot", "Laravel", "Gin", "Fastify", "Sinatra", "Axum", "Actix Web")
QUEUE_LIBS = ("Celery", "BullMQ", "RQ", "Dramatiq", "Sidekiq")
AUTH_LIBS = ("Passport", "NextAuth", "Authlib", "JWT library")
EXTERNAL_AI = {"OpenAI SDK": "OpenAI API", "Anthropic SDK": "Anthropic API"}
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
    return json.loads(Path(path).read_text(encoding="utf-8"))


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


def build_plan(scan_data, catalog, cloud, mode):
    if cloud not in catalog["clouds"]:
        raise ValueError("cloud must be one of: " + ", ".join(catalog["clouds"]))
    if mode not in catalog["modes"]:
        raise ValueError("compute model must be one of: " + ", ".join(catalog["modes"]))
    scan = Scan(scan_data)
    cloud_info = catalog["clouds"][cloud]
    decisions, external, questions = [], [], []
    keys = set()

    def pick(need):
        options = catalog["services"][need][cloud]
        return options.get(mode) or options["any"]

    def add(key, need, category, role, layer, evidence, intro, tech="", replaces=None, caveats=(),
            confidence="confirmed", service=None, short=None, why=None, alternatives=None, reason=None):
        entry = pick(need) if need else {}
        text = " ".join(part for part in (intro, why or entry.get("why", "")) if part)
        notes = [c for c in ([entry.get("caveat")] + list(caveats)) if c]
        decisions.append({
            "key": key, "category": category, "role": role, "layer": layer, "cloud": cloud,
            "service": service or entry["service"], "short": short or entry["short"], "tech": tech,
            "evidence": evidence, "evidence_text": ", ".join(e["text"] for e in evidence) or "-",
            "why": text, "replaces": replaces,
            "alternatives": alternatives if alternatives is not None else entry.get("alternatives", []),
            "caveats": notes, "confidence": confidence, "need": need,
            "reason": reason or entry.get("why", ""),
        })
        keys.add(key)

    signals = scan_data.get("signals", {})
    streaming = signals.get("streaming", "none")
    docker = scan_data.get("infrastructure", {}).get("dockerfiles", [])
    languages = scan_data.get("languages", {})

    # ---------------------------------------------------------------- compute
    backend = [n for n in BACKEND_FRAMEWORKS if scan.has(n)]
    has_next = scan.has("Next.js")
    spa = (scan.has("React") or scan.has("Vue")) and not has_next
    serverless_notes = []
    if mode == "serverless" and cloud in ("aws", "azure") and (backend or has_next):
        switch = catalog["services"]["compute"][cloud]["server"]["service"]
        serverless_notes.append(
            "%s run on functions through an adapter layer, and large apps may need changes. If that is too much change, "
            "choose the server-based model, which uses %s." % (", ".join(backend + (["Next.js"] if has_next else [])), switch))
    if mode == "serverless" and streaming != "none":
        serverless_notes.append(
            "The scan found streaming code (%s). Serverless platforms limit streaming and run time, "
            "so test the streaming route before committing." % (
                "used in a route" if streaming == "used" else "imported but not yet used"))
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
        add("static_site", "static_site", "Static hosting", "Front end (built static files)", "compute",
            file_evidence(scan.evidence("React", "Vue")), sentence_found(scan, [n for n in ("React", "Vue") if scan.has(n)]),
            tech="React or Vue", confidence="inferred")
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
                alternatives=[{"service": pick("vector_service")["service"],
                               "reason": "A separate vector service scales on its own but adds another system to run and secure."}],
                why="")
        else:
            add("vector", "vector_service", "Vector search", "Vector search", "data",
                file_evidence(scan.evidence(*names)), sentence_found(scan, names[:2]), tech=", ".join(names))
    cache_names = [n for n in ("Redis", "Memcached") if scan.has(n)]
    if cache_names or workers:
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
    if data_keys:
        add("network", "network", "Network", "Private network for data stores", "network",
            rule_evidence("Design requirement: data stores must not be reachable from the internet (SEC-002)"),
            "Your design has " + ", ".join(d["short"] for d in decisions if d["layer"] == "data") + ".",
            confidence="inferred")

    # ---------------------------------------------------------------- services that stay external
    for sdk, label in EXTERNAL_AI.items():
        if scan.has(sdk):
            external.append({"key": "ai_" + label.split()[0].lower(), "name": label,
                             "role": "Language model API (stays as it is)", "evidence": file_evidence(scan.evidence(sdk)),
                             "why": "%s Calls go to the provider's own API. Moving to %s would keep the model but require code changes, "
                                    "so it is listed as an option and not part of this plan." % (
                                        sentence_found(scan, [sdk]), pick("managed_ai")["service"]),
                             "option": pick("managed_ai")["service"]})
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
    if external and data_keys and has_compute:
        add("egress", "egress", "Network", "Outbound internet for private compute", "network",
            [x["evidence"][0] for x in external if x["evidence"]][:2],
            "Your app calls " + ", ".join(x["name"] for x in external) + " from private compute.", confidence="inferred")

    # ---------------------------------------------------------------- ingress, edge, identity, platform
    if has_compute:
        add("ingress", "ingress", "Ingress", "Public entry point", "ingress",
            rule_evidence("Design requirement: public traffic reaches private compute only through a managed front door (SEC-001)"),
            "Your design has public web traffic reaching application compute.", confidence="inferred")
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
        add("secrets", "secrets", "Secrets", "Credential storage", "support",
            rule_evidence("Design requirement: credentials stay out of code (SEC-005)"),
            "Every deployment has credentials such as database passwords.", confidence="inferred")
    if mode == "server" and docker:
        add("registry", "registry", "Registry", "Container image storage", "support",
            [{"kind": "file", "file": docker[0]["file"], "line": 1, "text": docker[0]["file"] + ":1"}],
            "The repository has a Dockerfile (`%s`)." % docker[0]["file"])
    add("observability", "observability", "Observability", "Logs, metrics and alarms", "support",
        rule_evidence("Design requirement: every deployment needs logs and alarms"),
        "Failures need to be visible after deployment.", confidence="inferred")

    # ---------------------------------------------------------------- questions and costs
    questions.append("Region: none was chosen. Pick the region closest to your users, and check data-residency "
                     "rules if the app stores personal data.")
    if streaming == "import-only":
        questions.append("The scan found a streaming import but no route that uses it. Does the app stream responses?")
    if (scan.has("LangChain") or scan.has("LlamaIndex")) and not any(x["key"].startswith("ai_") for x in external) and not hosted:
        questions.append("An AI framework was found but no model provider SDK. Which model provider does the app use?")
    if (scan.has("PyTorch") or scan.has("Transformers")) and not hosted:
        questions.append("PyTorch or Transformers was found. Where do the models run? GPU hosting is not part of this plan.")
    if not has_compute and not spa:
        questions.append("No application framework or Dockerfile was found. Describe how the app runs so compute can be chosen.")
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
        queue.append({"key": d["key"], "keys": [d["key"]], "service": d["service"], "cloud": cloud,
                      "query": "%s %s official documentation" % (d["service"], cloud_info["name"]),
                      "domains": cloud_info["domains"]})
    plan = {
        "plan_version": 1, "project": scan_data.get("root", ""), "cloud": cloud, "cloud_name": cloud_info["name"],
        "compute_model": mode, "compute_label": catalog["modes"][mode], "decisions": decisions,
        "external_services": external, "open_questions": questions, "cost_drivers": costs, "verify_queue": queue,
    }
    plan["fingerprint"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the SolsArch cloud plan from a scan")
    parser.add_argument("--scan", required=True, help="Scan JSON from ast_scanner.py")
    parser.add_argument("--cloud", required=True, choices=("aws", "azure", "gcp"))
    parser.add_argument("--compute", required=True, choices=("server", "serverless"), dest="mode")
    parser.add_argument("--output", help="Write plan JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        scan = json.loads(Path(args.scan).read_text(encoding="utf-8"))
        if "components" not in scan:
            raise ValueError("not output of ast_scanner.py")
        plan = build_plan(scan, load_catalog(), args.cloud, args.mode)
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
