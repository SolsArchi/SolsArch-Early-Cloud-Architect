#!/usr/bin/env python3
"""Bundle every SolsArch output into one zip.

Contents: the report, the architecture document, each diagram as a .mmd file, a
browser viewer for the diagrams (index.html), the plan, the scan, the web-check facts, the
decision records and a README that says what each file is. The zip is reproducible: the
same files give the same bytes.

Standard library only. Usage:
    python package_outputs.py --repo REPO [--output FILE]

Exit codes: 0 written, 1 a required file is missing.
"""

import argparse
import html
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brand  # noqa: E402

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
README = """SolsArch Early Cloud Architect output
=====================================

solsarch-report.md        The full report: verdict, why each service was chosen, every diagram
                          with an explanation, security findings, web checks, cost drivers.
solsarch-architecture.md  The diagrams with their explanations, on their own.
diagrams/*.svg            Pictures with a service icon on every box (infrastructure and service
                          cards). Open in any browser, or insert into a document or slide.
diagrams/*.mmd            Each other diagram as Mermaid source. Paste into any Mermaid viewer.
diagrams/index.html       Open in a browser to see everything drawn. The icon pictures work
                          offline; the Mermaid diagrams need internet access once, to load
                          the Mermaid library.
data/plan.json            The machine-readable plan: services, evidence, alternatives.
data/scan.json            What the scanner found in the code.
data/facts.json           Which service names were checked on the web, with sources.
data/decisions.json       The saved decision records.
solsarch-deploy-guide.md  Step-by-step deployment guide for someone who has never deployed (terminal and
                          console paths, written for your account type). deploy-guide.html is the same,
                          ready to print.
solsarch-costs.md         The monthly cost estimate and what is live, estimated or unpriced.
solsarch-iac.md           What infrastructure code was written and the capability checklist result.
solsarch-ops-scorecard.md Operations maturity across DevOps, DevSecOps, MLOps, AIOps, LLMOps and AgentOps.
solsarch-executive-deck.pptx / .html and solsarch-executive-summary.pdf
                          A short deck (PowerPoint, or print the HTML to PDF) and a two-page summary for leaders.
exports/                  The architecture for Structurizr, PlantUML, LikeC4, draw.io, Backstage, Score and Port.
infrastructure/           Terraform, Ansible, Kubernetes, Helm, Dockerfiles, platform configuration files
                          and the GitHub Actions pipeline for the chosen provider.

The same code, cloud and compute model always produce the same plan and diagrams.
"""


def viewer(diagrams, pictures=()):
    blocks = ["<section><h2>%s</h2><img src=\"%s\" alt=\"%s\" style=\"max-width:100%%;height:auto\"></section>" % (
        html.escape(path.stem.replace("-", " ").title()), html.escape(path.name), html.escape(path.stem.replace("-", " ")))
        for path in pictures]
    for path in diagrams:
        blocks.append("<section><h2>%s</h2><figure class=\"solsarch-figure\" style=\"margin:0\"><pre class=\"mermaid\">%s</pre>%s</figure></section>" % (
            html.escape(path.stem.split("-", 1)[-1].replace("-", " ").title()), html.escape(path.read_text(encoding="utf-8")),
            brand.html_badge()))
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>SolsArch diagrams</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px;background:#fff;color:#14202e}"
            "section{margin:0 0 32px;overflow-x:auto}h1,h2{font-weight:600}" + brand.CORNER_CSS + "</style></head><body>"
            "<h1>SolsArch diagrams</h1>" + ("\n".join(blocks) or "<p>No diagrams were drawn for this repository.</p>") +
            "<script type=\"module\">import mermaid from \"%s\";"
            "mermaid.initialize({startOnLoad:true,securityLevel:\"strict\"});</script></body></html>\n" % MERMAID_CDN)


def extras(repo, report_text):
    """Files that need the flat evidence: the interactive report, the evidence data and the icon picture."""
    state = Path(repo) / ".solsarch"
    files = {}
    try:
        import package_report
        title = (report_text.splitlines() or ["SolsArch report"])[0].lstrip("# ").strip()
        assets = {}
        icon = state / "architecture-icons.svg"
        if icon.exists():
            assets["images/architecture-icons.svg"] = icon.read_text(encoding="utf-8")
            files["images/architecture-icons.svg"] = icon.read_bytes()
        files["report.html"] = package_report.build_html(title, report_text, assets).encode("utf-8")
    except Exception as error:  # the zip must still be written if the viewer cannot be built
        files["report-html-error.txt"] = ("report.html was not built: %s" % error + chr(10)).encode("utf-8")
    for name, target in (("evidence.json", "data/evidence.json"), ("placements.json", "data/placements.json")):
        if (state / name).exists():
            files[target] = (state / name).read_bytes()
    return files


