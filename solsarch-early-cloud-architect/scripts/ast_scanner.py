#!/usr/bin/env python3
"""SolsArch repository evidence scanner.

Walks a repository and prints one JSON document describing what the code needs
from the cloud: frameworks, databases, queues, caches, containers, IaC, and the
pre-deployment security findings SEC-001 to SEC-005. Every item carries the
file and line that proves it. Secret values are never printed.

Standard library only. Usage:
    python ast_scanner.py [PATH] [--pretty] [--max-files N]
"""

import argparse
import ast
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import structure_scanner  # noqa: E402

SCANNER_VERSION = "1.0.0"
DEFAULT_MAX_FILES = 6000
MAX_FILE_BYTES = 1_000_000
MAX_AST_BYTES = 400_000
MAX_EVIDENCE = 5
MAX_TEST_EVIDENCE = 2
MAX_SIGNAL_EVIDENCE = 20
IMPORT_LINE_RE = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\s|const\s+.*require\()")
COMMENT_PREFIXES = ("#", "//", "/*", "*", "--", "<!--")
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs", "fixtures", "testdata", "e2e", "cypress"}
# Generated output holds duplicated or deliberately bad config and caused false blockers on real repos.
GENERATED_DIRS = {"results", "runs", "backups", "logs", "output", "outputs", "generated", "tmp", "temp"}
TEST_SUFFIXES = ("_test.py", "_test.go", ".test.js", ".test.ts", ".test.tsx", ".test.jsx",
                 ".spec.js", ".spec.ts", ".spec.tsx")

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
    ".next", ".nuxt", ".svelte-kit", ".terraform", ".gradle", ".idea", ".vscode",
    "vendor", "coverage", "site-packages", ".solsarch", ".cache", ".turbo", "out",
    ".claude", ".codex", ".agents", ".cursor",
}
LOCK_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock",
    "composer.lock", "cargo.lock", "gemfile.lock", "go.sum", "bun.lockb", "uv.lock",
}
TEXT_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".yml", ".yaml",
    ".toml", ".tf", ".tfvars", ".hcl", ".conf", ".ini", ".cfg", ".prisma", ".sql",
    ".go", ".rb", ".java", ".kt", ".php", ".rs", ".sh", ".env", ".xml", ".gradle",
    ".kts", ".properties", ".txt",
}
TEXT_NAMES = {"gemfile", "pipfile", "procfile", "go.mod"}
LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go",
    ".rb": "ruby", ".java": "java", ".kt": "kotlin", ".php": "php", ".rs": "rust",
}
DEPLOY_CONFIG_NAMES = {
    "vercel.json": "Vercel", "netlify.toml": "Netlify", "fly.toml": "Fly.io",
    "render.yaml": "Render", "app.yaml": "Google App Engine",
    "cloudbuild.yaml": "Google Cloud Build", "serverless.yml": "Serverless Framework",
    "serverless.yaml": "Serverless Framework", "template.yaml": "AWS SAM / CloudFormation",
    "buildspec.yml": "AWS CodeBuild", "azure-pipelines.yml": "Azure Pipelines",
    "firebase.json": "Firebase", "wrangler.toml": "Cloudflare Workers",
    "procfile": "Heroku-style Procfile", "railway.json": "Railway", "railway.toml": "Railway",
}

# ---------------------------------------------------------------- dependency rules
# token (normalised package / module name) -> (category, display name)
DEP_EXACT = {}
DEP_PREFIX = []


def _add(category, name, *tokens):
    for token in tokens:
        if token.endswith("*"):
            DEP_PREFIX.append((token[:-1], category, name))
        else:
            DEP_EXACT[token] = (category, name)


_add("framework", "Next.js", "next")
_add("framework", "React", "react")
_add("framework", "Vue", "vue")
_add("framework", "Express", "express")
_add("framework", "NestJS", "@nestjs/core", "@nestjs/common")
_add("framework", "FastAPI", "fastapi")
_add("framework", "Django", "django")
_add("framework", "Flask", "flask")
_add("framework", "Ruby on Rails", "rails")
_add("framework", "Spring Boot", "spring-boot-starter", "spring-boot-starter-web",
     "spring-boot-starter-webflux")
_add("framework", "Laravel", "laravel/framework")
_add("framework", "Gin", "github.com/gin-gonic/gin")
_add("framework", "Fastify", "fastify")
_add("framework", "Sinatra", "sinatra")
_add("framework", "Axum", "axum")
_add("framework", "Actix Web", "actix-web")
_add("database", "PostgreSQL", "pg", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg",
     "postgres", "postgresql", "github.com/lib/pq", "github.com/jackc/pgx*")
_add("database", "MySQL", "mysql", "mysql2", "pymysql", "mysqlclient",
     "mysql-connector-python", "mysql-connector-java")
