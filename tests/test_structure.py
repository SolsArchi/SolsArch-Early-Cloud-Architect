"""Front-end, back-end and schema discovery, and the diagrams drawn from them."""
import pytest

import ast_scanner
import diagram_sanitizer
import helpers
import mermaid_lint
import structure_diagrams
import structure_scanner as ss

w = helpers.write


class FakeCtx:
    """The parts of build_diagrams.Ctx that the structure makers use."""

    def __init__(self, scan, keys=()):
        self.scan = scan
        self.by_key = {k: {"short": k, "role": k, "reason": "", "evidence_text": ""} for k in keys}
        self.components = {c["name"]: c for c in scan.get("components", [])}

    def has(self, key):
        return key in self.by_key

    def comp_names(self, *names):
        return [n for n in names if n in self.components]


def structure_of(tmp_path):
    return ss.scan_structure(tmp_path)


def test_next_app_and_pages_routes_and_api_routes(tmp_path):
    w(tmp_path, "web/app/page.tsx", "x")
    w(tmp_path, "web/app/(marketing)/pricing/page.tsx", "x")
    w(tmp_path, "web/app/dashboard/[id]/page.tsx", "x")
    w(tmp_path, "web/app/api/users/route.ts", "export async function GET(){}\nexport async function POST(){}")
    w(tmp_path, "web/pages/about.tsx", "x")
    w(tmp_path, "web/pages/index.tsx", "x")
    w(tmp_path, "web/pages/api/hi.ts", "x")
    w(tmp_path, "web/pages/_app.tsx", "x")
    data = structure_of(tmp_path)
    paths = sorted(r["path"] for r in data["frontend"]["routes"])
    assert paths == ["/", "/", "/about", "/dashboard/[id]", "/pricing"]
    eps = {(e["method"], e["path"]) for e in data["backend"]["endpoints"]}
    assert {("GET", "/api/users"), ("POST", "/api/users"), ("ANY", "/api/hi")} <= eps


def test_react_router_components_and_calls(tmp_path):
    w(tmp_path, "src/App.jsx", 'import {Route} from "react-router-dom";\n<Route path="/settings" element={<S/>}/>\n')
    w(tmp_path, "src/components/Chart.jsx", "fetch(`/api/stats/${id}`); axios.post('/api/log'); fetch('https://x.io/a')")
    data = structure_of(tmp_path)["frontend"]
    assert [r["path"] for r in data["routes"]] == ["/settings"]
    assert [c["name"] for c in data["components"]] == ["Chart"]
    assert {(c["method"], c["target"]) for c in data["api_calls"]} == {
        ("GET", "/api/stats/${id}"), ("POST", "/api/log"), ("GET", "https://x.io/a")}


@pytest.mark.parametrize("name,files,expected", [
    ("fastapi", {"main.py": "@app.get('/a')\ndef a(): pass\n@router.post('/b')\ndef b(): pass\n"},
     {("GET", "/a"), ("POST", "/b")}),
    ("flask", {"app.py": "@app.route('/x', methods=['GET','POST'])\ndef x(): pass\n@bp.route('/y')\ndef y(): pass\n"},
     {("GET", "/x"), ("POST", "/x"), ("GET", "/y")}),
    ("django", {"urls.py": "urlpatterns=[path('items/', v), re_path(r'^old/$', v)]\n"},
     {("ANY", "/items"), ("ANY", "/old")}),
    ("express", {"server.js": "const app = require('express')();\napp.get('/h', f);\nrouter.delete('/u/:id', f);\n"},
     {("GET", "/h"), ("DELETE", "/u/:id")}),
    ("nest", {"c.ts": "import '@nestjs/common';\n@Controller('cats')\nclass C {\n @Get(':id')\n one(){}\n @Post()\n add(){}\n}\n"},
     {("GET", "/cats/:id"), ("POST", "/cats")}),
    ("spring", {"C.java": '@RequestMapping("/api")\nclass C {\n @GetMapping("/v")\n void v(){}\n}\n'}, {("GET", "/api/v")}),
    ("go", {"main.go": 'r.GET("/ping", h)\nhttp.HandleFunc("/raw", h)\n'}, {("GET", "/ping"), ("ANY", "/raw")}),
    ("rails", {"routes.rb": "get '/ok', to: 'a#b'\nresources :posts\n"}, {("GET", "/ok"), ("REST", "/posts")}),
    ("laravel", {"web.php": "Route::get('/home', f);\n"}, {("GET", "/home")}),
    ("axum", {"main.rs": 'Router::new().route("/r", get(h))\n'}, {("GET", "/r")}),
])
def test_backend_frameworks(tmp_path, name, files, expected):
    for rel, text in files.items():
        w(tmp_path, rel, text)
    eps = {(e["method"], e["path"]) for e in structure_of(tmp_path)["backend"]["endpoints"]}
    assert expected <= eps, (name, eps)


