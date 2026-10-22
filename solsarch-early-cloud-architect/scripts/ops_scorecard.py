#!/usr/bin/env python3
"""A maturity scorecard for how the software is built and run, across six disciplines, from files in the repository.

    ops_scorecard.py --repo REPO

Disciplines (from references/knowledge/13_six_discipline_ops_engineering.md): DevOps, DevSecOps, MLOps, AIOps, LLMOps, AgentOps.
Each gets a level from 0 (ad hoc) to 4 (optimizing), counted from practices found in the files, never guessed. A discipline
that does not apply (no machine learning, no language model, no agent) is marked not applicable instead of scored zero. The
same files always give the same scorecard. Findings carry the rule codes from the framework (OPS-DOCKER-001 ...).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".solsarch", "vendor", "target", ".next", ".terraform"}
MAX_FILES, MAX_BYTES = 4000, 300000
LEVELS = ["Ad hoc", "Managed", "Defined", "Quantitatively managed", "Optimizing"]
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".rb", ".php", ".cs", ".rs", ".yml", ".yaml", ".json", ".toml", ".tf", ".txt",
            ".cfg", ".ini", ".sh", ".dockerfile", ".prompt", ".jinja", ".j2"}


def walk(root):
    count = 0
    for folder, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(names):
            path = Path(folder) / name
            if path.suffix.lower() in TEXT_EXT or name in ("Dockerfile", "Jenkinsfile", "Makefile", "Procfile") or name.startswith("Dockerfile"):
                count += 1
                if count > MAX_FILES:
                    return
                yield path


class Repo:
    def __init__(self, root):
        self.root = Path(root)
        self.files = {}
        for p in walk(self.root):
            rel = p.relative_to(self.root).as_posix()
            try:
                if p.stat().st_size <= MAX_BYTES:
                    self.files[rel] = p.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue

    def has_path(self, pattern):
        rx = re.compile(pattern, re.I)
        return sorted(p for p in self.files if rx.search(p))

    def grep(self, pattern, only=None):
        rx = re.compile(pattern, re.I)
        return sorted(p for p, t in self.files.items() if (only is None or re.search(only, p, re.I)) and rx.search(t))

    def workflows(self):
        return [p for p in self.files if re.search(r"(^|/)\.github/workflows/|\.gitlab-ci\.yml$|jenkinsfile$|azure-pipelines\.yml$|\.circleci/|bitbucket-pipelines\.yml$", p, re.I)]


def practice(name, found, evidence=()):
    return {"practice": name, "met": bool(found), "evidence": list(evidence)[:3]}


def devops(r):
    ci = r.workflows()
    docker = r.has_path(r"(^|/)dockerfile[^/]*$")
    multistage = [p for p in docker if r.files[p].count("\nfrom ") + (1 if r.files[p].startswith("from ") else 0) > 1]
    nonroot = [p for p in docker if re.search(r"^\s*user\s+(?!root\b)\S+", r.files[p], re.M)]
    iac = r.has_path(r"\.tf$|(^|/)template\.ya?ml$|(^|/)pulumi\.ya?ml$|\.bicep$|(^|/)cdk\.json$|(^|/)serverless\.ya?ml$")
    tests = r.has_path(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|\.(test|spec)\.[jt]sx?$|_test\.go$")
    ci_tests = [p for p in ci if re.search(r"pytest|npm (run )?test|yarn test|go test|jest|mvn test|gradle test|cargo test|rspec|unittest", r.files[p])]
    release = [p for p in ci if re.search(r"canary|blue.?green|rollback|deploy", r.files[p])]
    return [practice("A build and test pipeline exists", ci, ci), practice("Container images build in stages", multistage, multistage),
            practice("Containers run as a non-root user", docker and len(nonroot) == len(docker), nonroot),
            practice("Infrastructure is defined as code", iac, iac), practice("Automated tests exist", tests, tests[:3]),
            practice("The pipeline runs the tests", ci_tests, ci_tests), practice("The pipeline deploys or releases", release, release)]


def devsecops(r):
    ci = r.workflows()
    sast = r.grep(r"bandit|semgrep|codeql|sonarqube|sonarcloud|gosec|brakeman|eslint-plugin-security", None)
    sca = r.grep(r"trivy|snyk|dependabot|renovate|osv-scanner|npm audit|pip-audit|safety check|grype", None) + r.has_path(r"(^|/)(dependabot\.ya?ml|renovate\.json)$")
    lock = r.has_path(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|pipfile\.lock|go\.sum|cargo\.lock|gemfile\.lock|uv\.lock)$")
    secrets = r.grep(r"gitleaks|trufflehog|detect-secrets|git-secrets|secretlint", None) + r.has_path(r"(^|/)\.pre-commit-config\.ya?ml$")
    signing = r.grep(r"cosign|sigstore|slsa-github-generator|notation sign|attest", None)
    sbom = r.grep(r"cyclonedx|syft|spdx|sbom", None)
    return [practice("Dependency lockfiles are committed", lock, lock), practice("Static analysis (SAST) runs", sast, sast),
            practice("Dependencies are scanned for known flaws (SCA)", sca, sca), practice("Secrets are scanned for", secrets, secrets),
            practice("Images and builds are signed or attested", signing, signing), practice("A software bill of materials is produced", sbom, sbom),
            practice("Scanners run in the pipeline", ci and [p for p in ci if re.search(r"trivy|snyk|codeql|semgrep|bandit|checkov", r.files[p])], [])]


ML_RX = r"\b(torch|tensorflow|sklearn|scikit-learn|xgboost|lightgbm|keras|jax|transformers|pytorch-lightning|mlflow|wandb|dvc)\b"
LLM_RX = r"\b(openai|anthropic|langchain|litellm|llama.?index|google\.generativeai|vertexai|cohere|mistralai|ollama|groq|together|bedrock-runtime|ai-sdk|vercel/ai)\b"
AGENT_RX = r"\b(langgraph|autogen|crewai|openai-agents|smolagents|pydantic.?ai|mcp|agno|semantic.?kernel|llama.?index.*agent|agent(executor)?|react.?agent)\b"


def mlops(r):
    return [practice("Data and models are versioned", r.grep(r"\bdvc\b|delta.?lake|lakefs|pachyderm", None) + r.has_path(r"(^|/)(dvc\.ya?ml|\.dvc)"), []),
            practice("Experiments are tracked", r.grep(r"mlflow|wandb|weights.?&.?biases|neptune|comet_ml|clearml", None), []),
            practice("A model registry or staged releases exist", r.grep(r"model.?registry|mlflow.*register|sagemaker.*model.?package|vertex.*model|staging.*production", None), []),
            practice("Training or serving is automated", r.grep(r"kubeflow|airflow|prefect|dagster|sagemaker.*pipeline|vertex.*pipeline|zenml|metaflow", None), [])]


def aiops(r):
    return [practice("Tracing and metrics are emitted", r.grep(r"opentelemetry|otel|prometheus|statsd|datadog|newrelic|dd-trace|micrometer|opencensus", None), []),
            practice("Errors are reported", r.grep(r"sentry|rollbar|bugsnag|honeybadger|airbrake", None), []),
            practice("Alerts are defined as code", r.grep(r"alertmanager|prometheusrule|aws_cloudwatch_metric_alarm|azurerm_monitor_metric_alert|google_monitoring_alert_policy|alert_rule|pagerduty|opsgenie", None), []),
            practice("Anomaly detection or automatic remediation exists", r.grep(r"anomaly.?detect|self.?heal|auto.?remediat|eventbridge.*remediat|runbook", None), []),
            practice("Health endpoints exist", r.grep(r"/health|/healthz|/readyz|/livez|healthcheck", None), [])]


def llmops(r):
    return [practice("Prompts are versioned files or a registry", r.has_path(r"(^|/)prompts?/|\.prompt$|\.prompt\.[a-z]+$|prompts?\.(ya?ml|json)$") + r.grep(r"langfuse|promptlayer|prompthub|humanloop|langsmith", None), []),
            practice("An evaluation harness runs on prompt changes", r.grep(r"promptfoo|deepeval|ragas|trulens|langsmith.*evaluat|openai.?evals|inspect_ai|giskard", None) + r.has_path(r"(^|/)evals?/"), []),
            practice("Model fallbacks or a gateway exist", r.grep(r"litellm|portkey|helicone|fallbacks?\s*[=:]|openrouter|ai.?gateway|circuit.?breaker", None), []),
            practice("Token or cost limits are set", r.grep(r"max_tokens|max_output_tokens|max_completion_tokens|token.?budget|usage_limit", None), []),
            practice("Model calls are traced", r.grep(r"langfuse|langsmith|arize|phoenix|helicone|opentelemetry.*(llm|genai)|traceloop|openllmetry", None), []),
            practice("Guardrails or output validation exist", r.grep(r"guardrails|nemo.?guardrails|llm.?guard|pydantic.*(output|schema)|structured.?output|response_format|json_schema", None), [])]


def agentops(r):
    return [practice("Untrusted code runs in a sandbox", r.grep(r"e2b|pyodide|firecracker|wasmtime|gvisor|sandbox(ed)?|isolated-vm|docker.*(run|sandbox)", None), []),
            practice("Loops have a hard limit", r.grep(r"max_iterations|max_steps|recursion_limit|max_turns|max_loops|step_limit", None), []),
            practice("Risky actions need a person's approval", r.grep(r"human.?in.?the.?loop|hitl|interrupt_before|require_approval|approval|confirm_before|needs_review", None), []),
            practice("Tool permissions are narrow (allow-lists)", r.grep(r"allow.?list|allowed_tools|tool.?permissions|scopes?=|least.?privilege", None), []),
            practice("Agent runs are logged and replayable", r.grep(r"langgraph.*checkpoint|checkpointer|trace|audit.?log|run.?log", None), [])]


def level(practices):
    met = sum(1 for p in practices if p["met"])
    total = len(practices)
    if met == 0:
        return 0
    share = met / total
    return 1 if share < 0.34 else 2 if share < 0.6 else 3 if share < 0.85 else 4


def findings(r):
    out = []
    for p in r.has_path(r"(^|/)dockerfile[^/]*$"):
        text = r.files[p]
        if not re.search(r"^\s*user\s+(?!root\b)\S+", text, re.M):
            out.append({"rule": "OPS-DOCKER-001", "severity": "high", "where": p, "message": "The Dockerfile has no non-root USER, so the container runs as root.",
                        "fix": "Add USER nonroot (or a numeric user) after installing packages."})
        stages = text.count("\nfrom ") + (1 if text.startswith("from ") else 0)
        if stages <= 1 and re.search(r"\b(gcc|make|build-essential|npm install|pip install|go build|mvn )\b", text):
            out.append({"rule": "OPS-DOCKER-002", "severity": "medium", "where": p, "message": "Build tools stay in the final image (single-stage build).",
                        "fix": "Use a multi-stage build and copy only the built output into the final image."})
    llm = r.grep(LLM_RX, r"\.(py|js|ts|tsx|go|java|rb)$")
    if llm and not (r.grep(r"promptfoo|deepeval|ragas|langfuse|langsmith|trulens", None) or r.has_path(r"(^|/)(prompts?|evals?)/")):
        out.append({"rule": "OPS-LLM-001", "severity": "high", "where": llm[0], "message": "A language model SDK is used with no prompt versioning or evaluation harness.",
                    "fix": "Keep prompts as versioned files and run an evaluation harness (Promptfoo, Langfuse) on every prompt change."})
    unsafe = r.grep(r"\b(exec|eval)\s*\(|subprocess\.(run|popen|call)|os\.system", r"\.py$")
    agents = r.grep(AGENT_RX, r"\.(py|js|ts|tsx|go)$")
    if agents and unsafe and not r.grep(r"e2b|pyodide|firecracker|wasmtime|gvisor|sandbox", None):
        out.append({"rule": "OPS-AGENT-001", "severity": "critical", "where": agents[0], "message": "An agent runs with direct host access: it can execute code and no sandbox was found.",
                    "fix": "Run tool execution in a sandbox (Wasm, Firecracker or a container with no host access)."})
    return out


def build(repo):
    r = Repo(repo)
    ml = bool(r.grep(ML_RX, r"\.(py|txt|toml|yml|yaml|json)$"))
    llm = bool(r.grep(LLM_RX, None))
    agent = bool(r.grep(AGENT_RX, r"\.(py|js|ts|tsx|go|toml|txt|json)$")) and (llm or bool(r.grep(r"\bagent\b", r"\.(py|ts|js)$")))
    defs = [("DevOps", devops, True, "No build pipeline, tests or infrastructure code were found."),
            ("DevSecOps", devsecops, True, "No security scanning or dependency checks were found."),
            ("MLOps", mlops, ml, "No machine-learning libraries were found."),
            ("AIOps", aiops, True, "No tracing, error reporting or alerting was found."),
            ("LLMOps", llmops, llm, "No language-model SDK was found."),
            ("AgentOps", agentops, agent, "No agent framework was found.")]
    rows = []
    for name, fn, applies, why in defs:
        if not applies:
            rows.append({"discipline": name, "applicable": False, "reason": why, "level": None, "label": "Not applicable", "practices": []})
            continue
        ps = fn(r)
        lv = level(ps)
        rows.append({"discipline": name, "applicable": True, "level": lv, "label": LEVELS[lv], "practices": ps,
                     "next": [p["practice"] for p in ps if not p["met"]][:3]})
    scored = [x["level"] for x in rows if x["applicable"]]
    overall = int(sum(scored) / len(scored)) if scored else 0
    return {"schema": 1, "overall_level": overall, "overall_label": LEVELS[overall], "disciplines": rows, "findings": findings(r),
            "files_read": len(r.files), "method": "Counted from files in the repository; a practice is met only when a file shows it."}


def render_markdown(card):
    lines = ["# Operations maturity scorecard", "", "Overall: **level %d, %s** (0 ad hoc to 4 optimizing). %s" % (card["overall_level"], card["overall_label"], card["method"]), "",
             "| Discipline | Level | Met | Next improvements |", "|---|---|---|---|"]
    for d in card["disciplines"]:
        if not d["applicable"]:
            lines.append("| %s | n/a | | %s |" % (d["discipline"], d["reason"]))
        else:
            met = sum(1 for p in d["practices"] if p["met"])
            lines.append("| %s | %d, %s | %d of %d | %s |" % (d["discipline"], d["level"], d["label"], met, len(d["practices"]), "; ".join(d["next"]) or "-"))
    for d in card["disciplines"]:
        if d["applicable"]:
            lines += ["", "### %s" % d["discipline"], ""]
            lines += ["- [%s] %s%s" % ("x" if p["met"] else " ", p["practice"], (" (%s)" % ", ".join(p["evidence"][:2])) if p["evidence"] else "") for p in d["practices"]]
    if card["findings"]:
        lines += ["", "## Findings", "", "| Rule | Severity | Where | What to do |", "|---|---|---|---|"]
        lines += ["| %s | %s | %s | %s %s |" % (f["rule"], f["severity"], f["where"], f["message"], f["fix"]) for f in card["findings"]]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Operations maturity scorecard")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    sys.stdout.write(json.dumps(build(args.repo), indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