_add("database", "MongoDB", "mongodb", "mongoose", "pymongo", "motor")
_add("database", "SQLite", "sqlite3", "better-sqlite3", "aiosqlite", "sqlite-jdbc", "rusqlite")
_add("orm", "Prisma", "prisma", "@prisma/client")
_add("orm", "SQLAlchemy", "sqlalchemy")
_add("orm", "TypeORM", "typeorm")
_add("orm", "Sequelize", "sequelize")
_add("orm", "Drizzle", "drizzle-orm")
_add("orm", "GORM", "gorm.io/gorm")
_add("vector", "pgvector", "pgvector")
_add("vector", "ChromaDB", "chromadb")
_add("vector", "Pinecone", "pinecone", "pinecone-client", "@pinecone-database/pinecone")
_add("vector", "Qdrant", "qdrant-client", "@qdrant/js-client-rest")
_add("vector", "Weaviate", "weaviate-client")
_add("vector", "FAISS", "faiss-cpu", "faiss-gpu")
_add("vector", "LanceDB", "lancedb", "@lancedb/lancedb")
_add("search", "Meilisearch", "meilisearch")
_add("search", "Elasticsearch", "elasticsearch", "@elastic/elasticsearch")
_add("search", "OpenSearch", "opensearch-py", "@opensearch-project/opensearch")
_add("queue", "BullMQ", "bullmq", "bull")
_add("queue", "Celery", "celery")
_add("queue", "Kafka", "kafkajs", "confluent-kafka", "aiokafka")
_add("queue", "RabbitMQ", "pika", "amqplib")
_add("queue", "RQ", "rq")
_add("queue", "Dramatiq", "dramatiq")
_add("queue", "Sidekiq", "sidekiq")
_add("cache", "Redis", "redis", "ioredis", "aioredis", "github.com/redis/go-redis*",
     "github.com/go-redis/redis*")
_add("cache", "Memcached", "pymemcache", "memcached")
_add("ai", "OpenAI SDK", "openai")
_add("ai", "Anthropic SDK", "anthropic", "@anthropic-ai/sdk")
_add("ai", "LangChain", "langchain", "langchain-core", "@langchain/core")
_add("ai", "LlamaIndex", "llama-index")
_add("ai", "Transformers", "transformers")
_add("ai", "PyTorch", "torch")
_add("ai", "vLLM", "vllm")
_add("ai", "Ollama", "ollama")
_add("auth", "Passport", "passport")
_add("auth", "NextAuth", "next-auth", "@auth/core")
_add("auth", "Authlib", "authlib")
_add("auth", "JWT library", "pyjwt", "python-jose", "jsonwebtoken", "jose")
_add("auth", "Clerk", "@clerk/nextjs")
_add("cloud-sdk", "AWS SDK", "boto3", "botocore", "@aws-sdk/*", "aws-sdk")
_add("cloud-sdk", "Google Cloud SDK", "google-cloud-*", "@google-cloud/*")
_add("cloud-sdk", "Azure SDK", "azure-*", "@azure/*")
_add("storage", "MinIO", "minio")

# Python import root -> normalised package token
IMPORT_MAP = {
    "sqlite3": "sqlite3", "psycopg2": "psycopg2", "psycopg": "psycopg", "asyncpg": "asyncpg",
    "pymongo": "pymongo", "redis": "redis", "celery": "celery", "fastapi": "fastapi",
    "django": "django", "flask": "flask", "sqlalchemy": "sqlalchemy", "openai": "openai",
    "anthropic": "anthropic", "boto3": "boto3", "pika": "pika", "pymysql": "pymysql",
    "langchain": "langchain", "jwt": "pyjwt", "aiosqlite": "aiosqlite",
    "chromadb": "chromadb", "dramatiq": "dramatiq", "rq": "rq", "motor": "motor",
    "qdrant_client": "qdrant-client", "torch": "torch", "transformers": "transformers",
}

# docker image basename -> (category, display name)
IMAGE_MAP = {
    "postgres": ("database", "PostgreSQL"), "pgvector": ("vector", "pgvector"),
    "mysql": ("database", "MySQL"), "mariadb": ("database", "MySQL"),
    "mongo": ("database", "MongoDB"), "redis": ("cache", "Redis"),
    "valkey": ("cache", "Redis"), "memcached": ("cache", "Memcached"),
    "rabbitmq": ("queue", "RabbitMQ"), "kafka": ("queue", "Kafka"),
    "elasticsearch": ("search", "Elasticsearch"), "opensearch": ("search", "OpenSearch"),
    "meilisearch": ("search", "Meilisearch"), "qdrant": ("vector", "Qdrant"),
    "chroma": ("vector", "ChromaDB"), "weaviate": ("vector", "Weaviate"),
    "minio": ("storage", "MinIO"),
}

# ---------------------------------------------------------------- content regexes
SQLITE_RE = re.compile(
    r"sqlite:/{2,3}|provider\s*=\s*\"sqlite\"|sqlite3\.connect\(|better-sqlite3|aiosqlite"
    r"|django\.db\.backends\.sqlite3")
LMDB_RE = re.compile(r"\blmdb\b", re.I)
STREAM_RE = re.compile(
    r"text/event-stream|StreamingResponse|EventSourceResponse|streamText\(|"
    r"toDataStreamResponse|stream\s*=\s*True|stream:\s*true")
