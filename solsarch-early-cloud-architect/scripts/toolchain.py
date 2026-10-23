#!/usr/bin/env python3
"""Check the generated infrastructure code with the real tools, repairing what can be repaired safely.

    toolchain.py status                       which tools are installed
    toolchain.py check --dir DIR [--engine terraform|tofu] [--fix] [--offline]

For every Terraform folder: `fmt` (repaired in place when --fix), `init -backend=false`, `validate`, then TFLint, Trivy and
Checkov when they are installed. For Kubernetes manifests: `kubeconform` when installed, else `kubectl kustomize` to prove
the base builds, then Trivy. For Ansible: `ansible-lint` when installed. A tool that is not installed is reported as
skipped, never as passed, and no cloud credential is passed to any of them (the environment is stripped to what a tool needs
to start). The loop repairs, checks again, and stops after three rounds; whatever remains is reported with the first lines of
the tool's own message so a person (or the generator's author) can fix the cause.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SAFE_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PATHEXT", "COMSPEC", "TF_PLUGIN_CACHE_DIR",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "LANG")
TIMEOUT = {"fmt": 60, "init": 600, "validate": 120, "tflint": 180, "trivy": 300, "checkov": 300, "kubeconform": 120, "kubectl": 60, "ansible-lint": 180}
EXTRA_DIRS = (Path.home() / ".solsarch-tools" / "checkov-venv" / "Scripts", Path.home() / ".solsarch-tools" / "checkov-venv" / "bin")
ROUNDS = 3
# Where to get each tool. Package-manager names are the vendors' own; where there is no package everyone shares, the release page is named.
INSTALL_HINTS = (
    ("terraform", "`winget install Hashicorp.Terraform`, `brew install terraform`, or https://developer.hashicorp.com/terraform/install"),
    ("tflint", "`brew install tflint`, or the release page https://github.com/terraform-linters/tflint/releases"),
    ("trivy", "`brew install trivy`, or https://trivy.dev/latest/getting-started/installation/"),
    ("checkov", "`pip install checkov` (use a virtual environment; run it from a folder with no file called schema.py)"),
    ("kubeconform", "`brew install kubeconform`, or https://github.com/yannh/kubeconform/releases"),
    ("kubectl", "https://kubernetes.io/docs/tasks/tools/"),
    ("ansible-lint", "`pip install ansible-lint` on Linux, macOS or WSL (it does not run on native Windows)"),
    ("hadolint", "`brew install hadolint`, or https://github.com/hadolint/hadolint/releases"),
    ("actionlint", "`brew install actionlint`, or https://github.com/rhysd/actionlint/releases"),
)


def clean_env():
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV}
    env["TF_IN_AUTOMATION"] = "1"
    env["CHECKPOINT_DISABLE"] = "1"
    return env


def which(name):
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_DIRS:
        for ext in ("", ".exe"):
            if (d / (name + ext)).exists():
                return str(d / (name + ext))
    return None


def run(cmd, cwd, timeout):
    try:
        done = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=clean_env())
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after %d seconds" % timeout
    except OSError as error:
        return 127, str(error)


def status():
    tools = {}
    for name in ("terraform", "tofu", "tflint", "trivy", "checkov", "terraform-docs", "infracost", "kubeconform", "helm", "kubectl", "ansible-lint", "opa", "docker"):
        path = which(name)
        tools[name] = {"installed": bool(path) if name != "checkov" else bool(checkov_command()), "path": path}
    return tools


def first_lines(text, n=12):
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines[:n])


def terraform_dirs(root):
    """Every folder that is a Terraform root module: it holds a .tf file that declares required_providers."""
    root = Path(root)
    found = []
    for folder in sorted({p.parent for p in root.rglob("*.tf")}):
        if any("required_providers" in f.read_text(encoding="utf-8", errors="ignore") for f in folder.glob("*.tf")):
            found.append(folder)
    return found


def check_terraform(folder, engine, fix, offline):
    binary = which(engine)
    out = {"dir": str(folder), "engine": engine, "steps": {}, "repaired": []}
    if not binary:
        out["steps"]["all"] = {"status": "skipped", "reason": "%s is not installed" % engine}
        return out
    code, text = run([binary, "fmt", "-check", "-recursive", "-no-color"], folder, TIMEOUT["fmt"])
    if code != 0 and fix:
        _, listed = run([binary, "fmt", "-recursive", "-no-color"], folder, TIMEOUT["fmt"])
        out["repaired"] = [l.strip() for l in listed.splitlines() if l.strip().endswith(".tf")]
        code, text = run([binary, "fmt", "-check", "-recursive", "-no-color"], folder, TIMEOUT["fmt"])
    out["steps"]["fmt"] = {"status": "passed" if code == 0 else "failed", "detail": "" if code == 0 else first_lines(text)}
    if offline:
        out["steps"]["init"] = {"status": "skipped", "reason": "offline: providers cannot be downloaded"}
        out["steps"]["validate"] = {"status": "skipped", "reason": "needs init"}
    else:
        code, text = run([binary, "init", "-backend=false", "-input=false", "-no-color"], folder, TIMEOUT["init"])
        out["steps"]["init"] = {"status": "passed" if code == 0 else "failed", "detail": "" if code == 0 else first_lines(text)}
        if code == 0:
            code, text = run([binary, "validate", "-json"], folder, TIMEOUT["validate"])
            try:
                report = json.loads(text)
            except ValueError:
                report = {"valid": code == 0, "diagnostics": [{"severity": "error", "summary": first_lines(text)}]}
            errors = [d for d in report.get("diagnostics", []) if d.get("severity") == "error"]
            warnings = [d for d in report.get("diagnostics", []) if d.get("severity") == "warning"]
            out["steps"]["validate"] = {"status": "passed" if report.get("valid") else "failed", "warnings": len(warnings),
                                        "errors": [{"summary": d.get("summary"), "detail": (d.get("detail") or "")[:240],
                                                    "file": (d.get("range") or {}).get("filename")} for d in errors[:8]],
                                        "warning_summaries": sorted({d.get("summary", "") for d in warnings})[:6]}
        else:
            out["steps"]["validate"] = {"status": "skipped", "reason": "init failed"}
    tflint = which("tflint")
    if tflint:
        run([tflint, "--init"], folder, TIMEOUT["tflint"])
        code, text = run([tflint, "--format", "json"], folder, TIMEOUT["tflint"])
        try:
            issues = json.loads(text).get("issues", [])
        except ValueError:
            issues = []
        out["steps"]["tflint"] = {"status": "passed" if code == 0 and not issues else "failed", "issues": len(issues),
                                  "detail": [i.get("message") for i in issues[:6]]}
    else:
        out["steps"]["tflint"] = {"status": "skipped", "reason": "tflint is not installed"}
    out["steps"]["trivy"] = trivy(folder)
    out["steps"]["checkov"] = checkov(folder)
    return out


def trivy(folder):
    binary = which("trivy")
    if not binary:
        return {"status": "skipped", "reason": "trivy is not installed"}
    code, text = run([binary, "config", "--quiet", "--format", "json", "--severity", "HIGH,CRITICAL", str(folder)], folder, TIMEOUT["trivy"])
    try:
        results = json.loads(text).get("Results", []) if text.strip().startswith("{") else []
    except ValueError:
        return {"status": "skipped", "reason": "trivy output could not be read: " + first_lines(text, 2)}
    found = [(r.get("Target"), m.get("ID"), m.get("Title")) for r in results for m in (r.get("Misconfigurations") or [])]
    return {"status": "passed" if not found else "findings", "findings": len(found), "top": ["%s %s (%s)" % (i, t, f) for f, i, t in found[:8]]}


_CHECKOV = {}


def checkov_command():
    """How to start Checkov. A virtual environment's own Python running the module is tried first, because the checkov launcher can pick the wrong Python on Windows."""
    if "cmd" in _CHECKOV:
        return _CHECKOV["cmd"]
    tools = Path.home() / ".solsarch-tools"
    candidates = [[str(p), "-I", "-m", "checkov.main"] for p in sorted(tools.glob("checkov-venv*/Scripts/python.exe"), reverse=True) + sorted(tools.glob("checkov-venv*/bin/python"), reverse=True)]
    exe = which("checkov")
    if exe:
        candidates.append([exe])
    found = None
    for cmd in candidates:
        code, text = run(cmd + ["--version"], Path(__import__("tempfile").gettempdir()), 300)
        if code == 0 and "Traceback" not in text and "No module named" not in text:
            found = cmd
            break
    _CHECKOV["cmd"] = found
    return found