def test_fastapi_router_prefix(tmp_path):
    w(tmp_path, "r.py", "router = APIRouter(prefix='/orders')\n@router.get('/{id}')\ndef g(): pass\n")
    assert structure_of(tmp_path)["backend"]["endpoints"][0]["path"] == "/orders/{id}"


def test_monorepo_endpoints_carry_their_service_folder(tmp_path):
    w(tmp_path, "services/a/main.py", "@app.get('/a')\ndef a(): pass\n")
    w(tmp_path, "services/b/main.py", "@app.get('/b')\ndef b(): pass\n")
    assert {e["service"] for e in structure_of(tmp_path)["backend"]["endpoints"]} == {"services/a", "services/b"}


def test_schemas_from_every_source(tmp_path):
    w(tmp_path, "db/schema.sql", "CREATE TABLE posts (id serial PRIMARY KEY, author_id int REFERENCES users(id), body text);\n"
                                 "CREATE TABLE users (id serial PRIMARY KEY, email text);")
    w(tmp_path, "prisma/schema.prisma", "model Team {\n id Int @id\n members Member[]\n}\nmodel Member {\n id Int @id\n"
                                         " teamId Int\n team Team @relation(fields:[teamId], references:[id])\n}\n")
    w(tmp_path, "a/models.py", "from sqlalchemy import Column, Integer, ForeignKey\nclass O(Base):\n    __tablename__='orders'\n"
                               "    id = Column(Integer, primary_key=True)\n    user_id = Column(Integer, ForeignKey('users.id'))\n")
    w(tmp_path, "b/models.py", "from django.db import models\nclass Book(models.Model):\n    title = models.CharField(max_length=9)\n"
                               "    author = models.ForeignKey('Author', on_delete=models.CASCADE)\nclass Author(models.Model):\n"
                               "    name = models.CharField(max_length=9)\n")
    w(tmp_path, "c/e.ts", "@Entity()\nexport class Account {\n  @PrimaryGeneratedColumn()\n  id: number;\n"
                          "  @ManyToOne(() => Account)\n  parent: Account;\n}\n")
    w(tmp_path, "d/m.js", "const mongoose = require('mongoose');\nconst s = new mongoose.Schema({ title: String, "
                          "owner: { type: mongoose.Schema.Types.ObjectId, ref: 'users' } });\nmongoose.model('Post', s);\n")
    schemas = structure_of(tmp_path)["schemas"]
    by_name = {t["name"]: t for t in schemas["tables"]}
    assert {"posts", "users", "Team", "Member", "orders", "Book", "Author", "Account", "Post"} <= set(by_name)
    assert {"prisma", "sql", "sqlalchemy", "django", "typeorm", "mongoose"} <= set(schemas["sources"])
    pairs = {(r["from"], r["to"]) for r in schemas["relations"]}
    assert {("users", "posts"), ("Team", "Member"), ("users", "orders"), ("Author", "Book"), ("users", "Post")} <= pairs
    assert next(c for c in by_name["Member"]["columns"] if c["name"] == "teamId")["fk"] == "Team"
    assert next(c for c in by_name["posts"]["columns"] if c["name"] == "id")["pk"] is True


def test_tests_and_vendor_dirs_are_ignored(tmp_path):
    w(tmp_path, "tests/test_x.py", "@app.get('/hidden')\ndef a(): pass\n")
    w(tmp_path, "node_modules/x/index.js", "app.get('/dep', f)")
    assert structure_of(tmp_path)["backend"]["endpoints"] == []


def test_normalise_path_equates_parameter_styles():
    forms = ["/u/${id}", "/u/:id", "/u/{id}", "/u/<int:id>", "/u/42", "/u/[id]"]
    assert len({ss.normalise_path(f) for f in forms}) == 1


def make_ctx(tmp_path, keys=()):
    scan = {"components": [{"category": "framework", "name": "Next.js", "evidence": []},
                           {"category": "database", "name": "PostgreSQL", "evidence": []}],
            "structure": structure_of(tmp_path)}
    return FakeCtx(scan, keys)