OPEN_CIDR_RE = re.compile(r"0\.0\.0\.0/0|::/0")
DB_PORT_RE = re.compile(r"\b(5432|3306|27017|6379|1433|1521)\b")
IAM_WILDCARD_RES = [
    re.compile(r'"Action"\s*:\s*"\*"'),
    re.compile(r'"Action"\s*:\s*\[\s*"\*"\s*\]'),
    re.compile(r"^\s*-?\s*Action:\s*['\"]?\*['\"]?\s*$"),
    re.compile(r"\bactions\s*=\s*\[\s*\"\*\"\s*\]"),
]
SECRET_PATTERNS = [
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("API secret key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{24,}")),
]
GENERIC_ASSIGN_RE = re.compile(
    r"""(?i)\b(?:api[_-]?key|secret(?:[_-]?key)?|password|passwd|auth[_-]?token|"""
    r"""access[_-]?token|client[_-]?secret|private[_-]?key)\b["']?\s*[:=]\s*["']([^"'\s]{12,})["']""")
ENV_ASSIGN_RE = re.compile(
    r"""^\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN)[A-Z0-9_]*\s*=\s*"""
    r"""["']?([^\s"'#]{12,})["']?\s*(?:#.*)?$""")
SECRET_HINT_RE = re.compile(
    r"AKIA|PRIVATE KEY|gh[pousr]_|xox[baprs]-|sk-|(?i:api[_-]?key|secret|passw|token|private[_-]?key)|://")
DB_URL_RE = re.compile(
    r"(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^:/\s\"']+:([^@\s\"']{6,})@")
PLACEHOLDERS = (
    "your", "example", "changeme", "change-me", "xxxx", "dummy", "sample", "placeholder",
    "replace", "todo", "fake", "redacted", "insert", "<", "secret_here", "token_here",
)
DB_URL_PLACEHOLDER_PASSWORDS = {
    "password", "postgres", "secret", "changeme", "example", "test", "root", "admin",
    "pass", "mysecretpassword", "yourpassword", "passw0rd", "password123", "mysql",
    "redis", "mongo", "db", "dbpass", "user",
}
DB_RESOURCE_TYPES = {
    "aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance", "aws_elasticache_cluster",
    "aws_redshift_cluster", "aws_docdb_cluster", "azurerm_postgresql_server",
    "azurerm_mysql_server", "google_sql_database_instance",
}
CONTAINER_RESOURCE_PREFIXES = (
    "aws_ecs_", "google_cloud_run", "azurerm_container_app", "azurerm_kubernetes_cluster",
    "aws_eks_", "google_container_cluster",
)
COMPOSE_NAME_RE = re.compile(r"^(?:docker-)?compose(?:\.[\w\-]+)*\.ya?ml$", re.I)
COMPOSE_PORT_RE = re.compile(
    r"""^\s*-\s*["']?(?:(\d{1,3}(?:\.\d{1,3}){3}):)?(\d+):(5432|3306|27017|6379|1433)"""
    r"""(?:/\w+)?["']?\s*$""")
YAML_IMAGE_RE = re.compile(r"""^\s*-?\s*image:\s*["']?([\w./\-]+)""")
K8S_KIND_RE = re.compile(
    r"^kind:\s*(?:Deployment|StatefulSet|Pod|CronJob|Job|DaemonSet)\s*$", re.M)
TF_BLOCK_RE = re.compile(r"^(resource|data|module|provider|variable|output|locals|terraform)\b")
TF_RESOURCE_RE = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([^"]+)"')
DOCKER_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.I)
DOCKER_USER_RE = re.compile(r"^\s*USER\s+(\S+)", re.I)
DOCKER_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+)", re.I)
NGINX_TIMEOUT_RE = re.compile(r"proxy_read_timeout\s+(\d+)\s*(ms|s|m|h|d)?\s*;")
UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}
GITIGNORE_ENV_RE = re.compile(r"^/?(?:\*\*/)?(?:\*)?\.env(?:\*|\.\*)?$")


def norm(name):
    return name.strip().lower().replace("_", "-")


def lookup(token):
    token = norm(token)
    hit = DEP_EXACT.get(token)
    if hit:
        return hit
    for prefix, category, name in DEP_PREFIX:
        if token.startswith(prefix):
            return category, name
    return None


# ---------------------------------------------------------------- collector
class Collector:
    def __init__(self):
        self.components = {}
        self.signals = defaultdict(list)
        self.languages = defaultdict(int)
        self.layout = defaultdict(lambda: defaultdict(int))
        self.dockerfiles = []
        self.compose_files = []
        self.k8s_files = []
        self.nginx = []
        self.tf_resources = defaultdict(int)
        self.tf_first = {}
        self.deploy_configs = []
        self.advisories = []
        self.provider_hits = defaultdict(int)

    def component(self, category, name, file, line, via, note=None, test=False):
        evidence = self.components.setdefault((category, name), [])
        limit = MAX_TEST_EVIDENCE if test else MAX_EVIDENCE
        if sum(1 for e in evidence if bool(e.get("test_file")) == test) >= limit:
            return
        if any(e["file"] == file and e["line"] == line for e in evidence):
            return
        item = {"file": file, "line": line, "via": via}
        if note:
            item["note"] = note
        if test:
            item["test_file"] = True
        evidence.append(item)

    def signal(self, key, file, line, note=None, test=False):
        bucket = self.signals[key]
        if sum(1 for e in bucket if bool(e.get("test_file")) == test) >= MAX_SIGNAL_EVIDENCE:
            return
        item = {"file": file, "line": line}
        if note:
            item["note"] = note
        if test:
            item["test_file"] = True
        bucket.append(item)


