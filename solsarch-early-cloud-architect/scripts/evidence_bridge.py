#!/usr/bin/env python3
"""Flatten a scan into numbered evidence (E001, E002, ...) for the placement engine and the verifier.

Standard library only. ast_scanner.py groups what it finds by component. place_services.py and
verify_report.py work on a flat list where every item has an id, a kind, a name and a signal. This
module converts one into the other without reading the repository again, so the ids are the same on
every run for the same scan.

Usage: python evidence_bridge.py SCAN.json [--output evidence.json]
"""
import argparse
import json
import sys
from pathlib import Path

KIND_BY_CATEGORY = {
    "framework": "framework", "orm": "orm", "queue": "queue", "cache": "cache", "database": "datastore",
    "storage": "storage", "cloud-sdk": "cloud_sdk", "ai": "ai", "vector": "ai", "search": "datastore",
    "auth": "auth",
}
NAME_ALIASES = {"Ruby on Rails": "Rails", "OpenAI SDK": "OpenAI API", "Anthropic SDK": "Anthropic API"}
SIGNAL_BY_NAME = {
    "PostgreSQL": "postgres", "MySQL": "mysql", "SQLite": "sqlite", "LMDB": "lmdb", "MongoDB": "mongodb",
    "Redis": "redis", "Memcached": "memcached", "Kafka": "stream", "RabbitMQ": "queue",
    "Celery": "queue", "BullMQ": "queue", "RQ": "queue", "Dramatiq": "queue", "Sidekiq": "queue",
    "OpenAI SDK": "llm", "Anthropic SDK": "llm", "LangChain": "llm", "LlamaIndex": "llm", "vLLM": "llm",
    "Ollama": "llm", "Transformers": "gpu", "PyTorch": "gpu", "MinIO": "object_storage",
    "AWS SDK": "aws", "Google Cloud SDK": "gcp", "Azure SDK": "azure",
    "Elasticsearch": "search", "OpenSearch": "search", "Meilisearch": "search",
}
LANG_NAMES = {"python": "Python", "javascript": "JavaScript", "typescript": "TypeScript", "go": "Go", "ruby": "Ruby",
              "java": "Java", "kotlin": "Kotlin", "php": "PHP", "rust": "Rust"}
SIGNAL_KINDS = {"streaming": ("realtime", "Streaming / WebSocket", "streaming"),
                "secret": ("secret", "Hardcoded credential", "hardcoded_secret"),
                "iam_wildcard": ("iam", "Wildcard IAM action", "iam_wildcard_action"),
                "db_open_cidr": ("network", "Public CIDR 0.0.0.0/0", "public_db_cidr")}


def flatten(scan):
    """Return {"evidence": [...], "by_component": {"category|name": [ids]}} for a scan written by ast_scanner.scan()."""
    items, seen, by_component = [], set(), {}

    def add(kind, name, file, line, detail, signal=None):
        key = (kind, name, file, line)
        if key in seen:
            return None
        seen.add(key)
        eid = "E%03d" % (len(items) + 1)
        items.append({"id": eid, "kind": kind, "name": name, "file": file,
                      "line": line, "detail": detail, "signal": signal})
        return eid

    for comp in scan.get("components", []):
        kind = KIND_BY_CATEGORY.get(comp["category"], comp["category"])
        name = NAME_ALIASES.get(comp["name"], comp["name"])
        if comp["category"] == "vector":
            signal = "vector"
        else:
            signal = SIGNAL_BY_NAME.get(comp["name"])
        for ev in comp["evidence"]:
            if ev.get("test_file"):
                continue
            eid = add(kind, name, ev["file"], ev.get("line"),
                      "%s found via %s" % (comp["name"], ev.get("via", "scan")), signal)
            if eid:
                by_component.setdefault("%s|%s" % (comp["category"], comp["name"]), []).append(eid)
    infra = scan.get("infrastructure", {})
    for df in infra.get("dockerfiles", []):
        add("container", "Dockerfile", df["file"], 1, "base image: " + ", ".join(df.get("base_images", [])), "container")
        for port in df.get("exposed_ports", []):
            add("port", "Exposed port", df["file"], 1, "EXPOSE " + str(port), None)
    for path in infra.get("compose_files", []):
        add("container", "Docker Compose", path, 1, "compose file defines a multi-container deployment", "container")
    for path in infra.get("kubernetes_files", []):
        add("container", "Kubernetes", path, 1, "Kubernetes workload manifest", "container")
    for proxy in infra.get("nginx_proxies", []):
        add("proxy", "nginx", proxy["file"], proxy.get("line"), "proxy_pass directive", "reverse_proxy")
    for rtype in infra.get("terraform_resources", {}):
        add("iac", "Terraform " + rtype, "(terraform)", None, "resource " + rtype, "terraform")
    for key, entries in scan.get("signal_evidence", {}).items():
        spec = SIGNAL_KINDS.get(key)
        if not spec:
            continue
        for ev in entries:
            add(spec[0], spec[1], ev["file"], ev.get("line"), "%s (value redacted)" % ev.get("note", key), spec[2])
    for lang, count in sorted(scan.get("languages", {}).items(), key=lambda kv: (-kv[1], kv[0])):
        add("language", LANG_NAMES.get(lang, lang), ".", None, "%d source files" % count)
    return {"scanner_version": scan.get("scanner_version"), "evidence": items, "by_component": by_component}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Flatten a scan into numbered evidence.")
    ap.add_argument("scan")
    ap.add_argument("--output")
    args = ap.parse_args(argv)
    try:
        doc = flatten(json.loads(Path(args.scan).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError) as error:
        sys.stderr.write("error: %s\n" % error)
        return 2
    text = json.dumps(doc, indent=2, ensure_ascii=True) + "\n"
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_bytes(text.encode("utf-8"))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
