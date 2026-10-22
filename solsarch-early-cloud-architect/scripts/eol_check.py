#!/usr/bin/env python3
"""Find runtimes, frameworks and base images in a repository that are past (or close to) end of life.

Versions come from files in the repo (Dockerfile FROM lines, .nvmrc, .python-version, package.json engines, go.mod,
runtime.txt, docker-compose images, pyproject, .tool-versions). The end-of-life dates come from references/eol.json
(endoflife.date, with the day it was fetched), so the answer is the same on every run. A product the file does not
know is reported as unknown, never guessed.
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EOL_FILE = HERE.parent / "references" / "eol.json"
SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".solsarch", "vendor", "target"}
IMAGE_PRODUCTS = {"node": "nodejs", "python": "python", "golang": "go", "php": "php", "ruby": "ruby", "postgres": "postgresql",
                  "mysql": "mysql", "mariadb": "mariadb", "redis": "redis", "mongo": "mongodb", "rabbitmq": "rabbitmq",
                  "ubuntu": "ubuntu", "debian": "debian", "alpine": "alpine", "elasticsearch": "elasticsearch",
                  "eclipse-temurin": "eclipse-temurin", "amazoncorretto": "amazon-corretto", "mcr.microsoft.com/dotnet/aspnet": "dotnet",
                  "mcr.microsoft.com/dotnet/sdk": "dotnet"}
WARN_DAYS = 180


def load():
    return json.loads(EOL_FILE.read_text(encoding="utf-8"))


def _cycle(product, version):
    """The product's release line that a version string belongs to."""
    cycles = product["cycles"]
    version = str(version).lstrip("v")
    parts = version.split(".")
    for depth in range(min(3, len(parts)), 0, -1):
        key = ".".join(parts[:depth])
        for c in cycles:
            if str(c["cycle"]) == key:
                return c
    return None


def _status(cycle, today):
    eol = cycle.get("eol")
    if eol in (False, None, "false"):
        return "supported", None
    if eol is True:
        return "end_of_life", None
    try:
        date = datetime.date.fromisoformat(str(eol))
    except ValueError:
        return "unknown", None
    days = (date - today).days
    if days < 0:
        return "end_of_life", str(eol)
    if days <= WARN_DAYS:
        return "ending_soon", str(eol)
    return "supported", str(eol)


def walk(root):
    """Files under root in a fixed order, without entering folders that hold other people's code."""
    import os
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not d.startswith("."))
        for name in sorted(names):
            yield Path(folder) / name


def find_versions(root):
    root = Path(root)
    found = []

    def add(product, version, file, line):
        found.append({"product": product, "version": version, "file": str(file.relative_to(root)).replace("\\", "/"), "line": line})

    for path in walk(root):
        name = path.name.lower()
        try:
            if path.stat().st_size > 400000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        lines = text.split("\n")
        if name.startswith("dockerfile") or name.endswith((".dockerfile", ".yml", ".yaml")) and "image" in text:
            for n, line in enumerate(lines, 1):
                m = re.match(r"^\s*(?:FROM\s+(?:--platform=\S+\s+)?|-?\s*image:\s*['\"]?)([\w./-]+):v?(\d[\w.]*)", line)
                if m and m.group(1) in IMAGE_PRODUCTS:
                    add(IMAGE_PRODUCTS[m.group(1)], m.group(2), path, n)
        if name == ".nvmrc":
            m = re.search(r"(\d+(?:\.\d+)*)", text)
            if m:
                add("nodejs", m.group(1), path, 1)
        if name in (".python-version", "runtime.txt"):
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
            if m and ("python" in text.lower() or name == ".python-version"):
                add("python", m.group(1), path, 1)
        if name == ".tool-versions":
            for n, line in enumerate(lines, 1):
                m = re.match(r"^(nodejs|python|golang|ruby|terraform)\s+v?(\d[\w.]*)", line)
                if m:
                    add({"golang": "go"}.get(m.group(1), m.group(1)), m.group(2), path, n)
        if name == "package.json":
            try:
                engines = (json.loads(text).get("engines") or {}).get("node", "")
            except ValueError:
                engines = ""
            m = re.search(r"(\d+)", engines)
            if m:
                add("nodejs", m.group(1), path, 1)
        if name == "go.mod":
            m = re.search(r"^go\s+(\d+\.\d+)", text, re.M)
            if m:
                add("go", m.group(1), path, 1)
        if name in ("pyproject.toml", "setup.py", "setup.cfg"):
            m = re.search(r"python_requires\s*=\s*[\"']?\s*>=\s*(\d+\.\d+)|requires-python\s*=\s*[\"']>=\s*(\d+\.\d+)", text)
            if m:
                add("python", m.group(1) or m.group(2), path, 1)
    return found


def check(root, today=None):
    data = load()
    today = today or datetime.date.fromisoformat(data["fetched"])
    out, seen = [], set()
    for item in find_versions(root):
        key = (item["product"], item["version"], item["file"])
        if key in seen:
            continue
        seen.add(key)
        product = data["products"].get(item["product"])
        cycle = _cycle(product, item["version"]) if product else None
        if not cycle:
            out.append(dict(item, status="unknown", eol=None, latest=None, name=(product or {}).get("name", item["product"])))
            continue
        status, eol = _status(cycle, today)
        out.append(dict(item, status=status, eol=eol, latest=cycle.get("latest"), name=product["name"], url=product.get("url")))
    out.sort(key=lambda r: (r["product"], r["version"], r["file"]))
    return {"fetched": data["fetched"], "source": data["source"], "findings": out,
            "end_of_life": [r for r in out if r["status"] == "end_of_life"], "ending_soon": [r for r in out if r["status"] == "ending_soon"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find end-of-life runtimes and images in a repository")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    sys.stdout.write(json.dumps(check(args.repo), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