# ---------------------------------------------------------------- manifests
def _line_of(lines, pattern):
    rx = re.compile(pattern)
    for index, text in enumerate(lines, 1):
        if rx.search(text):
            return index
    return 1


def extract_package_json(text, lines):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    keys = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            keys.update(block.keys())
    found = {}
    for index, text_line in enumerate(lines, 1):
        match = re.match(r'\s*"([^"]+)"\s*:', text_line)
        if match and match.group(1) in keys and match.group(1) not in found:
            found[match.group(1)] = index
    return [(key, found.get(key, 1)) for key in sorted(keys)]


def extract_composer(text, lines):
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    keys = set()
    for section in ("require", "require-dev"):
        block = data.get(section)
        if isinstance(block, dict):
            keys.update(block.keys())
    return [(key, _line_of(lines, re.escape('"%s"' % key))) for key in sorted(keys)]


def extract_requirements(text, lines):
    out = []
    for index, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        if "://" in line.split()[0]:
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.\-]*)", line)
        if match:
            out.append((match.group(1), index))
    return out


def extract_toml_like(text, lines):
    """Dependencies from pyproject.toml, Pipfile, Cargo.toml and setup.cfg.

    Only dependency sections and dependency arrays are read, so keywords, classifiers and
    descriptions that mention a package name are not mistaken for dependencies.
    """
    out = []
    key_rx = re.compile(r"""^\s*([A-Za-z0-9_.\-]+)\s*=\s*(?:["{\[]|\d)""")
    quoted_rx = re.compile(
        r"""["']\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;@ ,]|["'])""")
    section, in_array, in_install_requires = "", False, False
    for index, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = re.match(r"^\[+\s*([^\]]+?)\s*\]+$", stripped)
        if header:
            section, in_array, in_install_requires = header.group(1).lower(), False, False
            continue
        if any(word in section for word in ("dependencies", "packages", "dependency-groups")):
            key = key_rx.match(raw)
            if key and key.group(1).lower() != "python":
                out.append((key.group(1), index))
            for quoted in quoted_rx.finditer(raw):
                out.append((quoted.group(1), index))
        elif section == "project":
            start = re.match(r"^dependencies\s*=\s*\[(.*)$", stripped)
            if start:
                in_array = "]" not in start.group(1)
                for quoted in quoted_rx.finditer(start.group(1)):
                    out.append((quoted.group(1), index))
            elif in_array:
                for quoted in quoted_rx.finditer(raw):
                    out.append((quoted.group(1), index))
                if "]" in raw:
                    in_array = False
        elif section == "options":
            if re.match(r"^install_requires\s*=", stripped):
                in_install_requires = True
                rest = stripped.split("=", 1)[1].strip()
                if rest:
                    out.extend((m.group(1), index) for m in [re.match(r"([A-Za-z0-9][\w.\-]*)", rest)] if m)
            elif in_install_requires and raw[:1] in (" ", "\t"):
                match = re.match(r"([A-Za-z0-9][\w.\-]*)", stripped)
                if match:
                    out.append((match.group(1), index))
            else:
                in_install_requires = False
    return out


def extract_gemfile(text, lines):
    out = []
    for index, raw in enumerate(lines, 1):
        match = re.match(r"""\s*gem\s+['"]([^'"]+)['"]""", raw)
        if match:
            out.append((match.group(1), index))
    return out


def extract_go_mod(text, lines):
    out = []
    for index, raw in enumerate(lines, 1):
        match = re.match(r"\s*(?:require\s+)?([\w.\-]+/[\w.\-/]+)\s+v[\w.\-+]+", raw)
        if match:
            out.append((match.group(1), index))
    return out


def extract_pom(text, lines):
    out = []
    for index, raw in enumerate(lines, 1):
        match = re.search(r"<artifactId>([^<]+)</artifactId>", raw)
        if match:
            out.append((match.group(1), index))
    return out


def extract_gradle(text, lines):
    out = []
    rx = re.compile(r"""['"]([\w.\-]+):([\w.\-]+)(?::[^'"]*)?['"]""")
    for index, raw in enumerate(lines, 1):
        for match in rx.finditer(raw):
            out.append((match.group(2), index))
    return out


