#!/usr/bin/env python3
"""Optional: export Mermaid diagrams to SVG or PNG images.

Standard library only; needs the Mermaid CLI (`mmdc`, from `npm install -g @mermaid-js/mermaid-cli`)
on PATH. Diagrams are validated with diagram_sanitizer first, so broken diagrams are never rendered.

Usage: python render_diagram.py FILE [--format svg|png] [--out-dir DIR]
Exit codes: 0 rendered, 1 invalid diagram or render failure, 2 usage error, 3 mmdc not installed.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brand  # noqa: E402
import diagram_sanitizer  # noqa: E402

NO_MMDC = (
    "Mermaid CLI (mmdc) not found. The diagrams are still valid Mermaid text. To view them:\n"
    "  - paste them into https://mermaid.live, or open the .md file on GitHub or in VS Code, or\n"
    "  - install the CLI: npm install -g @mermaid-js/mermaid-cli, then rerun this command."
)


def extract_blocks(text):
    lines = text.replace("\r\n", "\n").split("\n")
    blocks = diagram_sanitizer.find_blocks(lines)
    if blocks is None:
        return ["\n".join(lines)]
    return ["\n".join(lines[a:b]) for a, b in blocks]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export Mermaid diagrams to SVG or PNG.")
    ap.add_argument("file")
    ap.add_argument("--format", choices=("svg", "png"), default="svg")
    ap.add_argument("--out-dir", default=None, help="default: next to the input file")
    args = ap.parse_args(argv)
    try:
        with open(args.file, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        print("error: cannot read %s: %s" % (args.file, exc), file=sys.stderr)
        return 2
    _, reported, remaining = diagram_sanitizer.sanitize_text(text, fix=False)
    if remaining:
        for f in reported:
            if f["severity"] == "error":
                print("%s:%d: [%s] %s" % (args.file, f["line"], f["rule"], f["message"]), file=sys.stderr)
        print("error: fix the diagram errors first (diagram_sanitizer.py --fix)", file=sys.stderr)
        return 1
    mmdc = shutil.which("mmdc")
    if not mmdc:
        print(NO_MMDC, file=sys.stderr)
        return 3
    out_dir = os.path.abspath(args.out_dir or os.path.dirname(os.path.abspath(args.file)))
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(args.file))[0]
    blocks = extract_blocks(text)
    for n, block in enumerate(blocks, 1):
        out = os.path.join(out_dir, "%s-%d.%s" % (stem, n, args.format) if len(blocks) > 1
                           else "%s.%s" % (stem, args.format))
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "diagram.mmd")
            with open(src, "w", encoding="utf-8") as fh:
                fh.write(block)
            proc = subprocess.run([mmdc, "-i", src, "-o", out], capture_output=True, text=True)
        if proc.returncode != 0:
            print("error: mmdc failed on diagram %d: %s" % (n, (proc.stderr or proc.stdout).strip()[:300]),
                  file=sys.stderr)
            return 1
        if args.format == "svg":
            with open(out, "r", encoding="utf-8") as fh:
                stamped = brand.stamp_svg(fh.read())
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(stamped)
        else:
            print("note: PNG export is not branded; use --format svg for the SolsArch corner mark", file=sys.stderr)
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
