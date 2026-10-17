#!/usr/bin/env python3
"""Find the shape of an application: front-end routes, back-end endpoints and database schemas.

Standard library only. This reads source files with fixed patterns; it does not run the code. The result feeds
the front-end, back-end and schema diagrams, so the same repository always gives the same diagrams.

Usage: python structure_scanner.py REPO [--output structure.json]

Front end: Next.js (app and pages routers), React Router, Vue Router, components, fetch/axios calls.
Back end: FastAPI, Flask, Django, Express/Fastify/Koa, NestJS, Spring, Gin/Echo/Chi, Rails, Laravel, Axum, Next.js API routes.
Schemas: SQL (CREATE TABLE), Prisma, SQLAlchemy, Django models, TypeORM entities, Mongoose schemas.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

MAX_FILE_BYTES = 400_000
MAX_FILES = 8000
MAX_ENDPOINTS = 300
MAX_ROUTES = 120
MAX_CALLS = 120
MAX_COMPONENTS = 60
MAX_TABLES = 80
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", "target", ".next", ".nuxt", ".svelte-kit", ".terraform",
    ".gradle", ".idea", ".vscode", "vendor", "coverage", "site-packages", ".solsarch", ".cache", ".turbo", "out",
    ".claude", ".codex", ".agents", ".cursor", "results", "runs", "backups", "logs", "output", "outputs",
    "generated", "tmp", "temp", "tests", "test", "__tests__", "spec", "specs", "e2e", "cypress", "testdata",
    "solsarch-early-cloud-architect",
}
FRONT_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".mjs"}
BACK_EXT = {".py", ".js", ".ts", ".mjs", ".java", ".kt", ".go", ".rb", ".php", ".rs"}
METHODS = ("get", "post", "put", "delete", "patch")
TEST_FILE_RE = re.compile(r"(^test_.*\.py$)|(_test\.(py|go)$)|(\.(test|spec)\.[jt]sx?$)|(^conftest\.py$)")
GROUP_PARENTS = {"services", "apps", "packages", "microservices", "projects", "modules", "libs"}

NEXT_PAGE_RE = re.compile(r"^(?:.*?/)?(?:src/)?app/(.*/)?page\.(?:tsx|jsx|js|ts)$")
NEXT_ROUTE_RE = re.compile(r"^(?:.*?/)?(?:src/)?app/(.*/)?route\.(?:ts|js)$")
NEXT_PAGES_RE = re.compile(r"^(?:.*?/)?(?:src/)?pages/(.*)\.(?:tsx|jsx|js|ts)$")
REACT_ROUTE_RE = re.compile(r"""<Route\b[^>]*?\bpath\s*=\s*[{]?["'`]([^"'`]+)["'`]""")
ROUTE_OBJECT_RE = re.compile(r"""\bpath\s*:\s*["'`](/[^"'`]*)["'`]""")
FETCH_RE = re.compile(r"""\bfetch\(\s*([`"'])((?:/|https?://)[^`"']*)\1""")
AXIOS_RE = re.compile(r"""\baxios(?:\.(get|post|put|delete|patch))?\(\s*([`"'])((?:/|https?://)[^`"']*)\2""")
HTTP_CLIENT_RE = re.compile(r"""\b(?:api|http|client|request)\.(get|post|put|delete|patch)\(\s*([`"'])(/[^`"']*)\2""")
COMPONENT_DIR_RE = re.compile(r"(?:^|/)(?:components?|ui|widgets)/")

FASTAPI_RE = re.compile(r"""@(\w+)\.(get|post|put|delete|patch)\(\s*["']([^"']*)["']""")
ROUTER_PREFIX_RE = re.compile(r"""(\w+)\s*=\s*APIRouter\([^)]*prefix\s*=\s*["']([^"']*)["']""")
FLASK_RE = re.compile(r"""@(\w+)\.route\(\s*["']([^"']+)["'](?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?""")
DJANGO_RE = re.compile(r"""\b(?:re_)?path\(\s*r?["']([^"']*)["']""")
EXPRESS_RE = re.compile(r"""\b(\w+)\.(get|post|put|delete|patch)\(\s*([`"'])(/[^`"']*)\3""")
NEST_CONTROLLER_RE = re.compile(r"""@Controller\(\s*(?:["']([^"']*)["'])?\s*\)""")
NEST_METHOD_RE = re.compile(r"""@(Get|Post|Put|Delete|Patch)\(\s*(?:["']([^"']*)["'])?\s*\)""")
SPRING_CLASS_RE = re.compile(r"""@RequestMapping\(\s*(?:value\s*=\s*)?["']([^"']*)["']""")
SPRING_METHOD_RE = re.compile(r"""@(Get|Post|Put|Delete|Patch)Mapping\(\s*(?:value\s*=\s*)?(?:["']([^"']*)["'])?""")
GO_RE = re.compile(r"""\.(GET|POST|PUT|DELETE|PATCH)\(\s*"([^"]+)\"""")
GO_HANDLE_RE = re.compile(r"""\bHandleFunc\(\s*"([^"]+)\"""")
RAILS_RE = re.compile(r"""^\s*(get|post|put|patch|delete)\s+['"]([^'"]+)['"]""", re.M)
RAILS_RESOURCES_RE = re.compile(r"""^\s*resources?\s+:(\w+)""", re.M)
LARAVEL_RE = re.compile(r"""Route::(get|post|put|delete|patch)\(\s*['"]([^'"]+)['"]""")
AXUM_RE = re.compile(r"""\.route\(\s*"([^"]+)"\s*,\s*(get|post|put|delete|patch)""")


def _skip_dir(name):
    return name in SKIP_DIRS or name.lower() in SKIP_DIRS


def _read(path):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


def _join(prefix, path):
    prefix, path = (prefix or "").strip("/"), (path or "").strip()
    if path.startswith(("http://", "https://")):
        return path
    path = path.strip("/")
    parts = [p for p in (prefix, path) if p]
    return "/" + "/".join(parts)


def normalise_path(path):
    """Collapse parameters so /users/${id}, /users/:id and /users/{id} compare equal."""
    path = re.sub(r"\$\{[^}]*\}", ":param", path)
    path = re.sub(r"\{[^}/]*\}", ":param", path)
    path = re.sub(r"<[^>/]*>", ":param", path)
    path = re.sub(r"/:[A-Za-z_][A-Za-z0-9_]*", "/:param", path)
    path = re.sub(r"\[\.\.\.\w+\]|\[\w+\]", ":param", path)
    path = re.sub(r"/\d+(?=/|$)", "/:param", path.split("?")[0])
    return path.rstrip("/") or "/"


def service_of(rel):
    parts = rel.split("/")
    if len(parts) <= 1:
        return "."
    if parts[0] in GROUP_PARENTS and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


# ------------------------------------------------------------------ front end
def next_route(rel_dir):
    parts = [p for p in (rel_dir or "").strip("/").split("/") if p and not (p.startswith("(") and p.endswith(")"))
             and not p.startswith("@")]
    return "/" + "/".join(parts)


def scan_frontend_file(rel, text, out):
    match = NEXT_PAGE_RE.match(rel)
    if match:
        out["routes"].append({"path": next_route(match.group(1)), "file": rel, "kind": "next-app"})
    match = NEXT_PAGES_RE.match(rel)
    if match and not match.group(1).startswith(("api/", "_")) and match.group(1) != "api":
        route = match.group(1)
        route = route[:-len("index")].rstrip("/") if route.endswith("index") else route
        out["routes"].append({"path": "/" + route.strip("/") if route.strip("/") else "/", "file": rel, "kind": "next-pages"})
    if "react-router" in text or "createBrowserRouter" in text or "<Routes" in text:
        for m in REACT_ROUTE_RE.finditer(text):
            out["routes"].append({"path": m.group(1), "file": rel, "kind": "react-router"})
        for m in ROUTE_OBJECT_RE.finditer(text):
            out["routes"].append({"path": m.group(1), "file": rel, "kind": "react-router"})
    elif "vue-router" in text:
        for m in ROUTE_OBJECT_RE.finditer(text):
            out["routes"].append({"path": m.group(1), "file": rel, "kind": "vue-router"})
    if COMPONENT_DIR_RE.search(rel) and Path(rel).suffix in (".jsx", ".tsx", ".vue", ".svelte", ".js", ".ts"):
        stem = Path(rel).stem
        if stem and stem[0].isalpha() and stem.lower() not in ("index", "types", "utils", "styles"):
            out["components"].append({"name": stem, "file": rel})
    for regex, target_group, method_group in ((FETCH_RE, 2, None), (AXIOS_RE, 3, 1), (HTTP_CLIENT_RE, 3, 1)):
        for m in regex.finditer(text):
            method = (m.group(method_group) or "get").upper() if method_group else "GET"
            out["api_calls"].append({"target": m.group(target_group), "method": method, "file": rel,
                                     "line": _line_of(text, m.start())})


# ------------------------------------------------------------------- back end
def add_endpoint(out, method, path, rel, line, framework):
    if len(out["endpoints"]) < MAX_ENDPOINTS:
        out["endpoints"].append({"method": method.upper(), "path": path or "/", "file": rel, "line": line,
                                 "framework": framework, "service": service_of(rel)})


def scan_backend_file(rel, text, out):
    ext = Path(rel).suffix
    name = Path(rel).name
    match = NEXT_ROUTE_RE.match(rel)
    if match:
        path = next_route(match.group(1))
        exported = re.findall(r"export\s+(?:async\s+)?function\s+(GET|POST|PUT|DELETE|PATCH)\b", text) or ["GET"]
        for method in exported:
            add_endpoint(out, method, path, rel, 1, "Next.js")
        return
    match = NEXT_PAGES_RE.match(rel)
    if match and match.group(1).startswith("api"):
        add_endpoint(out, "ANY", "/" + match.group(1).replace("/index", ""), rel, 1, "Next.js")
        return
    if ext == ".py":
        prefixes = {m.group(1): m.group(2) for m in ROUTER_PREFIX_RE.finditer(text)}
        for m in FASTAPI_RE.finditer(text):
            add_endpoint(out, m.group(2), _join(prefixes.get(m.group(1)), m.group(3)), rel, _line_of(text, m.start()),
                         "FastAPI")
        for m in FLASK_RE.finditer(text):
            methods = re.findall(r"[A-Za-z]+", m.group(3) or "") or ["GET"]
            for method in methods:
                add_endpoint(out, method, m.group(2), rel, _line_of(text, m.start()), "Flask")
        if name == "urls.py":
            for m in DJANGO_RE.finditer(text):
                add_endpoint(out, "ANY", "/" + m.group(1).strip("^$").strip("/"), rel, _line_of(text, m.start()), "Django")
        return
    if ext in (".js", ".ts", ".mjs"):
        controller = NEST_CONTROLLER_RE.search(text)
        if controller is not None and "@nestjs" in text:
            for m in NEST_METHOD_RE.finditer(text):
                add_endpoint(out, m.group(1), _join(controller.group(1), m.group(2)), rel, _line_of(text, m.start()),
                             "NestJS")
            return
        if re.search(r"express|fastify|koa|hono|router", text, re.I):
            for m in EXPRESS_RE.finditer(text):
                if m.group(1) in ("axios", "api", "http", "client", "request", "fetch", "supertest", "cy"):
                    continue
                add_endpoint(out, m.group(2), m.group(4), rel, _line_of(text, m.start()), "Express/Fastify")
        return
    if ext in (".java", ".kt"):
        prefix = SPRING_CLASS_RE.search(text)
        for m in SPRING_METHOD_RE.finditer(text):
            add_endpoint(out, m.group(1), _join(prefix.group(1) if prefix else "", m.group(2)), rel,
                         _line_of(text, m.start()), "Spring")
        return
    if ext == ".go":
        for m in GO_RE.finditer(text):
            add_endpoint(out, m.group(1), m.group(2), rel, _line_of(text, m.start()), "Go")
        for m in GO_HANDLE_RE.finditer(text):
            add_endpoint(out, "ANY", m.group(1), rel, _line_of(text, m.start()), "Go net/http")
        return
    if ext == ".rb" and name == "routes.rb":
        for m in RAILS_RE.finditer(text):
            add_endpoint(out, m.group(1), "/" + m.group(2).strip("/"), rel, _line_of(text, m.start()), "Rails")
        for m in RAILS_RESOURCES_RE.finditer(text):
            add_endpoint(out, "REST", "/" + m.group(1), rel, _line_of(text, m.start()), "Rails")
        return
    if ext == ".php":
        for m in LARAVEL_RE.finditer(text):
            add_endpoint(out, m.group(1), "/" + m.group(2).strip("/"), rel, _line_of(text, m.start()), "Laravel")
        return
    if ext == ".rs":
        for m in AXUM_RE.finditer(text):
            add_endpoint(out, m.group(2), m.group(1), rel, _line_of(text, m.start()), "Axum")


# ------------------------------------------------------------------- schemas
def clean_type(raw):
    base = re.split(r"[\s(\[<]", (raw or "").strip().strip(","), maxsplit=1)[0]
    base = re.sub(r"[^A-Za-z0-9_]", "", base).lower()
    return base or "text"


def clean_ident(raw):
    ident = re.sub(r"[^A-Za-z0-9_]", "_", str(raw)).strip("_")
    if not ident or ident[0].isdigit():
        ident = "t_" + ident
    return ident


def split_top_level(body):
    parts, depth, current = [], 0, []
    for ch in body:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def balanced(text, open_index, opener="(", closer=")"):
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


SQL_TABLE_RE = re.compile(r"""CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\[]?([\w.\-]+)[`"\]]?\s*\(""", re.I)
SQL_FK_RE = re.compile(r"""REFERENCES\s+[`"\[]?([\w.]+)[`"\]]?\s*(?:\(\s*[`"\[]?(\w+))?""", re.I)
SQL_TABLE_FK_RE = re.compile(r"""FOREIGN\s+KEY\s*\(\s*[`"]?(\w+)[`"]?\s*\)\s*REFERENCES\s+[`"\[]?([\w.]+)""", re.I)
SQL_TABLE_PK_RE = re.compile(r"""PRIMARY\s+KEY\s*\(([^)]*)\)""", re.I)
SQL_IDENT_RE = re.compile(r"""^(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\S+))""")
SQL_SKIP = ("constraint", "primary", "foreign", "unique", "key", "index", "check", "exclude", "fulltext", "like")


def scan_sql(rel, text, tables):
    for m in SQL_TABLE_RE.finditer(text):
        end = balanced(text, m.end() - 1)
        if end < 0:
            continue
        table = {"name": m.group(1).split(".")[-1], "source": "sql", "file": rel, "columns": []}
        pks, fks = set(), {}
        for part in split_top_level(text[m.end():end]):
            part = " ".join(part.split())
            if not part:
                continue
            first = part.split(" ", 1)[0].lower()
            if first in SQL_SKIP:
                pk = SQL_TABLE_PK_RE.search(part)
                if pk:
                    pks.update(c.strip().strip('`"') for c in pk.group(1).split(","))
                fk = SQL_TABLE_FK_RE.search(part)
                if fk:
                    fks[fk.group(1)] = fk.group(2).split(".")[-1]
                continue
            ident = SQL_IDENT_RE.match(part)
            name = next(g for g in ident.groups() if g)
            rest = part[ident.end():].strip()
            column = {"name": name, "type": clean_type(rest), "pk": bool(re.search(r"PRIMARY\s+KEY", rest, re.I)),
                      "fk": None}
            ref = SQL_FK_RE.search(rest)
            if ref:
                column["fk"] = ref.group(1).split(".")[-1]
            table["columns"].append(column)
        for column in table["columns"]:
            if column["name"] in pks:
                column["pk"] = True
            if column["name"] in fks:
                column["fk"] = fks[column["name"]]
        tables.append(table)


PRISMA_MODEL_RE = re.compile(r"^model\s+(\w+)\s*\{(.*?)^\}", re.M | re.S)
PRISMA_SCALARS = {"string", "int", "bigint", "float", "decimal", "boolean", "datetime", "json", "bytes"}


def scan_prisma(rel, text, tables):
    models = {m.group(1): m.group(2) for m in PRISMA_MODEL_RE.finditer(text)}
    for name, body in models.items():
        table = {"name": name, "source": "prisma", "file": rel, "columns": []}
        for line in body.splitlines():
            line = line.split("//")[0].strip()
            if not line or line.startswith("@@"):
                continue
            m = re.match(r"(\w+)\s+(\w+)(\[\])?(\?)?(.*)", line)
            if not m:
                continue
            fname, ftype, is_list, _, attrs = m.groups()
            if ftype in models and ftype != name or (ftype == name and "@relation" in attrs):
                rel_fields = re.search(r"fields:\s*\[([^\]]*)\]", attrs)
                if rel_fields and not is_list:
                    for local in (c.strip() for c in rel_fields.group(1).split(",")):
                        for column in table["columns"]:
                            if column["name"] == local:
                                column["fk"] = ftype
                continue
            table["columns"].append({"name": fname, "type": ftype.lower(), "pk": "@id" in attrs, "fk": None})
        tables.append(table)


SA_CLASS_RE = re.compile(r"^class\s+(\w+)\s*\(([^)]*)\)\s*:", re.M)
SA_COLUMN_RE = re.compile(r"^\s+(\w+)\s*(?::\s*[^=\n]+)?=\s*(?:\w+\.)?(?:mapped_)?[Cc]olumn\((.*)\)\s*$", re.M)
SA_FK_RE = re.compile(r"""ForeignKey\(\s*["']([\w.]+)["']""")
DJ_FIELD_RE = re.compile(r"^\s+(\w+)\s*=\s*models\.(\w+)\((.*)\)\s*$", re.M)


def class_bodies(text):
    matches = list(SA_CLASS_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield m.group(1), m.group(2), text[m.end():end]


def scan_python_models(rel, text, tables):
    if "sqlalchemy" in text.lower() or "db.Model" in text:
        for cname, bases, body in class_bodies(text):
            tablename = re.search(r"""__tablename__\s*=\s*["'](\w+)["']""", body)
            if not tablename:
                continue
            table = {"name": tablename.group(1), "source": "sqlalchemy", "file": rel, "columns": []}
            for m in SA_COLUMN_RE.finditer(body):
                args = m.group(2)
                fk = SA_FK_RE.search(args)
                kind = re.match(r"\s*(?:\w+\.)?(\w+)", args)
                ctype = kind.group(1) if kind and kind.group(1) != "ForeignKey" else "int"
                table["columns"].append({"name": m.group(1), "type": clean_type(ctype),
                                         "pk": "primary_key=True" in args.replace(" ", ""),
                                         "fk": fk.group(1).split(".")[0] if fk else None})
            if table["columns"]:
                tables.append(table)
    if "models.Model" in text:
        for cname, bases, body in class_bodies(text):
            if "models.Model" not in bases:
                continue
            table = {"name": cname, "source": "django", "file": rel,
                     "columns": [{"name": "id", "type": "bigint", "pk": True, "fk": None}]}
            for m in DJ_FIELD_RE.finditer(body):
                ftype, args = m.group(2), m.group(3)
                if ftype in ("ForeignKey", "OneToOneField"):
                    target = re.match(r"""\s*["']?([\w.]+)["']?""", args)
                    name = target.group(1).split(".")[-1] if target else None
                    table["columns"].append({"name": m.group(1) + "_id", "type": "bigint", "pk": False, "fk": name})
                elif ftype == "ManyToManyField":
                    continue
                else:
                    table["columns"].append({"name": m.group(1), "type": clean_type(ftype.replace("Field", "")),
                                             "pk": "primary_key=True" in args.replace(" ", ""), "fk": None})
            tables.append(table)


TYPEORM_CLASS_RE = re.compile(r"@Entity\([^)]*\)\s*(?:export\s+)?class\s+(\w+)\s*(?:extends\s+\w+\s*)?\{(.*?)^\}", re.M | re.S)


def scan_typeorm(rel, text, tables):
    for m in TYPEORM_CLASS_RE.finditer(text):
        table = {"name": m.group(1), "source": "typeorm", "file": rel, "columns": []}
        lines = m.group(2).splitlines()
        for i, line in enumerate(lines):
            decorator = re.search(r"@(PrimaryGeneratedColumn|PrimaryColumn|Column|ManyToOne|OneToOne)\((.*)\)", line)
            if not decorator:
                continue
            following = next((ln for ln in lines[i + 1:i + 3] if re.match(r"\s*\w+!?\??\s*:", ln)), None)
            if not following:
                continue
            fname, ftype = re.match(r"\s*(\w+)!?\??\s*:\s*([\w\[\]]+)", following).groups()
            kind = decorator.group(1)
            if kind in ("ManyToOne", "OneToOne"):
                target = re.search(r"=>\s*(\w+)", decorator.group(2))
                table["columns"].append({"name": fname + "Id", "type": "int", "pk": False,
                                         "fk": target.group(1) if target else None})
            else:
                table["columns"].append({"name": fname, "type": clean_type(ftype), "pk": kind.startswith("Primary"),
                                         "fk": None})
        if table["columns"]:
            tables.append(table)


MONGOOSE_SCHEMA_RE = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*new\s+(?:mongoose\.)?Schema\(\s*\{")
MONGOOSE_MODEL_RE = re.compile(r"""\.model\(\s*["'](\w+)["']\s*,\s*(\w+)""")


def scan_mongoose(rel, text, tables):
    names = {m.group(2): m.group(1) for m in MONGOOSE_MODEL_RE.finditer(text)}
    for m in MONGOOSE_SCHEMA_RE.finditer(text):
        end = balanced(text, m.end() - 1, "{", "}")
        if end < 0:
            continue
        table = {"name": names.get(m.group(1), m.group(1)), "source": "mongoose", "file": rel,
                 "columns": [{"name": "_id", "type": "objectid", "pk": True, "fk": None}]}
        for part in split_top_level(text[m.end():end].replace("{", "(").replace("}", ")")):
            field = re.match(r"\s*(\w+)\s*:\s*(.*)", part, re.S)
            if not field:
                continue
            body = field.group(2)
            ref = re.search(r"""ref\s*:\s*["'](\w+)["']""", body)
            kind = re.match(r"[\s(\[]*(?:type\s*:\s*)?[\s(\[]*(?:mongoose\.)?(?:Schema\.Types\.)?(\w+)", body)
            table["columns"].append({"name": field.group(1), "type": clean_type(kind.group(1) if kind else "mixed"),
                                     "pk": False, "fk": ref.group(1) if ref else None})
        tables.append(table)


def finalise_schemas(tables):
    priority = {"prisma": 0, "sqlalchemy": 1, "django": 1, "typeorm": 1, "mongoose": 1, "sql": 2}
    tables = sorted(tables, key=lambda t: priority.get(t["source"], 3))
    seen, unique = set(), []
    for table in tables:
        key = table["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(table)
    names = {t["name"].lower(): t["name"] for t in unique}
    relations = []
    for table in unique:
        for column in table["columns"]:
            target = column.get("fk")
            if not target:
                continue
            resolved = names.get(target.lower()) or names.get(target.lower().rstrip("s")) or names.get(target.lower() + "s")
            if resolved and resolved != table["name"]:
                relations.append({"from": resolved, "to": table["name"], "column": column["name"]})
            elif resolved:
                relations.append({"from": resolved, "to": table["name"], "column": column["name"]})
    unique_relations = []
    for r in relations:
        if r not in unique_relations:
            unique_relations.append(r)
    return unique[:MAX_TABLES], unique_relations


# ----------------------------------------------------------------------- walk
def scan_structure(root):
    root = Path(root).resolve()
    out_front = {"routes": [], "components": [], "api_calls": []}
    out_back = {"endpoints": []}
    tables = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d) and not os.path.islink(os.path.join(dirpath, d)))
        for filename in sorted(filenames):
            if scanned >= MAX_FILES:
                break
            path = Path(dirpath) / filename
            ext = path.suffix.lower()
            if path.is_symlink() or TEST_FILE_RE.search(filename.lower()):
                continue
            wanted = ext in FRONT_EXT or ext in BACK_EXT or ext in (".sql", ".prisma") or filename == "routes.rb"
            if not wanted:
                continue
            text = _read(path)
            if text is None:
                continue
            scanned += 1
            rel = path.relative_to(root).as_posix()
            if ext in FRONT_EXT:
                scan_frontend_file(rel, text, out_front)
            if ext in BACK_EXT or filename == "routes.rb":
                scan_backend_file(rel, text, out_back)
            if ext == ".sql":
                scan_sql(rel, text, tables)
            elif ext == ".prisma":
                scan_prisma(rel, text, tables)
            elif ext == ".py":
                scan_python_models(rel, text, tables)
            elif ext in (".ts", ".js", ".mjs"):
                if "@Entity" in text:
                    scan_typeorm(rel, text, tables)
                if "Schema(" in text and "mongoose" in text:
                    scan_mongoose(rel, text, tables)
    seen_routes, routes = set(), []
    for route in out_front["routes"]:
        key = (route["path"], route["file"])
        if key not in seen_routes and len(routes) < MAX_ROUTES:
            seen_routes.add(key)
            routes.append(route)
    out_front["routes"] = sorted(routes, key=lambda r: (r["path"], r["file"]))
    out_front["components"] = sorted(out_front["components"], key=lambda c: c["name"])[:MAX_COMPONENTS]
    out_front["api_calls"] = out_front["api_calls"][:MAX_CALLS]
    seen_eps, endpoints = set(), []
    for ep in out_back["endpoints"]:
        key = (ep["method"], ep["path"], ep["file"])
        if key not in seen_eps:
            seen_eps.add(key)
            endpoints.append(ep)
    out_back["endpoints"] = sorted(endpoints, key=lambda e: (e["service"], e["file"], e["path"], e["method"]))
    schema_tables, relations = finalise_schemas(tables)
    return {"frontend": out_front, "backend": out_back,
            "schemas": {"tables": schema_tables, "relations": relations,
                        "sources": sorted({t["source"] for t in schema_tables})}}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Find front-end routes, back-end endpoints and database schemas.")
    ap.add_argument("repo")
    ap.add_argument("--output")
    args = ap.parse_args(argv)
    if not os.path.isdir(args.repo):
        sys.stderr.write("error: %s is not a directory" % args.repo + chr(10))
        return 2
    text = json.dumps(scan_structure(args.repo), indent=2, ensure_ascii=True) + chr(10)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