def manifest_extractor(path, name):
    lower = name.lower()
    if lower == "package.json":
        return extract_package_json
    if lower == "composer.json":
        return extract_composer
    if lower.endswith(".txt") and (lower.startswith("requirements") or lower == "constraints.txt"
                                   or path.parent.name.lower() == "requirements"):
        return extract_requirements
    if lower in ("pyproject.toml", "pipfile", "setup.cfg", "cargo.toml"):
        return extract_toml_like
    if lower == "gemfile":
        return extract_gemfile
    if lower == "go.mod":
        return extract_go_mod
    if lower == "pom.xml":
        return extract_pom
    if lower in ("build.gradle", "build.gradle.kts"):
        return extract_gradle
    return None


# ---------------------------------------------------------------- helpers
def is_placeholder(value):
    lowered = value.lower()
    if lowered.startswith(("$", "%", "{", "@", "process.", "os.", "env", "ref:")):
        return True
    if "${" in value or "{{" in value:
        return True
    if any(token in lowered for token in PLACEHOLDERS):
        return True
    if re.fullmatch(r"[\*xX\-_.]+", value):
        return True
    return not any(char.isdigit() for char in value)


def snippet(line):
    return line.strip()[:120]


def to_seconds(number, unit):
    return int(number) * UNIT_SECONDS.get(unit, 1)


def read_text(path):
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, False
    if len(raw) > MAX_FILE_BYTES:
        return None, True
    if b"\0" in raw[:2048]:
        return None, False
    return raw.decode("utf-8", errors="replace"), False


def gitignored_dirs(root):
    """Directory names a root .gitignore excludes (plain `name/` or `/name` entries only)."""
    names = set()
    try:
        text = (Path(root) / ".gitignore").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return names
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")) or any(c in line for c in "*?[]"):
            continue
        name = line.strip("/")
        if name and "/" not in name:
            names.add(name.lower())
    return names


def root_gitignore_covers_env(root):
    try:
        text = (root / ".gitignore").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if GITIGNORE_ENV_RE.match(line.strip()):
            return True
    return False


# ---------------------------------------------------------------- per-file analysis
def is_test_path(rel, name):
    lowered = name.lower()
    if any(part in TEST_DIRS for part in rel.lower().split("/")[:-1]):
        return True
    return lowered.startswith("test_") or lowered == "conftest.py" or lowered.endswith(TEST_SUFFIXES)


def scan_python_imports(collector, rel, text, test):
    if len(text) > MAX_AST_BYTES or not any(module in text for module in IMPORT_MAP):
        return
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return
    # Imports are statements, so walk statement bodies only and skip expression trees.
    stack = [tree]
    while stack:
        node = stack.pop()
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
        for module in modules:
            token = IMPORT_MAP.get(module.split(".")[0])
            hit = lookup(token) if token else None
            if hit:
                collector.component(hit[0], hit[1], rel, node.lineno, "import",
                                    "import " + module, test=test)
        for field in ("body", "orelse", "finalbody", "handlers", "cases"):
            children = getattr(node, field, None)
            if isinstance(children, list):
                stack.extend(children)


def scan_terraform(collector, rel, lines):
    starts = [i for i, text in enumerate(lines) if TF_BLOCK_RE.match(text)]
    starts.append(len(lines))
    for position in range(len(starts) - 1):
        begin, end = starts[position], starts[position + 1]
        header = TF_RESOURCE_RE.match(lines[begin])
        if not header:
            continue
        rtype = header.group(1)
        collector.tf_resources[rtype] += 1
        collector.tf_first.setdefault(rtype, (rel, begin + 1))
        for prefix, provider in (("aws_", "aws"), ("google_", "gcp"), ("azurerm_", "azure")):
            if rtype.startswith(prefix):
                collector.provider_hits[provider] += 1
        block = lines[begin:end]
        cidr_line = next((begin + i + 1 for i, text in enumerate(block)
                          if OPEN_CIDR_RE.search(text)), None)
        if cidr_line and any(DB_PORT_RE.search(text) for text in block):
            collector.signal("db_open_cidr", rel, cidr_line,
                             "%s allows 0.0.0.0/0 on a database port" % rtype)
        if rtype in DB_RESOURCE_TYPES:
            for i, text in enumerate(block):
                if re.search(r"publicly_accessible\s*=\s*true", text):
                    collector.signal("db_public_flag", rel, begin + i + 1,
                                     "%s has publicly_accessible = true" % rtype)


def scan_dockerfile(collector, rel, lines):
    froms, users, exposed = [], [], []
    for text in lines:
        match = DOCKER_FROM_RE.match(text)
        if match:
            froms.append(match.group(1))
        match = DOCKER_USER_RE.match(text)
        if match:
            users.append(match.group(1).split(":")[0])
        match = DOCKER_EXPOSE_RE.match(text)
        if match:
            exposed.extend(match.group(1).split())
    runs_as_root = (not users) or users[-1].lower() in ("root", "0")
    collector.dockerfiles.append({
        "file": rel, "base_images": froms, "exposed_ports": exposed, "runs_as_root": runs_as_root,
    })
    collector.signal("container", rel, 1, "Dockerfile")
    if runs_as_root:
        collector.advisories.append({
            "id": "ADV-001", "severity": "low", "title": "Container runs as root",
            "fix": "Add a non-root USER instruction before the final CMD or ENTRYPOINT.",
            "evidence": [{"file": rel, "line": 1}],
        })


