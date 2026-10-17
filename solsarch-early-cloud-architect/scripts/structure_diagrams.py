#!/usr/bin/env python3
"""Mermaid diagrams for the front end, the back end and the database schema.

Standard library only. These read the "structure" block that structure_scanner.py puts in scan.json and return
the same dictionary shape as the other makers in build_diagrams.py (id, title, mmd, what, read, points,
services, limits). A maker returns None when the repository has nothing to show, so no empty diagram is drawn.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from structure_scanner import normalise_path, service_of  # noqa: E402

MAX_ROUTE_NODES = 12
MAX_ENDPOINT_NODES = 10
MAX_MODULE_NODES = 24
MAX_LINES_PER_MODULE = 5
MAX_ENTITIES = 20
MAX_COLUMNS = 14
FRONT_FRAMEWORKS = ("Next.js", "React", "Vue")
DATA_CATEGORIES = ("database", "cache", "vector", "search", "queue", "storage")


def _text(value, limit=48):
    """Safe text for a quoted Mermaid label."""
    value = re.sub(r"[\"<>`\r\n|]", "'", str(value)).strip()
    return value if len(value) <= limit else value[:limit - 1] + "..."


def _structure(ctx):
    return ctx.scan.get("structure") or {}


def _keys_present(ctx, wanted):
    return [k for k in wanted if ctx.has(k)]


def _data_names(ctx):
    names = []
    for comp in ctx.scan.get("components", []):
        if comp["category"] in DATA_CATEGORIES and comp["name"] not in names:
            names.append(comp["name"])
    return names


def _call_path(target):
    path = re.sub(r"^https?://[^/]+", "", target)
    return normalise_path(path or "/")


def _matches(call, endpoint):
    if call == endpoint:
        return True
    return call != "/" and endpoint != "/" and (call.endswith(endpoint) or endpoint.endswith(call))


def frontend_structure(ctx):
    front = _structure(ctx).get("frontend") or {}
    routes, calls, comps = front.get("routes", []), front.get("api_calls", []), front.get("components", [])
    if not (routes or calls or comps):
        return None
    backend = (_structure(ctx).get("backend") or {}).get("endpoints", [])
    frameworks = ctx.comp_names(*FRONT_FRAMEWORKS)
    lines = ["flowchart LR", '  user(["Browser user"]):::user',
             '  subgraph fe["Frontend%s"]' % (": " + ", ".join(frameworks) if frameworks else "")]
    shown = routes[:MAX_ROUTE_NODES]
    file_node = {}
    for i, route in enumerate(shown):
        lines.append('    r%d["%s<br/>%s"]' % (i, _text(route["path"]), _text(route["file"], 40)))
        file_node.setdefault(route["file"], "r%d" % i)
    comp_files = {c["file"] for c in comps}
    if comps:
        names = [c["name"] for c in comps[:6]]
        more = len(comps) - len(names)
        lines.append('    comps["Components<br/>%s"]' % _text(", ".join(names) + (" +%d more" % more if more > 0 else ""), 70))
    lines.append("  end")
    if shown:
        lines += ["  user --> r%d" % i for i in range(min(len(shown), 6))]
    else:
        lines.append('  user --> fe')
    if comps and shown:
        lines.append('  r0 -.->|"renders"| comps')
    edges, matched_eps, other_local, hosts = [], {}, [], []
    shared_used = False
    for call in calls:
        source = file_node.get(call["file"]) or ("comps" if call["file"] in comp_files else None)
        if source is None:
            source, shared_used = "shared", True
        target = call["target"]
        if target.startswith("http"):
            host = re.sub(r"^https?://", "", target).split("/")[0]
            if host not in hosts:
                hosts.append(host)
            edges.append((source, "ext"))
            continue
        path = _call_path(target)
        hit = next((ep for ep in backend if _matches(path, normalise_path(ep["path"]))), None)
        if hit is None:
            if path not in other_local:
                other_local.append(path)
            edges.append((source, "unmatched"))
        else:
            key = (hit["method"], hit["path"])
            if key not in matched_eps and len(matched_eps) < MAX_ENDPOINT_NODES:
                matched_eps[key] = "e%d" % len(matched_eps)
            if key in matched_eps:
                edges.append((source, matched_eps[key]))
    if shared_used:
        lines.append('  shared["Shared frontend code"]')
    if matched_eps or other_local:
        lines.append('  subgraph be["Backend API"]')
        for (method, path), node_id in matched_eps.items():
            lines.append('    %s["%s %s"]' % (node_id, _text(method), _text(path)))
        if other_local:
            lines.append('    unmatched["%s"]' % _text("Other calls: " + ", ".join(other_local[:4])
                                                     + (" +%d" % (len(other_local) - 4) if len(other_local) > 4 else ""), 80))
        lines.append("  end")
    if hosts:
        lines.append('  ext["External APIs<br/>%s"]:::external' % _text(", ".join(hosts[:3]) + (" +%d" % (len(hosts) - 3) if len(hosts) > 3 else ""), 70))
    seen = []
    for source, target in edges:
        if (source, target) not in seen:
            seen.append((source, target))
            lines.append('  %s -->|"HTTP"| %s' % (source, target))
    points = ["%d route(s) found%s." % (len(routes), "" if len(routes) <= MAX_ROUTE_NODES else
                                          ", the first %d are drawn" % MAX_ROUTE_NODES)]
    if calls:
        points.append("%d call(s) to a backend or external API were found in the front-end code." % len(calls))
    if other_local:
        points.append("Calls that match no endpoint the scan found are grouped as 'Other calls'; the backend may be in another repository.")
    return {
        "id": "frontend", "title": "Frontend structure", "mmd": "\n".join(lines),
        "what": "The pages and components of the user interface and the API calls they make.",
        "read": "Each box in the Frontend group is a page route or the shared components. Arrows leave the page that makes a call and point at the endpoint it reaches.",
        "points": points,
        "services": _keys_present(ctx, ("edge", "compute_web", "static_site")),
        "limits": "Found by reading route files and literal fetch or axios calls. Calls built from variables, GraphQL clients and generated API clients are not seen.",
    }


def backend_structure(ctx):
    endpoints = (_structure(ctx).get("backend") or {}).get("endpoints", [])
    if not endpoints:
        return None
    by_service = {}
    for ep in endpoints:
        by_service.setdefault(ep["service"], {}).setdefault(ep["file"], []).append(ep)
    data = _data_names(ctx)
    lines = ["flowchart LR", '  client(["Clients"]):::user']
    counter, drawn, total_modules = 0, [], sum(len(files) for files in by_service.values())
    for si, service in enumerate(sorted(by_service)):
        files = by_service[service]
        frameworks = sorted({ep["framework"] for eps in files.values() for ep in eps})
        title = ("repository root" if service == "." else service) + ": " + ", ".join(frameworks)
        lines.append('  subgraph svc%d["%s"]' % (si, _text(title, 60)))
        for file in sorted(files):
            if counter >= MAX_MODULE_NODES:
                break
            eps = files[file]
            body = ["%s %s" % (ep["method"], ep["path"]) for ep in eps[:MAX_LINES_PER_MODULE]]
            if len(eps) > MAX_LINES_PER_MODULE:
                body.append("+%d more" % (len(eps) - MAX_LINES_PER_MODULE))
            lines.append('    m%d["%s<br/>%s"]' % (counter, _text(file.split("/")[-1], 30),
                                                    "<br/>".join(_text(b, 40) for b in body)))
            drawn.append(counter)
            counter += 1
        lines.append("  end")
    hidden = total_modules - counter
    if hidden > 0:
        lines.append('  more["+%d more modules"]:::external' % hidden)
    if data:
        lines.append('  store[("Data stores<br/>%s")]:::data' % _text(", ".join(data[:5]), 70))
    for n in drawn:
        lines.append('  client --> m%d' % n)
        if data:
            lines.append('  m%d --> store' % n)
    if hidden > 0:
        lines.append("  client --> more")
    count_by_method = {}
    for ep in endpoints:
        count_by_method[ep["method"]] = count_by_method.get(ep["method"], 0) + 1
    mix = ", ".join("%d %s" % (n, m) for m, n in sorted(count_by_method.items(), key=lambda kv: (-kv[1], kv[0])))
    points = ["%d endpoint(s) in %d file(s): %s." % (len(endpoints), total_modules, mix)]
    if len(by_service) > 1:
        points.append("%d service folders expose endpoints; each is drawn as its own group." % len(by_service))
    if data:
        points.append("All modules point at the data stores found in the repository, because per-endpoint data access is not traced.")
    return {
        "id": "backend", "title": "Backend API structure", "mmd": "\n".join(lines),
        "what": "The HTTP endpoints of the backend, grouped by service folder and source file, and the data stores behind them.",
        "read": "Each box is one source file with its endpoints. Arrows go from clients to the file that serves a route, and from the file to the data stores.",
        "points": points,
        "services": _keys_present(ctx, ("ingress", "compute_api", "compute_app", "compute_worker", "database", "cache")),
        "limits": "Found by reading route declarations. Routes registered dynamically, mounted sub-apps and GraphQL schemas may be missing. Data access is not traced per endpoint.",
    }


def _entity(name, used):
    base = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "entity"
    if base[0].isdigit():
        base = "t_" + base
    candidate, n = base, 2
    while candidate in used and used[candidate] != name:
        candidate, n = "%s_%d" % (base, n), n + 1
    used[candidate] = name
    return candidate


def _attr(text):
    text = re.sub(r"[^A-Za-z0-9_]", "_", str(text)).strip("_")
    return text if text and not text[0].isdigit() else "f_" + text


def data_schema(ctx):
    schemas = _structure(ctx).get("schemas") or {}
    tables = schemas.get("tables", [])
    if not tables:
        return None
    relations = schemas.get("relations", [])
    shown = tables[:MAX_ENTITIES]
    used, ids = {}, {}
    for table in shown:
        ids[table["name"]] = _entity(table["name"], used)
    lines = ["erDiagram"]
    truncated = False
    for table in shown:
        lines.append("  %s {" % ids[table["name"]])
        for column in table["columns"][:MAX_COLUMNS]:
            keys = " PK" if column.get("pk") else (" FK" if column.get("fk") else "")
            lines.append("    %s %s%s" % (_attr(column.get("type") or "text"), _attr(column["name"]), keys))
        if len(table["columns"]) > MAX_COLUMNS:
            truncated = True
        lines.append("  }")
    drawn = 0
    for rel in relations:
        if rel["from"] in ids and rel["to"] in ids:
            lines.append('  %s ||--o{ %s : "%s"' % (ids[rel["from"]], ids[rel["to"]], _text(rel["column"], 30)))
            drawn += 1
    sources = ", ".join(schemas.get("sources", []))
    points = ["%d table(s) or collection(s) read from %s." % (len(tables), sources)]
    if len(tables) > MAX_ENTITIES:
        points.append("Only the first %d are drawn." % MAX_ENTITIES)
    if truncated:
        points.append("Columns beyond %d per table are left out of the picture." % MAX_COLUMNS)
    points.append("%d relationship(s) were found from foreign keys and references." % drawn if drawn
                  else "No foreign keys were found, so the tables are drawn without links.")
    return {
        "id": "data-schema", "title": "Database schema", "mmd": "\n".join(lines),
        "what": "The tables or collections defined in the code, their columns and how they reference each other.",
        "read": "Each box is a table. PK marks a primary key and FK a foreign key. A line with a crow's foot points at the table that holds the foreign key.",
        "points": points,
        "services": [k for k in ctx.by_key if k.startswith("database") or k == "vector"],
        "limits": "Read from schema files and model classes (SQL, Prisma, SQLAlchemy, Django, TypeORM, Mongoose). Columns added only by migrations or at runtime are not shown.",
    }