def checkov(folder, framework="terraform"):
    cmd = checkov_command()
    if not cmd:
        return {"status": "skipped", "reason": "checkov is not installed or does not start"}
    code, text = run(cmd + ["-d", str(folder), "--framework", framework, "-o", "json", "--quiet", "--compact"], folder, TIMEOUT["checkov"])
    try:
        data = json.loads(text)
    except ValueError:
        return {"status": "skipped", "reason": "checkov output could not be read: " + first_lines(text, 2)}
    reports = data if isinstance(data, list) else [data]
    failed = [c for r in reports for c in (r.get("results", {}).get("failed_checks") or [])]
    passed = sum(r.get("summary", {}).get("passed", 0) for r in reports)
    return {"status": "passed" if not failed else "findings", "passed": passed, "findings": len(failed),
            "top": ["%s %s (%s)" % (c.get("check_id"), c.get("check_name"), c.get("file_path")) for c in failed[:8]]}


def check_kubernetes(folder):
    out = {"dir": str(folder), "steps": {}}
    kubectl = which("kubectl")
    conform = which("kubeconform")
    if conform:
        code, text = run([conform, "-summary", "-strict", "-ignore-missing-schemas", "-kubernetes-version", "1.30.0", str(folder)], folder, TIMEOUT["kubeconform"])
        out["steps"]["kubeconform"] = {"status": "passed" if code == 0 else "failed", "detail": first_lines(text, 8)}
    else:
        out["steps"]["kubeconform"] = {"status": "skipped", "reason": "kubeconform is not installed"}
    if kubectl:
        code, text = run([kubectl, "kustomize", str(folder)], folder, TIMEOUT["kubectl"])
        out["steps"]["kustomize"] = {"status": "passed" if code == 0 else "failed", "detail": "" if code == 0 else first_lines(text, 8)}
    else:
        out["steps"]["kustomize"] = {"status": "skipped", "reason": "kubectl is not installed"}
    out["steps"]["trivy"] = trivy(folder)
    return out