def scan_nginx(collector, rel, text, lines):
    if "proxy_pass" not in text:
        return
    proxy_line = _line_of(lines, r"proxy_pass")
    buffering_off = re.search(r"proxy_buffering\s+off\s*;", text) is not None
    timeouts = [to_seconds(m.group(1), m.group(2)) for m in NGINX_TIMEOUT_RE.finditer(text)]
    collector.nginx.append({
        "file": rel, "line": proxy_line, "buffering_off": buffering_off,
        "read_timeout_seconds": max(timeouts) if timeouts else None,
    })


def scan_lines(collector, rel, name, text, lines, check_secrets, env_ignored, test):
    is_env_file = name.startswith(".env")
    is_compose = bool(COMPOSE_NAME_RE.match(name))
    is_yaml = name.endswith((".yml", ".yaml"))
    lowered = text.lower()
    # File-level prefilters keep the per-line loop cheap on large repositories.
    maybe_sqlite = "sqlite" in lowered
    maybe_lmdb = "lmdb" in lowered
    maybe_stream = "stream" in lowered
    maybe_iam = "action" in lowered
    for number, line in enumerate(lines, 1):
        if len(line) > 2000:
            continue
        commented = line.lstrip().startswith(COMMENT_PREFIXES)
        if maybe_sqlite and not commented and SQLITE_RE.search(line):
            collector.component("database", "SQLite", rel, number, "content", snippet(line), test=test)
        if maybe_lmdb and not commented and LMDB_RE.search(line):
            collector.component("database", "LMDB", rel, number, "content", snippet(line), test=test)
        if maybe_stream and not commented and STREAM_RE.search(line):
            weak = IMPORT_LINE_RE.match(line) is not None
            collector.signal("streaming", rel, number,
                             "import only" if weak else snippet(line), test=test)
        if maybe_iam and any(rx.search(line) for rx in IAM_WILDCARD_RES):
            collector.signal("iam_wildcard", rel, number, "IAM policy allows Action *", test=test)
        if is_yaml:
            image = YAML_IMAGE_RE.match(line)
            if image:
                base = image.group(1).rsplit("/", 1)[-1].split(":")[0].lower()
                hit = IMAGE_MAP.get(base)
                if hit:
                    collector.component(hit[0], hit[1], rel, number, "image",
                                        "image " + image.group(1), test=test)
        if is_compose:
            port = COMPOSE_PORT_RE.match(line)
            if port and port.group(1) != "127.0.0.1":
                collector.signal("compose_db_port", rel, number,
                                 "database port %s published on all interfaces" % port.group(3))
        if not check_secrets or not SECRET_HINT_RE.search(line):
            continue
        reported = False
        for label, rx in SECRET_PATTERNS:
            if rx.search(line):
                collector.signal("secret", rel, number, label, test=test)
                reported = True
                break
        if reported:
            continue
        match = GENERIC_ASSIGN_RE.search(line)
        if match and not is_placeholder(match.group(1)):
            collector.signal("secret", rel, number, "Hardcoded credential assignment", test=test)
            continue
        match = DB_URL_RE.search(line)
        if match:
            password = match.group(1)
            if (password.lower() not in DB_URL_PLACEHOLDER_PASSWORDS and "${" not in password
                    and "{{" not in password and not password.startswith(("$", "%", "<"))):
                collector.signal("secret", rel, number, "Database URL with embedded password",
                                 test=test)
                continue
        if is_env_file and not env_ignored:
            match = ENV_ASSIGN_RE.match(line)
            if match and not is_placeholder(match.group(1)):
                collector.signal("secret", rel, number,
                                 "Secret value in a .env file that is not gitignored", test=test)


def scan_file(collector, rel, path, name, text, env_ignored):
    lower = name.lower()
    lines = text.splitlines()
    suffix = path.suffix.lower()
    if suffix in LANG_BY_EXT:
        collector.languages[LANG_BY_EXT[suffix]] += 1
        if not is_test_path(rel, name):
            top = rel.split("/")[0] if "/" in rel else "."
            collector.layout[top][LANG_BY_EXT[suffix]] += 1
    if lower in DEPLOY_CONFIG_NAMES:
        collector.deploy_configs.append({"file": rel, "platform": DEPLOY_CONFIG_NAMES[lower]})
    extractor = manifest_extractor(path, name)
    if extractor:
        for dep, line in extractor(text, lines):
            hit = lookup(dep)
            if hit:
                collector.component(hit[0], hit[1], rel, line, "manifest", dep)
    test = is_test_path(rel, name)
    if suffix == ".py":
        scan_python_imports(collector, rel, text, test)
    if lower.startswith("dockerfile") or lower.endswith(".dockerfile"):
        scan_dockerfile(collector, rel, lines)
    if COMPOSE_NAME_RE.match(name):
        collector.compose_files.append(rel)
        collector.signal("container", rel, 1, "docker compose")
    if suffix in (".yml", ".yaml") and K8S_KIND_RE.search(text):
        collector.k8s_files.append(rel)
        collector.signal("container", rel, 1, "Kubernetes workload")
    if suffix == ".conf":
        scan_nginx(collector, rel, text, lines)
    if suffix == ".tf":
        scan_terraform(collector, rel, lines)
    is_env_example = lower.endswith((".example", ".sample", ".template", ".dist"))
    check_secrets = suffix != ".md" and not is_env_example
    if lower.startswith(".env") and env_ignored:
        check_secrets = False
    scan_lines(collector, rel, lower, text, lines, check_secrets, env_ignored, test)