def full_app(tmp_path):
    w(tmp_path, "web/app/page.tsx", "fetch('/api/users'); fetch('https://api.stripe.com/v1/x'); fetch('/nothing')")
    w(tmp_path, "web/components/Navbar.tsx", "axios.get('/api/users')")
    w(tmp_path, "api/main.py", "@app.get('/api/users')\ndef p(): pass\n")
    w(tmp_path, "api/models.py", "from sqlalchemy import Column, Integer, ForeignKey\nclass U(Base):\n    __tablename__='users'\n"
                                 "    id = Column(Integer, primary_key=True)\nclass O(Base):\n    __tablename__='orders'\n"
                                 "    id = Column(Integer, primary_key=True)\n    user_id = Column(Integer, ForeignKey('users.id'))\n")


def test_diagrams_are_valid_and_connected(tmp_path):
    full_app(tmp_path)
    ctx = make_ctx(tmp_path, ("compute_web",))
    front = structure_diagrams.frontend_structure(ctx)
    back = structure_diagrams.backend_structure(ctx)
    schema = structure_diagrams.data_schema(ctx)
    assert "GET /api/users" in front["mmd"] and "api.stripe.com" in front["mmd"] and "Other calls: /nothing" in front["mmd"]
    assert "main.py" in back["mmd"] and "PostgreSQL" in back["mmd"]
    assert schema["mmd"].startswith("erDiagram") and 'users ||--o{ orders : "user_id"' in schema["mmd"]
    for item in (front, back, schema):
        cleaned, dtype, issues = diagram_sanitizer.sanitize(item["mmd"])
        assert issues == [], (item["id"], issues)
        _, reported, remaining = mermaid_lint.sanitize_text("```mermaid\n%s\n```" % item["mmd"], fix=False)
        assert remaining == 0, (item["id"], reported)
        for key in ("id", "title", "what", "read", "points", "services", "limits"):
            assert item[key] is not None


def test_nothing_to_show_draws_nothing(tmp_path):
    w(tmp_path, "README.md", "x")
    ctx = make_ctx(tmp_path)
    assert structure_diagrams.frontend_structure(ctx) is None
    assert structure_diagrams.backend_structure(ctx) is None
    assert structure_diagrams.data_schema(ctx) is None


def test_large_inputs_are_capped_and_stay_valid(tmp_path):
    for i in range(40):
        w(tmp_path, "api/m%d.py" % i, "".join("@app.get('/r%d_%d')\ndef f%d(): pass\n" % (i, j, j) for j in range(12)))
        w(tmp_path, "db/t%d.sql" % i, "CREATE TABLE t%d (%s);" % (i, ", ".join("c%d int" % j for j in range(30))))
        w(tmp_path, "web/app/p%d/page.tsx" % i, "x")
    ctx = make_ctx(tmp_path)
    for maker in (structure_diagrams.frontend_structure, structure_diagrams.backend_structure,
                  structure_diagrams.data_schema):
        item = maker(ctx)
        assert item and len(item["mmd"]) < 20000
        assert diagram_sanitizer.sanitize(item["mmd"])[2] == []


def test_awkward_names_do_not_break_the_diagram(tmp_path):
    w(tmp_path, "db/s.sql", 'CREATE TABLE "order-items" (id int PRIMARY KEY, "weird col" int, 9lives text);')
    w(tmp_path, "a/main.py", "@app.get('/q?x=1&y=<b>')\ndef f(): pass\n")
    ctx = make_ctx(tmp_path)
    for maker in (structure_diagrams.backend_structure, structure_diagrams.data_schema):
        assert diagram_sanitizer.sanitize(maker(ctx)["mmd"])[2] == []


def test_pipeline_draws_the_new_diagrams_and_verifies(tmp_path):
    full_app(tmp_path)
    w(tmp_path, "web/package.json", '{"dependencies": {"next": "14", "react": "18"}}')
    w(tmp_path, "api/requirements.txt", "fastapi\nsqlalchemy\npsycopg2-binary\n")
    out = helpers.pipeline(str(tmp_path))
    assert out["verification"] == "passed"
    names = sorted(p.name for p in (tmp_path / "docs" / "diagrams").glob("*.mmd"))
    assert any("frontend" in n for n in names) and any("backend" in n for n in names) and any("data-schema" in n for n in names)
    arch = (tmp_path / "docs" / "solsarch-architecture.md").read_text(encoding="utf-8")
    assert "erDiagram" in arch and "Frontend structure" in arch and "Backend API structure" in arch


def test_scan_json_contains_structure(tmp_path):
    full_app(tmp_path)
    assert "structure" in ast_scanner.scan(tmp_path)


def test_cli(tmp_path):
    full_app(tmp_path)
    out = tmp_path / "s.json"
    assert ss.main([str(tmp_path), "--output", str(out)]) == 0 and out.is_file()
    assert ss.main([str(tmp_path / "missing")]) == 2