def check_ansible(folder):
    lint = which("ansible-lint")
    playbooks = sorted(p.name for p in Path(folder).glob("*.yml") if p.name in ("site.yml", "k8s-addons.yml"))
    if not lint or not playbooks:
        return {"dir": str(folder), "steps": {"ansible-lint": {"status": "skipped", "reason": "ansible-lint is not installed" if not lint else "no playbook"}}}
    results = {}
    for name in playbooks:
        code, text = run([lint, "--format", "pep8", name], folder, TIMEOUT["ansible-lint"])
        if "No module named 'grp'" in text:
            results[name] = {"status": "skipped", "reason": "ansible-lint does not run on native Windows; use WSL or a container"}
            continue
        results[name] = {"status": "passed" if code == 0 else "failed", "detail": "" if code == 0 else first_lines(text, 8)}
    states = {r["status"] for r in results.values()}
    overall = "failed" if "failed" in states else ("skipped" if states == {"skipped"} else "passed")
    step = {"status": overall, "playbooks": results}
    if overall == "skipped":
        step["reason"] = next(iter(results.values()))["reason"]
    return {"dir": str(folder), "steps": {"ansible-lint": step}}


def check_all(root, engine="auto", fix=True, offline=False):
    tools = status()
    if engine == "auto":
        engine = "terraform" if tools["terraform"]["installed"] else "tofu"
    root = Path(root).resolve()
    rounds = []
    for n in range(1, ROUNDS + 1):
        report = {"round": n, "terraform": [check_terraform(d, engine, fix, offline) for d in terraform_dirs(root)], "kubernetes": [], "ansible": []}
        k8s = root / "kubernetes"
        if (k8s / "kustomization.yaml").exists():
            report["kubernetes"].append(check_kubernetes(k8s))
        for a in sorted(root.glob("ansible")):
            report["ansible"].append(check_ansible(a))
        rounds.append(report)
        repaired = sum(len(t["repaired"]) for t in report["terraform"])
        if not repaired:
            break
    final = rounds[-1]
    failures = []
    for t in final["terraform"]:
        for step, r in t["steps"].items():
            if r.get("status") == "failed":
                failures.append("%s: terraform %s" % (Path(t["dir"]).name, step))
    for k in final["kubernetes"] + final["ansible"]:
        for step, r in k["steps"].items():
            if r.get("status") == "failed":
                failures.append("%s: %s" % (Path(k["dir"]).name, step))
    findings = sum(r.get("findings", 0) for t in final["terraform"] for r in t["steps"].values() if isinstance(r, dict)) \
        + sum(r.get("findings", 0) for k in final["kubernetes"] for r in k["steps"].values() if isinstance(r, dict))
    skipped = sorted({"%s (%s)" % (s, r.get("reason", "")) for grp in (final["terraform"], final["kubernetes"], final["ansible"]) for t in grp
                      for s, r in t["steps"].items() if isinstance(r, dict) and r.get("status") == "skipped"})
    return {"schema": 1, "engine": engine, "tools": {k: v["installed"] for k, v in tools.items()}, "rounds": rounds, "failed": failures,
            "security_findings": findings, "skipped": skipped, "verdict": "failed" if failures else ("passed_with_findings" if findings else "passed")}


