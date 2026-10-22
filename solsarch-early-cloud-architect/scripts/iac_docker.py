#!/usr/bin/env python3
"""Hardened Dockerfiles and a .dockerignore for units that do not have one, chosen from the frameworks the scan found.

Every file builds in stages where that helps, runs as a non-root user, keeps secrets out of the image, and exposes the port
the code uses. The start command is the one thing a scan cannot always know, so it is the only line marked to check.
"""
from iac_common import Files, T

PY = "python:3.12-slim"
NODE = "node:22-slim"
GO = "golang:1.24"
JAVA_BUILD = "maven:3.9-eclipse-temurin-21"
JAVA_RUN = "eclipse-temurin:21-jre"

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


def python_file(unit, port):
    cmd = "uvicorn main:app --host 0.0.0.0 --port %d" % port
    if has(unit, "Django"):
        cmd = "gunicorn config.wsgi:application --bind 0.0.0.0:%d" % port
    elif has(unit, "Flask"):
        cmd = "gunicorn app:app --bind 0.0.0.0:%d" % port
    elif unit["kind"] == "worker" and has(unit, "Celery"):
        cmd = "celery -A app.worker worker --loglevel=INFO"
    return T("""
        FROM @@base@@ AS build
        WORKDIR /app
        ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
        COPY requirements*.txt ./
        RUN python -m venv /opt/venv && /opt/venv/bin/pip install -r requirements.txt

        FROM @@base@@
        ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=@@port@@
        RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app
        WORKDIR /app
        COPY --from=build /opt/venv /opt/venv
        COPY --chown=app:app . .
        USER 10001
        EXPOSE @@port@@
        # Check this line: it is the command that starts @@name@@. The scan chose it from the framework.
        CMD ["sh", "-c", "@@cmd@@"]
    """, base=PY, port=port, name=unit["name"], cmd=cmd)


def node_file(unit, port):
    return T("""
        FROM @@base@@ AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npm run build --if-present && npm prune --omit=dev

        FROM @@base@@
        ENV NODE_ENV=production PORT=@@port@@
        WORKDIR /app
        COPY --from=build --chown=node:node /app ./
        USER 1000:1000
        EXPOSE @@port@@
        # Check this line: it is the command that starts @@name@@. The scan chose it from the framework.
        CMD ["npm", "start"]
    """, base=NODE, port=port, name=unit["name"])


def go_file(unit, port):
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


def java_file(unit, port):
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


def generate(model):
    files = Files(model.fingerprint)
    made = []
    for u in model.units:
        if u["kind"] == "static" or u.get("dockerfile"):
            continue
        builder = BUILDERS.get(lang(u))
        if not builder:
            continue
        folder = u["folder"].strip("./") or "."
        path = "docker/%s/Dockerfile" % u["name"]
        files.add(path, builder(u, u["port"] or 8080))
        files.add("docker/%s/.dockerignore" % u["name"], DOCKERIGNORE, header=False)
        made.append((u["name"], folder))
    if made:
        files.add("docker/README.md", T("""
            # Dockerfiles for @@project@@

            These units had no Dockerfile in the repository, so one was written for each from the frameworks the scan found.
            Copy each into its folder (or build it with `docker build -f docker/<unit>/Dockerfile <folder>`):

            @@list@@

            Each image runs as a non-root user and keeps secrets out of the image. Check the `CMD` line in each file before you build.
        """, project=model.project, list="\n".join("- `docker/%s/Dockerfile` for `%s`" % (n, f) for n, f in made)))
    return files
