#!/usr/bin/env python3
"""Bundle the finished review into one zip: report, diagrams, images, data.

Standard library only. The report is verified first; a report that fails verification is not packaged
unless --force is given.

Zip layout (inside a single top-level folder named after --name):
  README.txt        what is inside and how to view it
  report.md         the full report
  report.html       the same report as a web page that draws the diagrams (needs internet for Mermaid)
  diagrams/*.mmd    one Mermaid source file per diagram
  images/           SVG images of the diagrams (a note instead if the Mermaid CLI is not installed)
  data/             evidence.json, placements.json, research.json, memory.json when present

Usage: python package_report.py REPORT.md --evidence evidence.json --name NAME
         [--placements P.json] [--research R.json] [--project-root DIR] [--out-dir DIR] [--force]
Exit codes: 0 zip written, 1 report failed verification, 2 usage or input error.
"""
import argparse
import contextlib
import html
import io
import json
import os
import re
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brand  # noqa: E402
import icon_diagram  # noqa: E402
import render_diagram  # noqa: E402
import verify_report  # noqa: E402

MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def slug(text, fallback="diagram"):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:50] or fallback


def extract_diagrams(report_text):
    """Return [(name, mermaid_source)] named by the nearest preceding heading."""
    lines = report_text.replace("\r\n", "\n").split("\n")
    out, heading, i, used = [], "diagram", 0, set()
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("#"):
            heading = s.lstrip("#").strip()
        elif s.lower().startswith("```mermaid"):
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            base = slug(heading)
            name, n = base, 2
            while name in used:
                name, n = "%s-%d" % (base, n), n + 1
            used.add(name)
            out.append((name, "\n".join(lines[i + 1:j]).strip() + "\n"))
            i = j
        i += 1
    return out


def inline(text):
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def markdown_to_html(md, assets=None):
    assets = assets or {}
    lines = md.replace("\r\n", "\n").split("\n")
    out, i, para, in_list = [], 0, [], None

    def flush_para():
        if para:
            out.append("<p>%s</p>" % inline(" ".join(para)))
            para.clear()

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</%s>" % in_list)
            in_list = None

    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if s.startswith("```"):
            flush_para()
            close_list()
            kind = s[3:].strip().lower()
            j = i + 1
            body = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                body.append(lines[j])
                j += 1
            text = html.escape("\n".join(body), quote=False)
            out.append('<figure class="solsarch-figure"><pre class="mermaid">%s</pre>%s</figure>' % (text, brand.html_badge())
                       if kind == "mermaid"
                       else "<pre><code>%s</code></pre>" % text)
            i = j + 1
            continue
        if s.startswith("|") and s.endswith("|"):
            flush_para()
            close_list()
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-{2,}:?", c) for c in r if c)]
            out.append("<div class=\"scroll\"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>" % (
                "".join("<th>%s</th>" % inline(c) for c in rows[0]),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r) for r in body)))
            continue
        img = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)$", s)
        if img:
            flush_para()
            close_list()
            alt, src = img.group(1), img.group(2)
            if src in assets:
                out.append('<figure class="icons">%s<figcaption>%s</figcaption></figure>' % (assets[src], html.escape(alt)))
            else:
                out.append('<p><img src="%s" alt="%s"></p>' % (html.escape(src, quote=True), html.escape(alt, quote=True)))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            flush_para()
            close_list()
            level = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (level, inline(m.group(2)), level))
        elif re.match(r"^[-*]\s+", s):
            flush_para()
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            item = re.sub(r"^[-*]\s+", "", s)
            box = ""
            checkbox = re.match(r"^\[(x| )\]\s*(.*)$", item, re.I)
            if checkbox:
                box = "&#10003; " if checkbox.group(1).lower() == "x" else "&#9744; "
                item = checkbox.group(2)
            out.append("<li>%s%s</li>" % (box, inline(item)))
        elif re.match(r"^\d+\.\s+", s):
            flush_para()
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append("<li>%s</li>" % inline(re.sub(r"^\d+\.\s+", "", s)))
        elif s in ("---", "***"):
            flush_para()
            close_list()
            out.append("<hr>")
        elif not s:
            flush_para()
            close_list()
        else:
            close_list()
            para.append(s)
        i += 1
    flush_para()
    close_list()
    return "\n".join(out)