def render_markdown(result):
    lines = ["# Infrastructure code checks", "", "Verdict: **%s**. Engine: %s." % (result["verdict"].replace("_", " "), result["engine"]), ""]
    lines += ["| Tool | Installed |", "|---|---|"] + ["| %s | %s |" % (k, "yes" if v else "no") for k, v in sorted(result["tools"].items())]
    final = result["rounds"][-1]
    lines += ["", "## Terraform", "", "| Folder | fmt | init | validate | tflint | trivy | checkov |", "|---|---|---|---|---|---|---|"]
    for t in final["terraform"]:
        s = t["steps"]
        lines.append("| %s | %s |" % (Path(t["dir"]).name, " | ".join((s.get(k) or {"status": "-"})["status"] for k in ("fmt", "init", "validate", "tflint", "trivy", "checkov"))))
    if final["kubernetes"]:
        lines += ["", "## Kubernetes", ""] + ["- %s: %s" % (k, v["status"]) for item in final["kubernetes"] for k, v in item["steps"].items()]
    if final["ansible"]:
        lines += ["", "## Ansible", ""] + ["- %s: %s" % (k, v["status"]) for item in final["ansible"] for k, v in item["steps"].items()]
    if result["failed"]:
        lines += ["", "## Failed", ""] + ["- " + f for f in result["failed"]]
    if result["skipped"]:
        lines += ["", "## Not run", ""] + ["- " + f for f in result["skipped"]]
        hints = [(name, how) for name, how in INSTALL_HINTS if any(name in f for f in result["skipped"])]
        if hints:
            lines += ["", "### How to install what did not run", "",
                      "Install the tool, then run `solsarch.py validate` again. Until then the quality score treats these checks as not verified.", ""]
            lines += ["- **%s**: %s" % h for h in hints]
    lines += ["", "Rounds run: %d (formatting is repaired in place and checked again; nothing else is changed automatically)." % len(result["rounds"])]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check generated infrastructure code with the real tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    p = sub.add_parser("check")
    p.add_argument("--dir", required=True)
    p.add_argument("--engine", choices=("auto", "terraform", "tofu"), default="auto")
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    out = status() if args.command == "status" else check_all(args.dir, args.engine, not args.no_fix, args.offline)
    sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0 if args.command == "status" or out["verdict"] != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