# ---------------------------------------------------------------- findings
def build_findings(collector):
    findings = []
    signals = collector.signals
    tf = collector.tf_resources

    def first(rtype):
        file, line = collector.tf_first[rtype]
        return {"file": file, "line": line, "note": rtype}

    # SEC-001
    if "aws_cloudfront_distribution" in tf and any(t.startswith("aws_ecs_") for t in tf) \
            and not any(t in tf for t in ("aws_lb", "aws_alb")):
        evidence = [first("aws_cloudfront_distribution")]
        ecs = next(t for t in tf if t.startswith("aws_ecs_"))
        evidence.append(first(ecs))
        findings.append({
            "id": "SEC-001", "severity": "high",
            "title": "Direct ingress to private compute",
            "detail": "CloudFront and ECS are defined but no load balancer sits between them.",
            "fix": "Insert a public ALB with an HTTPS listener and target group; keep tasks in private subnets.",
            "evidence": evidence,
        })
    # SEC-002
    exposure = signals.get("db_open_cidr", []) + signals.get("db_public_flag", [])
    if exposure:
        findings.append({
            "id": "SEC-002", "severity": "critical", "title": "Public database exposure",
            "detail": "A database is reachable from the internet.",
            "fix": "Move the database to a private data subnet and allow only the application security group.",
            "evidence": exposure[:MAX_EVIDENCE],
        })
    elif signals.get("compose_db_port"):
        findings.append({
            "id": "SEC-002", "severity": "medium", "title": "Public database exposure",
            "detail": "Compose publishes a database port on all interfaces. Fine for local development only.",
            "fix": "Bind to 127.0.0.1 locally and never copy this mapping into a deployed environment.",
            "evidence": signals["compose_db_port"][:MAX_EVIDENCE],
        })
    # SEC-003
    embedded = []
    for key in (("database", "SQLite"), ("database", "LMDB")):
        for item in collector.components.get(key, []):
            if not item.get("test_file"):
                embedded.append(dict(item, note=key[1]))
    if embedded:
        stateless = bool(signals.get("container")) or any(
            t.startswith(CONTAINER_RESOURCE_PREFIXES) for t in tf)
        findings.append({
            "id": "SEC-003", "severity": "high" if stateless else "medium",
            "title": "Ephemeral storage data loss trap",
            "detail": ("Embedded file database found alongside container deployment."
                       if stateless else
                       "Embedded file database found. Data is lost or corrupted once compute is replicated or replaced."),
            "fix": "Use a managed PostgreSQL service, or a dedicated persistent volume with a single writer.",
            "evidence": embedded[:MAX_EVIDENCE],
        })
    # SEC-004
    live_streaming = [s for s in signals.get("streaming", []) if not s.get("test_file")]
    if live_streaming and collector.nginx:
        usage_seen = any(s.get("note") != "import only" for s in live_streaming)
        evidence = []
        for proxy in collector.nginx:
            problems = []
            if not proxy["buffering_off"]:
                problems.append("proxy_buffering is not off")
            timeout = proxy["read_timeout_seconds"]
            if timeout is None or timeout < 300:
                problems.append("proxy_read_timeout is %s" % (
                    "unset" if timeout is None else "%ss" % timeout))
            if problems:
                evidence.append({"file": proxy["file"], "line": proxy["line"],
                                 "note": "; ".join(problems)})
        if evidence:
            findings.append({
                "id": "SEC-004", "severity": "medium",
                "confidence": "confirmed" if usage_seen else "inferred",
                "title": "Reverse-proxy streaming dropout",
                "detail": ("The app streams responses but the proxy buffers or times out early."
                           if usage_seen else
                           "Streaming is only suggested by an import. Confirm a streaming route exists; "
                           "if it does, the proxy buffers or times out early."),
                "fix": "Set proxy_buffering off and proxy_read_timeout 300s or higher for streaming routes.",
                "evidence": evidence[:MAX_EVIDENCE],
            })
    # SEC-005
    found = signals.get("secret", []) + signals.get("iam_wildcard", [])
    real = [s for s in found if not s.get("test_file")]
    in_tests = [s for s in found if s.get("test_file")]
    if real or in_tests:
        critical = any(s.get("note") in ("AWS access key id", "Private key block") for s in real)
        if real:
            severity = "critical" if critical else "high"
            detail = "Secret values are redacted in this report; only the location is shown."
        else:
            severity = "low"
            detail = ("Only found in test files. Confirm these are fake values. "
                      "Secret values are redacted in this report.")
        findings.append({
            "id": "SEC-005", "severity": severity,
            "title": "Hardcoded credentials or permissive IAM",
            "detail": detail,
            "fix": " ".join(part for part in (
                "Rotate anything exposed and move the values to a secrets manager."
                if any(s in signals.get("secret", []) for s in real + in_tests) else "",
                "Replace Action * with the specific actions needed."
                if any(s in signals.get("iam_wildcard", []) for s in real + in_tests) else "") if part),
            "evidence": (real + in_tests)[:MAX_EVIDENCE],
        })
    for finding in findings:
        finding.setdefault("confidence", "confirmed")
    return findings