def build_html(title, md, assets=None):
    body = markdown_to_html(md, assets)
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;max-width:960px;margin:0 auto;padding:24px 16px;color:#14202b;background:#fff}
h1,h2,h3,h4{line-height:1.25}h2{border-top:2px solid #14202b;padding-top:16px;margin-top:40px}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%%;font-size:.92rem;margin:12px 0}
th,td{border:1px solid #cfd8e0;padding:8px 10px;text-align:left;vertical-align:top}th{background:#eef3f8}
code{background:#eef3f8;padding:1px 5px;border-radius:3px;font-size:.9em}pre{overflow-x:auto}
figure.icons{margin:16px 0;overflow-x:auto;border:1px solid #cfd8e0;border-radius:6px;padding:8px;background:#fff}
figure.icons svg{height:auto;min-width:900px}figcaption{font-size:.85rem;color:#566574}
%s
pre.mermaid{background:#f7f9fb;border:1px solid #cfd8e0;border-radius:6px;padding:16px;text-align:center}
@media (prefers-color-scheme:dark){body{background:#0e1620;color:#e4ebf2}th{background:#17304a}td,th{border-color:#2a3947}
code{background:#17304a}pre.mermaid{background:#15212d;border-color:#2a3947}h2{border-color:#e4ebf2}}
</style></head><body>
%s
<script src="%s"></script>
<script>if(window.mermaid){mermaid.initialize({startOnLoad:true,securityLevel:"strict"});}</script>
</body></html>
""" % (html.escape(title), brand.CORNER_CSS, body, MERMAID_CDN)


def render_images(diagrams, workdir):
    """Try to render each diagram to SVG. Returns {name: svg_path}; empty if the Mermaid CLI is missing."""
    images = {}
    for name, source in diagrams:
        src = os.path.join(workdir, name + ".mmd")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(source)
        out_dir = os.path.join(workdir, "img")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = render_diagram.main([src, "--format", "svg", "--out-dir", out_dir])
        if code == 3:
            return {}
        svg = os.path.join(out_dir, name + ".svg")
        if code == 0 and os.path.isfile(svg):
            images[name] = svg
    return images


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def readme_text(name, cloud, model, have_images):
    lines = [
        "%s - cloud architecture review" % name,
        "",
        "report.md        The full review: choices, why each service was chosen, blockers, diagrams with",
        "                 explanations, web sources.",
        "report.html      The same report as a web page. Open it in a browser; it draws the diagrams",
        "                 (needs an internet connection for the Mermaid library).",
        "diagrams/        Mermaid source for each diagram. Paste into https://mermaid.live to edit.",
        "images/          architecture-icons.svg (the architecture with a service icon on every box)"
        + (" and an SVG of every Mermaid diagram." if have_images else
           ". The Mermaid diagram images were not made because the Mermaid CLI is not installed; open report.html."),
        "data/            The evidence, service placements and web sources the report is based on.",
    ]
    if cloud:
        lines += ["", "Cloud: %s   Compute model: %s" % (cloud, model)]
    return "\n".join(lines) + "\n"


def package(report_path, evidence_path, name, placements_path=None, research_path=None,
            project_root=None, out_dir=None, force=False, icon_dir=None):
    report = read_text(report_path)
    evidence = json.loads(read_text(evidence_path))
    placements = json.loads(read_text(placements_path)) if placements_path else None
    research = None
    if research_path:
        research = json.loads(read_text(research_path)) if os.path.isfile(research_path) else {"entries": []}
    problems = verify_report.check(report, evidence, placements, research)
    errors = [p for p in problems if p["severity"] == "error"]
    if errors and not force:
        return None, errors
    diagrams = extract_diagrams(report)
    name = slug(name, "project")
    out_dir = os.path.abspath(out_dir or os.getcwd())
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, "%s-architecture.zip" % name)
    members = []
    with tempfile.TemporaryDirectory() as tmp:
        images = render_images(diagrams, tmp) if diagrams else {}
        icon_svg = None
        if placements:
            try:
                icon_svg = icon_diagram.render(placements, evidence, icon_dir)
            except (ValueError, KeyError):
                icon_svg = None
        members.append(("README.txt", readme_text(name, placements and placements.get("cloud"),
                                                  placements and placements.get("compute_model"),
                                                  bool(images)).encode("utf-8")))
        members.append(("report.md", report.encode("utf-8")))
        svg_assets = {"images/architecture-icons.svg": icon_svg} if icon_svg else {}
        members.append(("report.html", build_html(name + " architecture review", report, svg_assets).encode("utf-8")))
        if icon_svg:
            members.append(("images/architecture-icons.svg", icon_svg.encode("utf-8")))
        for dname, source in diagrams:
            members.append(("diagrams/%s.mmd" % dname, source.encode("utf-8")))
        if images:
            for iname, path in sorted(images.items()):
                with open(path, "rb") as fh:
                    members.append(("images/%s.svg" % iname, fh.read()))
        else:
            members.append(("images/README.txt", (
                "The Mermaid diagram images were not generated (Mermaid CLI not found).\n"
                "architecture-icons.svg (if present) is the icon diagram and needs no extra tools.\n"
                "Open ../report.html to see every diagram, or install the CLI and run render_diagram.py.\n").encode("utf-8")))
        members.append(("data/evidence.json", read_text(evidence_path).encode("utf-8")))
        if placements_path:
            members.append(("data/placements.json", read_text(placements_path).encode("utf-8")))
        if research_path and os.path.isfile(research_path):
            members.append(("data/research.json", read_text(research_path).encode("utf-8")))
        if project_root:
            mem = os.path.join(project_root, ".solsarch", "memory.json")
            if os.path.isfile(mem):
                members.append(("data/memory.json", read_text(mem).encode("utf-8")))
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arc, data in members:
                info = zipfile.ZipInfo("%s/%s" % (name, arc), FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zf.writestr(info, data)
    return zip_path, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="Package the review into one zip.")
    ap.add_argument("report")
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--name", required=True, help="project name used for the folder and zip")
    ap.add_argument("--placements")
    ap.add_argument("--research")
    ap.add_argument("--project-root", help="project folder holding .solsarch/memory.json")
    ap.add_argument("--out-dir")
    ap.add_argument("--icon-dir", help="folder of official icon SVGs to use instead of the built-in glyphs")
    ap.add_argument("--force", action="store_true", help="package even if verification fails")
    args = ap.parse_args(argv)
    try:
        zip_path, errors = package(args.report, args.evidence, args.name, args.placements, args.research,
                                   args.project_root, args.out_dir, args.force, args.icon_dir)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if zip_path is None:
        for p in errors:
            print("error: %s" % p["message"], file=sys.stderr)
        print("report failed verification; fix it or use --force", file=sys.stderr)
        return 1
    print(zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
