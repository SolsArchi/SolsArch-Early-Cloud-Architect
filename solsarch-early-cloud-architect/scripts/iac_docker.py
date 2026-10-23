#!/usr/bin/env python3
"""Hardened Dockerfiles and a .dockerignore for units that do not have one, chosen from the frameworks the scan found.

Every file builds in stages where that helps, runs as a non-root user, keeps secrets out of the image, and exposes the port
the code uses. The start command is the one thing a scan cannot always know, so it is the only line marked to check.
"""
import re

from iac_common import Files, T, shq

PY = "python:3.12-slim"
NODE = "node:22-slim"
GO = "golang:1.24"
JAVA_BUILD = "maven:3.9-eclipse-temurin-21"
JAVA_RUN = "eclipse-temurin:21-jre"
ADAPTER = "public.ecr.aws/awsguru/aws-lambda-adapter:0.8.4"
ADAPTER_LINE = "COPY --from=%s /lambda-adapter /opt/extensions/lambda-adapter" % ADAPTER


def lambda_env(unit, port):
    return "\nENV AWS_LWA_PORT=%d AWS_LWA_READINESS_CHECK_PATH=%s%s" % (
        port, unit.get("health_path") or "/", " AWS_LWA_INVOKE_MODE=response_stream" if unit.get("streaming") else "")

DOCKERIGNORE = """.git
.gitignore
node_modules
__pycache__
*.pyc
.venv
venv
.env
.env.*
*.pem
*.key
.solsarch
docs
tests
terraform
ansible
kubernetes
helm
*.md
"""


def has(unit, *names):
    return any(n.lower() in [f.lower() for f in unit.get("frameworks", [])] for n in names)


def lang(unit):
    langs = [l.lower() for l in unit.get("languages", [])]
    for l in ("python", "typescript", "javascript", "go", "java", "ruby", "php", "csharp"):
        if l in langs:
            return {"typescript": "node", "javascript": "node"}.get(l, l)
    if has(unit, "Next.js", "React", "Express", "NestJS", "Vue", "Nuxt", "BullMQ", "Svelte", "Remix", "Astro"):
        return "node"
    if has(unit, "FastAPI", "Flask", "Django", "Celery"):
        return "python"
    return "unknown"


def python_file(unit, port, lam=False):
    entry = unit.get("entrypoint") or {}
    if entry.get("kind") == "asgi":
        cmd, source = "uvicorn %s --host 0.0.0.0 --port %d" % (entry["target"], port), "found in %s:%s" % (entry["file"], entry["line"])
    elif entry.get("kind") == "wsgi":
        cmd, source = "gunicorn %s --bind 0.0.0.0:%d" % (entry["target"], port), "found in " + entry["file"]
    else:
        cmd, source = "uvicorn main:app --host 0.0.0.0 --port %d" % port, "guessed from the framework; check it"
        if has(unit, "Django"):
            cmd = "gunicorn config.wsgi:application --bind 0.0.0.0:%d" % port
        elif has(unit, "Flask"):
            cmd = "gunicorn app:app --bind 0.0.0.0:%d" % port
        elif unit["kind"] == "worker" and has(unit, "Celery"):
            cmd = "celery -A app.worker worker --loglevel=INFO"
    adapter, env = "", ""
    if lam:
        adapter = ADAPTER_LINE + chr(10)
        env = lambda_env(unit, port)
    return T("""
        FROM @@base@@ AS build
        WORKDIR /app
        ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
        COPY requirements*.txt ./
        RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

        FROM @@base@@
        @@adapter@@ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=@@port@@@@env@@
        RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
        WORKDIR /app
        COPY --from=build /opt/venv /opt/venv
        COPY --chown=app:app . .
        USER 10001
        EXPOSE @@port@@
        # The start command for @@name@@ (@@source@@).
        CMD ["sh", "-c", "@@cmd@@"]
    """, base=PY, port=port, name=unit["name"], cmd=cmd, adapter=adapter, env=env, source=source)


def node_file(unit, port, lam=False):
    adapter = (ADAPTER_LINE + "\n") if lam else ""
    env = lambda_env(unit, port) if lam else ""
    if has(unit, "Next.js"):
        return T("""
            FROM @@base@@ AS build
            WORKDIR /app
            COPY package*.json ./
            RUN npm ci
            COPY . .
            RUN npm run build

            FROM @@base@@
            @@adapter@@ENV NODE_ENV=production PORT=@@port@@ HOSTNAME=0.0.0.0@@env@@
            WORKDIR /app
            # Needs  output: "standalone"  in next.config.js, which makes the build small enough to start quickly.
            COPY --from=build --chown=1000:1000 /app/.next/standalone ./
            COPY --from=build --chown=1000:1000 /app/.next/static ./.next/static
            COPY --from=build --chown=1000:1000 /app/public ./public
            USER 1000:1000
            EXPOSE @@port@@
            CMD ["node", "server.js"]
        """, base=NODE, port=port, adapter=adapter, env=env)
    return T("""
        FROM @@base@@ AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npm run build --if-present && npm prune --omit=dev

        FROM @@base@@
        @@adapter@@ENV NODE_ENV=production PORT=@@port@@@@env@@
        WORKDIR /app
        COPY --from=build --chown=node:node /app ./
        USER 1000:1000
        EXPOSE @@port@@
        # Check this line: it is the command that starts @@name@@. The scan chose it from the framework.
        CMD ["npm", "start"]
    """, base=NODE, port=port, name=unit["name"], adapter=adapter, env=env)