# ---------------------------------------------------------------- walk and report
def scan(root, max_files=DEFAULT_MAX_FILES):
    started = time.perf_counter()
    root = Path(root).resolve()
    collector = Collector()
    env_ignored = root_gitignore_covers_env(root)
    ignored_dirs = gitignored_dirs(root) | GENERATED_DIRS
    scanned = skipped_large = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d.lower() not in ignored_dirs
                             and not os.path.islink(os.path.join(dirpath, d)))
        for filename in sorted(filenames):
            lower = filename.lower()
            path = Path(dirpath) / filename
            if lower in LOCK_NAMES or lower.endswith(".min.js") or path.is_symlink():
                continue
            wanted = (path.suffix.lower() in TEXT_EXT or lower in TEXT_NAMES
                      or lower.startswith(("dockerfile", ".env")) or lower.endswith(".dockerfile"))
            if wanted and path.suffix.lower() == ".txt" and not manifest_extractor(path, filename):
                wanted = False  # prose notes are not evidence; only requirements files are read
            if not wanted:
                continue
            if scanned >= max_files:
                truncated = True
                break
            text, too_large = read_text(path)
            if too_large:
                skipped_large += 1
                continue
            if text is None:
                continue
            scanned += 1
            rel = path.relative_to(root).as_posix()
            scan_file(collector, rel, path, filename, text, env_ignored)
        if truncated:
            break

    components = [
        {"category": cat, "name": name, "evidence": evidence}
        for (cat, name), evidence in sorted(collector.components.items())
    ]
    for sdk, provider in (("AWS SDK", "aws"), ("Google Cloud SDK", "gcp"), ("Azure SDK", "azure")):
        collector.provider_hits[provider] += len(collector.components.get(("cloud-sdk", sdk), []))
    names_by_dir = defaultdict(set)
    for (cat, name), evidence in collector.components.items():
        for item in evidence:
            if not item.get("test_file"):
                top = item["file"].split("/")[0] if "/" in item["file"] else "."
                names_by_dir[top].add(name)
    code_layout = [
        {"path": top, "languages": dict(sorted(langs.items())), "components": sorted(names_by_dir.get(top, []))}
        for top, langs in sorted(collector.layout.items())
    ]
    live_streaming = [s for s in collector.signals.get("streaming", []) if not s.get("test_file")]
    streaming = ("used" if any(s.get("note") != "import only" for s in live_streaming)
                 else "import-only" if live_streaming else "none")
    return {
        "scanner_version": SCANNER_VERSION,
        "root": root.name,
        "stats": {
            "files_scanned": scanned,
            "files_skipped_too_large": skipped_large,
            "truncated": truncated,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        },
        "languages": dict(sorted(collector.languages.items())),
        "code_layout": code_layout,
        "signals": {"streaming": streaming},
        "components": components,
        "signal_evidence": {key: [e for e in items if not e.get("test_file")]
                            for key, items in sorted(collector.signals.items())
                            if any(not e.get("test_file") for e in items)},
        "infrastructure": {
            "dockerfiles": collector.dockerfiles,
            "compose_files": collector.compose_files,
            "kubernetes_files": collector.k8s_files,
            "nginx_proxies": collector.nginx,
            "terraform_resources": dict(sorted(collector.tf_resources.items())),
            "deploy_configs": collector.deploy_configs,
        },
        "provider_hints": dict(sorted(collector.provider_hits.items())),
        "findings": build_findings(collector),
        "advisories": collector.advisories,
        "structure": structure_scanner.scan_structure(root),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="SolsArch repository evidence scanner")
    parser.add_argument("path", nargs="?", default=".", help="Repository root (default: current directory)")
    parser.add_argument("--pretty", action="store_true", help="Indent the JSON output")
    parser.add_argument("--output", help="Write the JSON here instead of stdout (parent folders are created)")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES,
                        help="Stop after this many text files (default %d)" % DEFAULT_MAX_FILES)
    args = parser.parse_args(argv)
    root = Path(args.path)
    if not root.is_dir():
        sys.stderr.write("error: %s is not a directory\n" % args.path)
        return 2
    result = scan(root, max_files=max(1, args.max_files))
    text = json.dumps(result, indent=2 if args.pretty else None) + "\n"
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        sys.stdout.write(json.dumps({"written": str(target), "findings": len(result["findings"]),
                                     "components": len(result["components"])}) + "\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
