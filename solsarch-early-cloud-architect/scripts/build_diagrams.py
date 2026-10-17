#!/usr/bin/env python3
"""Draw the SolsArch diagrams from the plan and the scan.

Eight diagrams at most, all Mermaid, each with a written explanation: cloud
infrastructure, C4 context, C4 container, C4 component, authentication sequence, core data
path sequence, background job sequence and a map from code folders to the cloud services
that run them. Nothing is drawn that the plan or the scan does not support, and the output
is identical for identical inputs. Every diagram passes diagram_sanitizer before it is
returned.

Standard library only. Library use: build_diagrams.build(plan, scan) -> list of dicts.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram_sanitizer  # noqa: E402
import structure_diagrams  # noqa: E402

PALETTE = Path(__file__).resolve().parent.parent / "assets" / "cloud-vendor-palette.json"
BACKEND = ("FastAPI", "Django", "Flask", "Express", "NestJS", "Ruby on Rails", "Spring Boot", "Laravel", "Gin", "Fastify", "Sinatra", "Axum", "Actix Web")
QUEUES = ("Celery", "BullMQ", "RQ", "Dramatiq", "Sidekiq")


class BuildError(Exception):
    """A diagram failed validation; the message names it."""


class Ctx:
    def __init__(self, plan, scan):
        self.plan, self.scan = plan, scan
        self.cloud = plan["cloud"]
        self.by_key = {d["key"]: d for d in plan["decisions"]}
        self.external = plan.get("external_services", [])
        self.components = {c["name"]: c for c in scan.get("components", [])}

    def has(self, key):
        return key in self.by_key

    def d(self, key):
        return self.by_key[key]

    def label(self, key, role=None):
        d = self.by_key[key]
        return "%s<br/>%s" % (d["short"], role or d["role"])

    def nid(self, key):
        return "n_" + key

    def compute_keys(self):
        return [k for k in ("compute_web", "compute_api", "compute_app") if k in self.by_key]

    def front_key(self):
        keys = self.compute_keys()
        return keys[0] if keys else None

    def data_keys(self):
        return [d["key"] for d in self.plan["decisions"] if d["layer"] == "data"]

    def comp_names(self, *names):
        return [n for n in names if n in self.components]


def node(ctx, key, cls=None, role=None):
    return '%s["%s"]:::%s' % (ctx.nid(key), ctx.label(key, role), cls or ctx.cloud)


def ext_node(x):
    return 'x_%s["%s<br/>external service"]:::external' % (x["key"], x["name"])


def services_bullets(ctx, keys):
    return ["**%s** (%s): %s Evidence: %s." % (ctx.d(k)["short"], ctx.d(k)["role"], ctx.d(k).get("reason", ""),
                                               ctx.d(k)["evidence_text"])
            for k in keys if ctx.has(k)]


# ------------------------------------------------------------------ individual diagrams
def infrastructure(ctx):
    keys = ctx.by_key
    comp = ctx.compute_keys()
    worker = ["compute_worker"] if "compute_worker" in keys else []
    if not (comp or worker or "static_site" in keys or ctx.data_keys()):
        return None
    lines = ["flowchart TB", 'user["Users"]:::user']
    if "edge" in keys or "ingress" in keys or "static_site" in keys:
        lines.append('subgraph pub["Public zone"]')
        for k in ("edge", "ingress", "static_site"):
            if k in keys:
                lines.append("  " + node(ctx, k))
        lines.append("end")
    data = ctx.data_keys()
    lines.append('subgraph vpc["%s"]' % (ctx.d("network")["short"] + " private network" if "network" in keys else "Private zone"))
    app_nodes = comp + worker + (["gpu"] if "gpu" in keys else [])
    if app_nodes:
        lines.append('  subgraph app["Application"]')
        for k in app_nodes:
            lines.append("    " + node(ctx, k))
        lines.append("  end")
    if data:
        lines.append('  subgraph dat["Data"]')
        for k in data:
            lines.append("    " + node(ctx, k, "data"))
        lines.append("  end")
    if "egress" in keys:
        lines.append("  " + node(ctx, "egress"))
    lines.append("end")
    plat = [k for k in ("secrets", "observability", "registry", "identity") if k in keys]
    if plat:
        lines.append('subgraph plat["Platform services"]')
        for k in plat:
            lines.append("  " + node(ctx, k))
        lines.append("end")
    for x in ctx.external:
        lines.append(ext_node(x))
    # traffic path
    front = ctx.front_key()
    entry = "edge" if "edge" in keys else ("ingress" if "ingress" in keys else None)
    if entry:
        lines.append("user --> %s" % ctx.nid(entry))
        if entry == "edge" and "ingress" in keys:
            lines.append("%s --> %s" % (ctx.nid("edge"), ctx.nid("ingress")))
        if front and "ingress" in keys:
            lines.append("%s --> %s" % (ctx.nid("ingress"), ctx.nid(front)))
    if "static_site" in keys:
        lines.append('user -->|"static files"| %s' % ctx.nid("static_site"))
    if "compute_web" in keys and "compute_api" in keys:
        lines.append('%s -->|"calls"| %s' % (ctx.nid("compute_web"), ctx.nid("compute_api")))
    backend = "compute_api" if "compute_api" in keys else ("compute_app" if "compute_app" in keys else front)
    if backend:
        for k in data:
            if k == "vector" and ctx.d("vector")["short"] == "pgvector":
                lines.append('%s -.->|"runs inside"| %s' % (ctx.nid("vector"), ctx.nid("database")))
            else:
                lines.append("%s --> %s" % (ctx.nid(backend), ctx.nid(k)))
        if "gpu" in keys:
            lines.append("%s --> %s" % (ctx.nid(backend), ctx.nid("gpu")))
    for k in worker:
        for target in [t for t in ("cache", "database") if t in keys]:
            lines.append("%s --> %s" % (ctx.nid(k), ctx.nid(target)))
    if backend and "egress" in keys:
        lines.append('%s -.-> %s' % (ctx.nid(backend), ctx.nid("egress")))
        for x in ctx.external:
            lines.append('%s -.->|"HTTPS"| x_%s' % (ctx.nid("egress"), x["key"]))
    for k in comp + worker:
        for p in ("secrets", "observability"):
            if p in keys:
                lines.append("%s -.-> %s" % (ctx.nid(k), ctx.nid(p)))
    if "registry" in keys and front:
        lines.append('%s -.->|"images"| %s' % (ctx.nid("registry"), ctx.nid(front)))
    if "identity" in keys and backend:
        lines.append('%s -.-> %s' % (ctx.nid(backend), ctx.nid("identity")))
    path = " to ".join(["Users"] + [ctx.d(k)["short"] for k in ("edge", "ingress") if k in keys] +
                       [ctx.d(front)["short"] if front else "your app"])
    points = ["Traffic path: %s." % path]
    if data:
        points.append("The data stores (%s) sit in the private zone, so nothing on the internet can connect to them directly."
                      % ", ".join(ctx.d(k)["short"] for k in data))
    if "egress" in keys:
        points.append("Calls to %s leave through %s, which allows outbound traffic only."
                      % (", ".join(x["name"] for x in ctx.external), ctx.d("egress")["short"]))
    return {
        "id": "infrastructure", "title": "Cloud infrastructure", "mmd": "\n".join(lines),
        "what": "The cloud services your application would run on, grouped by zone: what the public can reach, "
                "what runs your code, where data lives, and the supporting services around them.",
        "read": "Solid arrows show requests or data flowing. Dotted arrows show supporting links such as secrets, logs "
                "and outbound calls. Grey boxes are services outside your cloud account.",
        "points": points,
        "services": [k for k in ("edge", "ingress", "static_site") if k in keys] + comp + worker + data +
        [k for k in ("network", "egress") if k in keys] + plat + (["gpu"] if "gpu" in keys else []),
        "limits": "Zones and links follow the plan, not a live account. Availability zones and subnet ranges are not drawn.",
    }


def has_parts(ctx):
    return bool(ctx.compute_keys() or "static_site" in ctx.by_key or "compute_worker" in ctx.by_key or ctx.data_keys())


def c4_context(ctx):
    if not has_parts(ctx):
        return None
    name = ctx.plan.get("project") or "Your system"
    lines = ["flowchart LR", 'user["Users<br/>people using the app"]:::user',
             'system["%s<br/>the system you are deploying"]:::%s' % (name, ctx.cloud)]
    lines.append('user -->|"uses over HTTPS"| system')
    for x in ctx.external:
        lines.append(ext_node(x))
        lines.append('system -->|"calls"| x_%s' % x["key"])
    if "identity" in ctx.by_key:
        lines.append('idp["%s<br/>managed sign-in"]:::%s' % (ctx.d("identity")["short"], ctx.cloud))
        lines.append('system -->|"signs users in with"| idp')
    names = ", ".join(x["name"] for x in ctx.external)
    return {
        "id": "c4-context", "title": "C4 level 1: system context", "mmd": "\n".join(lines),
        "what": "The system as one box, with the people who use it and the outside services it depends on.",
        "read": "Start here to see who and what touches the system. Details inside the box come in the next diagrams.",
        "points": ["External services found in the code: %s." % names if names else
                   "No external services were found in the code."],
        "services": ["identity"] if "identity" in ctx.by_key else [],
        "limits": "Only services found in the scan are shown. People who administer the system are not drawn.",
    }


def c4_container(ctx):
    if not has_parts(ctx):
        return None
    keys = ctx.by_key
    name = ctx.plan.get("project") or "Your system"
    lines = ["flowchart LR", 'user["Users"]:::user', 'subgraph sys["%s"]' % name]
    for k in ctx.compute_keys() + (["compute_worker"] if "compute_worker" in keys else []) + \
            [k for k in ("static_site",) if k in keys]:
        d = ctx.d(k)
        lines.append('  %s["%s<br/>%s on %s"]:::%s' % (ctx.nid(k), d["role"], d["tech"] or "your code", d["short"], ctx.cloud))
    for k in ctx.data_keys():
        lines.append("  " + node(ctx, k, "data"))
    lines.append("end")
    for x in ctx.external:
        lines.append(ext_node(x))
    front = ctx.front_key() or ("static_site" if "static_site" in keys else None)
    backend = "compute_api" if "compute_api" in keys else ("compute_app" if "compute_app" in keys else front)
    if front:
        lines.append('user -->|"HTTPS"| %s' % ctx.nid(front))
    if "static_site" in keys and front != "static_site":
        lines.append('user -->|"static files"| %s' % ctx.nid("static_site"))
    if "compute_web" in keys and "compute_api" in keys:
        lines.append('%s -->|"calls"| %s' % (ctx.nid("compute_web"), ctx.nid("compute_api")))
    for k in ctx.data_keys():
        if backend and not (k == "vector" and ctx.d("vector")["short"] == "pgvector"):
            lines.append("%s --> %s" % (ctx.nid(backend), ctx.nid(k)))
    if "vector" in keys and ctx.d("vector")["short"] == "pgvector" and "database" in keys:
        lines.append('%s -.->|"stored in"| %s' % (ctx.nid("vector"), ctx.nid("database")))
    if "compute_worker" in keys:
        for t in [t for t in ("cache", "database") if t in keys]:
            lines.append("%s --> %s" % (ctx.nid("compute_worker"), ctx.nid(t)))
    if backend:
        for x in ctx.external:
            lines.append('%s -->|"HTTPS"| x_%s' % (ctx.nid(backend), x["key"]))
    units = ctx.compute_keys() + (["compute_worker"] if "compute_worker" in keys else []) + ctx.data_keys() + \
        (["static_site"] if "static_site" in keys else [])
    return {
        "id": "c4-container", "title": "C4 level 2: containers", "mmd": "\n".join(lines),
        "what": "The separately running parts of your system: front end, API, workers and data stores, and the technology "
                "and cloud service each one uses.",
        "read": "Each box is something that runs or stores data on its own. Arrows are the calls between them.",
        "points": ["%s runs %s (%s)." % (ctx.d(k)["role"], ctx.d(k)["tech"] or "your code", ctx.d(k)["service"])
                   for k in ctx.compute_keys() + (["compute_worker"] if "compute_worker" in keys else [])],
        "services": units,
        "limits": "Connections follow the usual pattern for these parts. The scan does not trace individual calls.",
    }


def c4_component(ctx):
    names = ctx.components
    framework = ctx.comp_names(*BACKEND)
    if not framework and not any(k in ctx.by_key for k in ("compute_app", "compute_api")):
        return None
    top = framework[0] if framework else "Application code"
    parts = [("routes", "Request handlers<br/>%s" % top, [top] if framework else [])]
    groups = (
        ("auth", "Sign-in<br/>%s", ("Passport", "NextAuth", "Authlib", "JWT library", "Clerk")),
        ("orm", "Data access<br/>%s", ("Prisma", "SQLAlchemy", "TypeORM", "Sequelize", "Drizzle", "GORM", "PostgreSQL", "MySQL",
                                       "MongoDB", "SQLite")),
        ("cache", "Cache client<br/>%s", ("Redis", "Memcached")),
        ("jobs", "Job producer<br/>%s", QUEUES + ("Kafka", "RabbitMQ")),
        ("ai", "Model client<br/>%s", ("OpenAI SDK", "Anthropic SDK", "LangChain", "LlamaIndex", "vLLM", "Ollama")),
        ("vec", "Vector store client<br/>%s", ("ChromaDB", "FAISS", "LanceDB", "Qdrant", "Weaviate", "Pinecone", "pgvector")),
        ("search", "Search client<br/>%s", ("Meilisearch", "Elasticsearch", "OpenSearch")),
    )
    for ident, template, candidates in groups:
        found = ctx.comp_names(*candidates)
        if found:
            parts.append((ident, template % ", ".join(found[:2]), found))
    lines = ["flowchart TB", 'subgraph api["%s container"]' % (ctx.d("compute_api")["role"] if ctx.has("compute_api") else "Application")]
    for ident, label, _ in parts:
        lines.append('  c_%s["%s"]:::%s' % (ident, label, ctx.cloud if ident == "routes" else "external"))
    lines.append("end")
    for ident, _, _ in parts[1:]:
        lines.append("c_routes --> c_%s" % ident)
    evidence = []
    for _, label, found in parts:
        for n in found[:1]:
            ev = names[n]["evidence"][0]
            evidence.append("%s: `%s:%s`" % (n, ev["file"], ev["line"]))
    return {
        "id": "c4-component", "title": "C4 level 3: components of the application", "mmd": "\n".join(lines),
        "what": "The building blocks inside the main application container, derived from the libraries your code uses.",
        "read": "Each box is a responsibility the code has. The request handlers use the other parts to do their work.",
        "points": ["Evidence for each part: " + "; ".join(evidence) + "."],
        "services": [], "limits": "Derived from the libraries found, not from tracing function calls, so the arrows show the "
        "usual relationship and not every real call.",
    }


def sequence_auth(ctx):
    idp = ctx.d("identity")["short"] if ctx.has("identity") else None
    if idp is None:
        return None
    keys = ctx.by_key
    lines = ["sequenceDiagram", "  autonumber", "  participant B as Browser"]
    chain = []
    for k, ident in (("edge", "E"), ("ingress", "I")):
        if k in keys:
            lines.append("  participant %s as %s" % (ident, ctx.d(k)["short"]))
            chain.append(ident)
    lines += ["  participant A as Application", "  participant P as %s" % idp]
    hops = ["B"] + chain + ["A"]
    for a, b in zip(hops, hops[1:]):
        lines.append("  %s->>%s: Sign-in request" % (a, b))
    lines += ["  A-->>B: Redirect to the sign-in page", "  B->>P: Enter credentials", "  P-->>B: Authorization code",
              "  B->>A: Return with code", "  A->>P: Exchange code for tokens", "  P-->>A: ID token and access token",
              "  A-->>B: Session cookie", "  B->>A: Later requests carry the session", "  A->>A: Check signature, expiry and audience",
              "  A-->>B: Response"]
    return {
        "id": "sequence-auth", "title": "Sign-in sequence", "mmd": "\n".join(lines),
        "what": "How a user signs in through the managed identity service, step by step.",
        "read": "Read top to bottom. Time moves down the page, and each arrow is one message.",
        "points": ["Passwords are handled by %s, so the application never stores them." % idp,
                   "The application checks the token signature, expiry and audience on every request."],
        "services": ["identity"] + [k for k in ("edge", "ingress") if k in keys],
        "limits": "Shows the standard authorization-code flow. Your code may use a different flow.",
    }


def sequence_core(ctx):
    if not (ctx.compute_keys() or "static_site" in ctx.by_key):
        return None
    keys = ctx.by_key
    ai = ctx.external[0] if ctx.external and ctx.external[0]["key"].startswith("ai_") else None
    store = "vector" if "vector" in keys else ("database" if "database" in keys else None)
    lines = ["sequenceDiagram", "  autonumber", "  participant B as Browser"]
    chain = []
    for k, ident in (("edge", "E"), ("ingress", "I")):
        if k in keys:
            lines.append("  participant %s as %s" % (ident, ctx.d(k)["short"]))
            chain.append(ident)
    lines.append("  participant A as Application")
    hops = ["B"] + chain + ["A"]
    forward = ["  %s->>%s: Request" % (a, b) for a, b in zip(hops, hops[1:])]
    if ai and store:
        lines.append("  participant S as %s" % ctx.d(store)["short"])
        lines.append("  participant M as %s" % ai["name"])
        lines += forward + ["  A->>S: Find content related to the question", "  S-->>A: Matching passages",
                            "  A->>M: Question plus passages, streaming on", "  loop Until the answer is complete",
                            "    M-->>A: Next piece of the answer", "    A-->>B: Stream it to the browser", "  end",
                            "  A->>S: Save the conversation"]
        title, what = "Question and answer path", "How a question travels through the system, is answered from your data by a language model, and streams back."
        points = ["The application searches %s first, so the model answers from your content." % ctx.d(store)["short"],
                  "Answers stream back piece by piece, so the connection must stay open for the whole answer."]
        if ctx.scan.get("signals", {}).get("streaming") == "none":
            points.append("No streaming code was found in the scan; this flow assumes streaming responses.")
        services = [k for k in ("edge", "ingress") if k in keys] + [store]
    elif ai:
        lines.append("  participant M as %s" % ai["name"])
        lines += forward + ["  A->>M: Send the prompt", "  M-->>A: Response", "  A-->>B: Response"]
        title, what, points = "Model request path", "How a request travels to the language model and back.", \
            ["The model is called from the application, so its API key must come from the secrets service."]
        services = [k for k in ("edge", "ingress") if k in keys]
    else:
        lines.append("  participant D as %s" % (ctx.d("database")["short"] if "database" in keys else "Data store"))
        lines += forward + ["  A->>D: Query", "  D-->>A: Rows", "  A-->>B: Response"]
        title, what, points = "Request path", "How an ordinary request travels through the system and reads data.", \
            ["The data store never starts a connection; it only answers the application."]
        services = [k for k in ("edge", "ingress", "database") if k in keys]
    return {"id": "sequence-core", "title": title, "mmd": "\n".join(lines), "what": what,
            "read": "Read top to bottom. Time moves down the page, and each arrow is one message.", "points": points,
            "services": services, "limits": "Typical flow for this kind of app. Check the steps against your real routes."}


def sequence_async(ctx):
    keys = ctx.by_key
    if "compute_worker" not in keys:
        return None
    broker = ctx.d("cache")["short"] if "cache" in keys else "Job queue"
    db = ctx.d("database")["short"] if "database" in keys else "Data store"
    lines = ["sequenceDiagram", "  autonumber", "  participant B as Browser", "  participant A as Application",
             "  participant Q as %s" % broker, "  participant W as Worker", "  participant D as %s" % db,
             "  B->>A: Start a long task", "  A->>Q: Put the job on the queue", "  A-->>B: Accepted with a job id",
             "  Q->>W: Deliver the job", "  W->>D: Save the result", "  B->>A: Ask for the result",
             "  A->>D: Read the status", "  A-->>B: Done"]
    return {
        "id": "sequence-async", "title": "Background job sequence", "mmd": "\n".join(lines),
        "what": "How slow work is moved out of the web request and handled by a worker.",
        "read": "Read top to bottom. The browser gets an answer immediately and checks back for the result.",
        "points": ["Workers run %s, found in your code." % ctx.d("compute_worker")["tech"],
                   "%s holds the queue, so jobs survive a restart of the application." % broker],
        "services": [k for k in ("compute_worker", "cache", "database") if k in keys],
        "limits": "Shows the usual job pattern. Retries and failed-job handling are not drawn.",
    }


def topology(ctx):
    layout = ctx.scan.get("code_layout", [])
    keys = ctx.by_key
    runtime = [k for k in ctx.compute_keys() + ["static_site", "compute_worker"] if k in keys]
    if not layout or not runtime:
        return None
    lines = ["flowchart LR", 'subgraph repo["Repository folders"]']
    edges, notes = [], []
    for index, entry in enumerate(layout):
        comps = set(entry.get("components", []))
        label = "%s<br/>%s" % ("(root files)" if entry["path"] == "." else entry["path"] + "/",
                               ", ".join(entry["languages"]) or "files")
        lines.append('  d%d["%s"]:::external' % (index, label))
        targets = []
        if "Next.js" in comps and "compute_web" in keys:
            targets.append("compute_web")
        if comps & set(BACKEND):
            targets.append("compute_api" if "compute_api" in keys else ("compute_app" if "compute_app" in keys else None))
        if comps & set(QUEUES) and "compute_worker" in keys:
            targets.append("compute_worker")
        if comps & {"React", "Vue"} and "static_site" in keys and "Next.js" not in comps:
            targets.append("static_site")
        if not targets:
            fallback = "compute_api" if "compute_api" in keys else ("compute_app" if "compute_app" in keys else runtime[0])
            targets.append(fallback)
            notes.append("%s has no framework of its own, so it is assumed to ship with %s."
                         % ("The repository root" if entry["path"] == "." else entry["path"] + "/", ctx.d(fallback)["short"]))
        for t in targets:
            if t:
                edges.append('d%d -->|"runs on"| %s' % (index, ctx.nid(t)))
    lines.append("end")
    lines.append('subgraph run["Cloud services"]')
    for k in runtime:
        lines.append("  " + node(ctx, k))
    lines.append("end")
    lines += sorted(set(edges), key=edges.index)
    return {
        "id": "topology", "title": "Code to cloud map", "mmd": "\n".join(lines),
        "what": "Which folders of your repository run on which cloud service.",
        "read": "Each left box is a top-level folder with the languages found in it. The arrow points to the service that runs it.",
        "points": notes or ["Every folder maps to a service because the scan found a framework in it."],
        "services": runtime,
        "limits": "Based on the frameworks found in each folder. Shared libraries are mapped to the application that most likely uses them.",
    }


# ------------------------------------------------------------------ assembly
def classdefs(text):
    wanted = set(re.findall(r":::(\w+)", text))
    try:
        defs = json.loads(PALETTE.read_text(encoding="utf-8"))["mermaid_classdefs"]
    except (OSError, ValueError, KeyError):
        return []
    return [line for line in defs if line.split()[1] in wanted]


def build(plan, scan):
    ctx = Ctx(plan, scan)
    for d in plan["decisions"]:
        d.setdefault("reason", d["why"])
    diagrams = []
    for maker in (infrastructure, c4_context, c4_container, c4_component, structure_diagrams.frontend_structure,
                  structure_diagrams.backend_structure, structure_diagrams.data_schema, sequence_auth, sequence_core,
                  sequence_async, topology):
        result = maker(ctx)
        if result is None:
            continue
        text = result["mmd"]
        if text.startswith("flowchart"):
            text += "\n" + "\n".join(classdefs(text))
        cleaned, _, issues = diagram_sanitizer.sanitize(text)
        if issues:
            raise BuildError("%s: %s" % (result["id"], "; ".join(issues)))
        result["mmd"] = cleaned.rstrip("\n")
        result["bullets"] = services_bullets(ctx, result.pop("services"))
        diagrams.append(result)
    for number, item in enumerate(diagrams, 1):
        item["number"] = number
        item["file"] = "%02d-%s.mmd" % (number, item["id"])
    return diagrams


LOGO_FILE = "solsarch-logo.png"
LOGO_TAG = '<img src="diagrams/%s" align="right" width="40" alt="SolsArch">' % LOGO_FILE


def diagram_markdown(item, heading="##"):
    out = ["%s %d. %s" % (heading, item["number"], item["title"]), "", LOGO_TAG, "", "```mermaid", item["mmd"], "```", "",
           "**What it shows.** " + item["what"], "", "**How to read it.** " + item["read"], ""]
    if item["points"]:
        out += ["**What to notice.**", ""] + ["- " + p for p in item["points"]] + [""]
    if item["bullets"]:
        out += ["**Why these services.**", ""] + ["- " + b for b in item["bullets"]] + [""]
    out += ["*Limits: %s*" % item["limits"], ""]
    return "\n".join(out)


ICON_FILES = (("Cloud infrastructure with service icons", "diagrams/infrastructure-icons.svg"),
              ("Services chosen, with icons", "diagrams/service-cards.svg"))


def icon_images(available):
    """Markdown image lines for the icon pictures that were drawn."""
    return ["![%s](%s)" % (alt, path) for alt, path in ICON_FILES if path.split("/")[-1] in available]


def architecture_markdown(diagrams, plan, images=()):
    if not diagrams:
        return "\n".join(["# Architecture diagrams: %s" % (plan.get("project") or "project"), "",
                          "No diagrams were drawn because the scan found no application code, data stores or web front end to draw.",
                          ""])
    head = ["# Architecture diagrams: %s" % (plan.get("project") or "project"), "",
            "Cloud: %s. Compute model: %s. Plan fingerprint: %s." % (plan["cloud_name"], plan["compute_label"], plan["fingerprint"]),
            ""]
    for image in images:
        head += [image, ""]
    return "\n".join(head) + "\n" + "\n".join(diagram_markdown(d) for d in diagrams)