def go_file(unit, port, lam=False):
    return T("""
        FROM @@build@@ AS build
        WORKDIR /src
        COPY go.mod go.sum ./
        RUN go mod download
        COPY . .
        RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/app ./...

        FROM gcr.io/distroless/static-debian12:nonroot
        ENV PORT=@@port@@
        COPY --from=build /out/app /app
        USER nonroot:nonroot
        EXPOSE @@port@@
        ENTRYPOINT ["/app"]
    """, build=GO, port=port)


def java_file(unit, port, lam=False):
    return T("""
        FROM @@build@@ AS build
        WORKDIR /src
        COPY pom.xml .
        RUN mvn -q -B dependency:go-offline
        COPY src ./src
        RUN mvn -q -B package -DskipTests

        FROM @@run@@
        ENV PORT=@@port@@
        RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
        COPY --from=build --chown=app:app /src/target/*.jar /app/app.jar
        USER 10001
        EXPOSE @@port@@
        ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/app.jar"]
    """, build=JAVA_BUILD, run=JAVA_RUN, port=port)


BUILDERS = {"python": python_file, "node": node_file, "go": go_file, "java": java_file}


SAFE_PATH = re.compile(r"^/[A-Za-z0-9/_.-]*$")


def with_health(text, unit, port, language, lam):
    """Add a HEALTHCHECK that asks the app's own health path, where the image has something to ask with.

    Python and Node images can make the request themselves. A Lambda image is not health-checked by Docker at all, a worker listens
    on no port, and distroless or JRE images have no shell or curl; those get a one-line note that a scanner reads as a reasoned exemption."""
    path = unit.get("health_path") or "/"
    if not SAFE_PATH.match(path):
        path = "/"
    if not lam and unit["kind"] != "worker" and language == "python":
        check = ("HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD "
                 "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:%d%s', timeout=2)\"" % (port, path))
    elif not lam and unit["kind"] != "worker" and language == "node":
        check = ("HEALTHCHECK --interval=30s --timeout=3s --start-period=20s CMD "
                 "node -e \"fetch('http://127.0.0.1:%d%s').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))\"" % (port, path))
    else:
        why = ("AWS Lambda does not use Docker health checks, the platform checks the function" if lam else
               "a worker listens on no port" if unit["kind"] == "worker" else
               "this image has no shell or curl to ask with, so the orchestrator probes the port")
        return "# checkov:skip=CKV_DOCKER_2:" + why + chr(10) + text
    lines = text.split(chr(10))
    at = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith("EXPOSE ")), None)
    if at is None:
        return text
    lines.insert(at + 1, check)
    return chr(10).join(lines)


def generate(model):
    files = Files(model.fingerprint)
    made = []
    lambda_target = model.cloud == "aws" and model.mode == "serverless"
    for u in model.units:
        if u["kind"] == "static":
            continue
        builder = BUILDERS.get(lang(u))
        if not builder:
            continue
        if u.get("dockerfile") and not lambda_target:
            continue
        name = "Dockerfile.lambda" if (u.get("dockerfile") and lambda_target) else "Dockerfile"
        folder = u["folder"].strip("./") or "."
        files.add("docker/%s/%s" % (u["name"], name), with_health(builder(u, u["port"] or 8080, lambda_target), u, u["port"] or 8080, lang(u), lambda_target))
        files.add("docker/%s/.dockerignore" % u["name"], DOCKERIGNORE, header=False)
        made.append((u["name"], folder, name))
    if made:
        intro = ("These units run as AWS Lambda container images, so each Dockerfile adds the AWS Lambda Web Adapter: it lets an ordinary web "
                 "server (uvicorn, Node) answer Lambda invocations unchanged. Where the repository already had a Dockerfile it is left alone "
                 "and a `Dockerfile.lambda` is written beside it; build that one for Lambda." if lambda_target else
                 "These units had no Dockerfile in the repository, so one was written for each from the frameworks the scan found.")
        fence = chr(96) * 3
        commands = chr(10).join("docker build -f %s %s" % (shq("docker/%s/%s" % (n, f)), shq(folder)) for n, folder, f in made)
        listing = ("One Dockerfile per unit: " + ", ".join("`docker/%s/%s`" % (n, f) for n, folder, f in made) + "." + chr(10) + chr(10)
                   + "Build them from the repository root (each folder name is quoted, because a name from your repository may hold spaces or shell characters):"
                   + chr(10) + chr(10) + fence + "bash" + chr(10) + commands + chr(10) + fence)
        files.add("docker/README.md", T("""
            # Dockerfiles for @@project@@

            @@intro@@

            @@listing@@

            Each image keeps secrets out of the image. Check the start command in each file before you build.
        """, project=model.project, intro=intro, listing=listing))
    return files
