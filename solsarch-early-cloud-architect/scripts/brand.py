#!/usr/bin/env python3
"""SolsArch brand mark for generated diagrams.

Standard library only. The mark is a small (about 7 KB) PNG embedded here, so installed copies of the skill
carry no image files. stamp_svg() adds it to the bottom-right corner of any SVG, in a footer strip below the
drawing so it never covers content. Stamping twice does nothing.

Usage: python brand.py IN.svg [OUT.svg]     stamp one SVG (in place when OUT is omitted)
"""
import base64
import re
import sys
from pathlib import Path

MARK_WIDTH, MARK_HEIGHT = 142, 160
MARKER = "data-solsarch-brand"
STRIP = 60
LOGO_PNG_B64 = (
    "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"
)


def data_uri():
    return "data:image/png;base64," + LOGO_PNG_B64


def png_bytes():
    return base64.b64decode(LOGO_PNG_B64)


def html_badge(size=40):
    """An <img> for HTML pages; place it with CSS (see CORNER_CSS)."""
    return '<img class="solsarch-mark" src="%s" alt="SolsArch" width="%d">' % (data_uri(), size)


CORNER_CSS = (".solsarch-figure{position:relative;padding-bottom:48px}"
              ".solsarch-mark{position:absolute;right:8px;bottom:6px;height:auto;background:#fff;border-radius:6px;padding:2px}")


def _viewbox(root_tag):
    match = re.search(r'viewBox\s*=\s*"([^"]+)"', root_tag)
    if not match:
        return None
    try:
        x, y, w, h = [float(v) for v in match.group(1).replace(",", " ").split()]
    except ValueError:
        return None
    return x, y, w, h


WATERMARK_LINE = "Prepared with SolsArch Early Cloud Architect | https://solsarch.com/ | https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect"


def watermark_markdown(text):
    """Add the SolsArch line at the end of a Markdown document once."""
    if WATERMARK_LINE in text:
        return text
    return text.rstrip(chr(10)) + chr(10) * 2 + "---" + chr(10) + "*" + WATERMARK_LINE + "*" + chr(10)


def stamp_svg(svg):
    """Return svg with the SolsArch mark in the bottom-right corner. Unparseable input is returned unchanged."""
    if MARKER in svg:
        return svg
    start = svg.find("<svg")
    end = svg.find(">", start) if start >= 0 else -1
    close = svg.rfind("</svg>")
    if start < 0 or end < 0 or close < 0:
        return svg
    root = svg[start:end + 1]
    box = _viewbox(root)
    if box is None:
        return svg
    x, y, w, h = box
    new_h = h + STRIP
    size = STRIP - 12
    width = size * MARK_WIDTH / MARK_HEIGHT
    new_root = re.sub(r'viewBox\s*=\s*"[^"]+"', 'viewBox="%g %g %g %g"' % (x, y, w, new_h), root, count=1)
    height_match = re.search(r'\sheight\s*=\s*"([0-9.]+)(px)?"', new_root)
    if height_match:
        scaled = float(height_match.group(1)) * new_h / h
        new_root = new_root.replace(height_match.group(0), ' height="%g"' % round(scaled, 2), 1)
    footer = ('<g %s="true"><rect x="%g" y="%g" width="%g" height="%d" fill="#ffffff"/>'
              '<text x="%g" y="%g" font-family="system-ui,Segoe UI,Arial,sans-serif" font-size="13" font-weight="600" '
              'text-anchor="end" fill="#14202b">SolsArch</text>'
              '<image href="%s" x="%g" y="%g" width="%.1f" height="%d"><title>SolsArch</title></image></g>') % (
        MARKER, x, y + h, w, STRIP, x + w - width - 18, y + h + STRIP / 2 + 5, data_uri(), x + w - width - 10,
        y + h + 6, width, size)
    return svg[:start] + new_root + svg[end + 1:close] + footer + svg[close:]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or len(argv) > 2:
        sys.stderr.write("usage: brand.py IN.svg [OUT.svg]" + chr(10))
        return 2
    source = Path(argv[0])
    try:
        text = source.read_text(encoding="utf-8")
        Path(argv[1] if len(argv) == 2 else source).write_bytes(stamp_svg(text).encode("utf-8"))
    except OSError as error:
        sys.stderr.write("error: %s" % error + chr(10))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