def deploy_files(repo, docs, state):
    """The infrastructure code, the deploy guide, the cost report and the capability report, when they were built."""
    files = {}
    root = state / "iac"
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files["infrastructure/" + path.relative_to(root).as_posix()] = path.read_bytes()
    exports = state / "exports"
    if exports.is_dir():
        for path in sorted(exports.rglob("*")):
            if path.is_file():
                files["exports/" + path.relative_to(exports).as_posix()] = path.read_bytes()
    for name in ("solsarch-executive-deck.pptx", "solsarch-executive-deck.html", "solsarch-executive-summary.pdf"):
        if (docs / name).exists():
            files[name] = (docs / name).read_bytes()
    for name in ("solsarch-deploy-guide.md", "solsarch-iac.md", "solsarch-costs.md", "solsarch-validation.md", "solsarch-ops-scorecard.md"):
        source = docs / name
        if source.exists():
            text = brand.watermark_markdown(source.read_text(encoding="utf-8"))
            files[name] = text.encode("utf-8")
            if name == "solsarch-deploy-guide.md":
                try:
                    import package_report
                    title = (text.splitlines() or ["Deploy guide"])[0].lstrip("# ").strip()
                    files["deploy-guide.html"] = package_report.build_html(title, text, {}).encode("utf-8")
                except Exception as error:  # the zip must still be written if the page cannot be built
                    files["deploy-guide-html-error.txt"] = ("deploy-guide.html was not built: %s" % error + chr(10)).encode("utf-8")
    return files


def collect(repo):
    repo = Path(repo)
    docs = repo / "docs"
    state = repo / ".solsarch"
    required = [docs / "solsarch-report.md", docs / "solsarch-architecture.md", state / "scan.json", state / "plan.json"]
    missing = [str(p) for p in required if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing: " + ", ".join(missing) + ". Run the build step first.")
    diagrams = sorted((docs / "diagrams").glob("*.mmd"))
    pictures = sorted((docs / "diagrams").glob("*.svg"))
    logo = docs / "diagrams" / "solsarch-logo.png"
    files = {
        "README.txt": README.encode("utf-8"),
        "solsarch-report.md": required[0].read_bytes(),
        "solsarch-architecture.md": required[1].read_bytes(),
        "data/scan.json": required[2].read_bytes(),
        "data/plan.json": required[3].read_bytes(),
        "diagrams/index.html": viewer(diagrams, pictures).encode("utf-8"),
    }
    for path in diagrams + pictures + ([logo] if logo.exists() else []):
        files["diagrams/" + path.name] = path.read_bytes()
    facts = state / "facts.json"
    files["data/facts.json"] = facts.read_bytes() if facts.exists() else b'{"facts": [], "skipped": null}\n'
    memory = state / "memory.json"
    decisions = []
    if memory.exists():
        try:
            decisions = json.loads(memory.read_text(encoding="utf-8")).get("architectural_decisions", [])
        except ValueError:
            decisions = []
    files.update(extras(repo, required[0].read_text(encoding="utf-8")))
    files.update(deploy_files(repo, docs, state))
    files["data/decisions.json"] = (json.dumps(decisions, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return files


def write_zip(files, target):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, files[name])
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bundle SolsArch outputs into one zip")
    parser.add_argument("--repo", default=".", help="Project root (default: current directory)")
    parser.add_argument("--output", help="Zip path (default: REPO/docs/solsarch-output.zip)")
    args = parser.parse_args(argv)
    output = Path(args.output) if args.output else Path(args.repo) / "docs" / "solsarch-output.zip"
    try:
        files = collect(args.repo)
        write_zip(files, output)
    except (OSError, FileNotFoundError) as error:
        sys.stdout.write(json.dumps({"status": "error", "message": str(error)}) + "\n")
        return 1
    sys.stdout.write(json.dumps({"status": "written", "zip": str(output), "files": sorted(files)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
