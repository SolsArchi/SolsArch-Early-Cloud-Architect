#!/usr/bin/env python3
"""Draw the architecture as an SVG with a service icon on every box.

Standard library only. The layout is fixed code, so the same placements always give the same picture,
whatever model ran the skill. Icons are simple original glyphs coloured by cloud (they are not the
vendors' official logos). To use official icon sets, download them from the vendors, put SVG files in a
folder and pass --icon-dir; files named after the service (for example amazon-aurora-postgresql.svg), the
category (relational-database.svg) or the glyph (database.svg) replace the built-in glyph.

Usage: python icon_diagram.py PLACEMENTS.json --out diagram.svg [--evidence EVIDENCE.json]
         [--icon-dir DIR] [--title TEXT]
Exit codes: 0 written, 2 usage or input error.
"""
import argparse
import base64
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brand  # noqa: E402

VENDOR = {  # fill, dark text
    "aws": ("#FF9900", "#232F3E"),
    "azure": ("#0078D4", "#003A6C"),
    "gcp": ("#4285F4", "#1A4DA1"),
}
CLOUD_TITLE = {"aws": "AWS", "azure": "Microsoft Azure", "gcp": "Google Cloud"}

STROKE = 'fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"'
GLYPHS = {
    "user": '<circle cx="20" cy="14" r="6"/><path d="M8 34c0-7 5-11 12-11s12 4 12 11"/>',
    "globe": '<circle cx="20" cy="20" r="13"/><ellipse cx="20" cy="20" rx="6" ry="13"/><path d="M7 20h26"/>',
    "balancer": '<circle cx="20" cy="9" r="3"/><circle cx="9" cy="31" r="3"/><circle cx="20" cy="31" r="3"/>'
                '<circle cx="31" cy="31" r="3"/><path d="M20 12v5M20 17L9 28M20 17v11M20 17l11 11"/>',
    "container": '<rect x="7" y="10" width="26" height="20" rx="2"/><path d="M7 17h26M13 23.5h.1M19 23.5h.1"/>',
    "function": '<path d="M16 8c-4 0-4 3-4 6s-1 5-4 6c3 1 4 3 4 6s0 6 4 6M24 8c4 0 4 3 4 6s1 5 4 6c-3 1-4 3-4 6s0 6-4 6"/>',
    "database": '<ellipse cx="20" cy="10" rx="11" ry="4"/><path d="M9 10v20c0 2.5 5 4.5 11 4.5s11-2 11-4.5V10M9 20c0 2.5 5 4.5 11 4.5s11-2 11-4.5"/>',
    "vector": '<ellipse cx="20" cy="10" rx="11" ry="4"/><path d="M9 10v20c0 2.5 5 4.5 11 4.5s11-2 11-4.5V10"/>'
              '<circle cx="15" cy="22" r="1.6"/><circle cx="25" cy="20" r="1.6"/><circle cx="20" cy="28" r="1.6"/>',
    "cache": '<path d="M22 6L11 22h8l-2 12 12-17h-8z"/>',
    "queue": '<rect x="5" y="13" width="8" height="14" rx="1"/><rect x="16" y="13" width="8" height="14" rx="1"/>'
             '<rect x="27" y="13" width="8" height="14" rx="1"/>',
    "storage": '<ellipse cx="20" cy="12" rx="11" ry="3.5"/><path d="M9 12l3 22h16l3-22"/>',
    "lock": '<rect x="10" y="19" width="20" height="15" rx="2"/><path d="M14 19v-5a6 6 0 0 1 12 0v5"/>',
    "egress": '<path d="M8 10v20M8 20h20M22 13l7 7-7 7"/>',
    "chip": '<rect x="12" y="12" width="16" height="16" rx="2"/><path d="M17 7v5M23 7v5M17 28v5M23 28v5M7 17h5M7 23h5M28 17h5M28 23h5"/>',
    "stream": '<path d="M5 13c4-4 6 4 10 0s6 4 10 0 6 4 10 0M5 21c4-4 6 4 10 0s6 4 10 0 6 4 10 0M5 29c4-4 6 4 10 0s6 4 10 0 6 4 10 0"/>',
    "realtime": '<path d="M8 15h24M27 10l5 5-5 5M32 25H8M13 20l-5 5 5 5"/>',
    "external": '<path d="M12 31a7 7 0 0 1 0-14 9 9 0 0 1 17-2 7 7 0 0 1 0 16z"/>',
}

# category -> (glyph, column, row order, edge label from the compute tier)
CATEGORY = {
    "Static frontend hosting": ("storage", 1, 0),
    "Public entry point": ("balancer", 1, 1),
    "Realtime connections": ("realtime", 1, 2),
    "Frontend compute": ("container", 2, 0),
    "Backend compute": ("container", 2, 1),
    "Application compute": ("container", 2, 1),
    "Background worker compute": ("container", 2, 2),
    "GPU compute": ("chip", 2, 3),
    "Relational database": ("database", 3, 0),
    "Document database": ("database", 3, 1),
    "Vector store": ("vector", 3, 2),
    "Cache": ("cache", 3, 3),
    "Message broker": ("queue", 3, 4),
    "Event streaming": ("stream", 3, 5),
    "Object storage": ("storage", 3, 6),
    "Secrets management": ("lock", 4, 0),
    "Outbound internet access": ("egress", 4, 1),
}
COMPUTE = ("Frontend compute", "Backend compute", "Application compute")
DATA_LABEL = {
    "Relational database": "PostgreSQL:5432",
    "Document database": "MongoDB:27017",
    "Vector store": "SQL:5432",
    "Cache": "Redis:6379",
    "Message broker": "AMQP / HTTPS",
    "Event streaming": "Kafka:9092",
    "Object storage": "HTTPS:443",
    "Secrets management": "HTTPS:443",
}
NODE_W, NODE_H, COL_W, ROW_H = 176, 124, 300, 150
MARGIN_X, TOP = 36, 96


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def wrap(text, width=22, max_lines=3):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])[:width]]
    return lines


def load_icon(icon_dir, service, category, glyph):
    if not icon_dir:
        return None
    for key in (slug(service), slug(category), glyph):
        path = os.path.join(icon_dir, key + ".svg")
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode("ascii")
    return None


def build_nodes(placements, evidence):
    cloud = placements["cloud"]
    nodes = []
    for p in placements["placements"]:
        if p["category"] not in CATEGORY:
            continue
        glyph, col, row = CATEGORY[p["category"]]
        if p["category"] in ("Backend compute", "Application compute", "Background worker compute", "Frontend compute") \
                and re.search(r"Lambda|Azure Functions|Cloud Functions", p["service"]):
            glyph = "function"
        nodes.append({"id": p["id"], "category": p["category"], "service": p["service"], "glyph": glyph,
                      "col": col, "row": row, "vendor": cloud})
    ai = sorted({e["name"] for e in (evidence or {}).get("evidence", [])
                 if e.get("kind") == "ai" and e.get("signal") == "llm" and e["name"].endswith(" API")})
    nodes.append({"id": "USER", "category": "Users", "service": "Users (browser)", "glyph": "user",
                  "col": 0, "row": 1, "vendor": None})
    if ai:
        nodes.append({"id": "EXT", "category": "External", "service": " and ".join(ai) + " (external)",
                      "glyph": "external", "col": 5, "row": 1, "vendor": None})
    return nodes


def build_edges(nodes):
    by_cat = {}
    for n in nodes:
        by_cat.setdefault(n["category"], n)
    edges = []

    def add(a, b, label):
        if a and b and a is not b:
            edges.append((a["id"], b["id"], label))

    entry = by_cat.get("Public entry point")
    static = by_cat.get("Static frontend hosting")
    realtime = by_cat.get("Realtime connections")
    backend = by_cat.get("Backend compute") or by_cat.get("Application compute")
    frontend = by_cat.get("Frontend compute")
    user = by_cat["Users"]
    worker = by_cat.get("Background worker compute")
    first_hop = entry or static
    add(user, first_hop, "HTTPS:443")
    if not entry:
        add(user, frontend or backend, "HTTPS:443")
    if entry:
        add(entry, frontend, "HTTP")
        add(entry, backend, "HTTP:8000" if backend and backend["category"] != "Frontend compute" else "HTTP")
        add(entry, realtime, "WSS:443")
    add(realtime, backend, "HTTP")
    for cat, label in DATA_LABEL.items():
        add(backend, by_cat.get(cat), label)
    for cat in ("Relational database", "Cache", "Message broker"):
        add(worker, by_cat.get(cat), DATA_LABEL[cat])
    egress, ext = by_cat.get("Outbound internet access"), by_cat.get("External")
    if egress:
        add(backend, egress, "HTTPS:443")
        add(egress, ext, "HTTPS:443")
    else:
        add(backend, ext, "HTTPS:443")
    seen, out = set(), []
    for e in edges:
        if (e[0], e[1]) not in seen:
            seen.add((e[0], e[1]))
            out.append(e)
    return out


def render(placements, evidence=None, icon_dir=None, title=None):
    cloud = placements["cloud"]
    if cloud not in VENDOR:
        raise ValueError("unknown cloud '%s'" % cloud)
    nodes = build_nodes(placements, evidence)
    if len(nodes) <= 1:
        raise ValueError("no placements to draw")
    fill, dark = VENDOR[cloud]
    model = "serverless" if placements.get("compute_model") == "serverless" else "server-based"
    inside_max = max([n["col"] for n in nodes if 1 <= n["col"] <= 4] or [1])
    for n in nodes:
        if n["id"] == "EXT":
            n["col"] = inside_max + 1
        n["x"] = MARGIN_X + n["col"] * COL_W
    # compact rows within each column so empty slots do not leave gaps
    for col in {n["col"] for n in nodes}:
        for i, n in enumerate(sorted((n for n in nodes if n["col"] == col), key=lambda n: (n["row"], n["id"]))):
            n["y"] = TOP + 40 + i * ROW_H
    max_rows = max(sum(1 for m in nodes if m["col"] == c) for c in {n["col"] for n in nodes})
    has_ext = any(n["id"] == "EXT" for n in nodes)
    last_col = inside_max + 1 if has_ext else inside_max
    width = MARGIN_X * 2 + (last_col + 1) * COL_W - (COL_W - NODE_W)
    arcs_needed = sum(1 for e in build_edges(nodes)
                      if {n["id"]: n for n in nodes}[e[1]]["col"] - {n["id"]: n for n in nodes}[e[0]]["col"] >= 2)
    height = TOP + 40 + max_rows * ROW_H + 50 + arcs_needed * 22
    by_id = {n["id"]: n for n in nodes}
    parts = []
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
                 'font-family="Segoe UI, Helvetica, Arial, sans-serif" role="img" aria-label="%s">' % (
                     width, height, width, height,
                     html.escape("Architecture diagram: %s, %s" % (CLOUD_TITLE[cloud], model))))
    parts.append('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
                 'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#5b6b7b"/></marker></defs>')
    parts.append('<rect width="100%" height="100%" fill="#ffffff"/>')
    heading = title or "Architecture on %s (%s)" % (CLOUD_TITLE[cloud], model)
    parts.append('<text x="%d" y="40" font-size="22" font-weight="600" fill="#14202b">%s</text>' % (MARGIN_X, html.escape(heading)))
    parts.append('<text x="%d" y="64" font-size="12" fill="#566574">Boxes are the services chosen by the fixed placement rules. '
                 'Icons are generic glyphs coloured by cloud, not official vendor logos.</text>' % MARGIN_X)
    # cloud boundary around columns 1 to 4
    bx = MARGIN_X + 1 * COL_W - 18
    bw = inside_max * COL_W - (COL_W - NODE_W) + 36
    parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="10" fill="#fbfcfd" stroke="%s" stroke-width="2" '
                 'stroke-dasharray="7 5"/>' % (bx, TOP - 4, bw, height - TOP - 24, dark))
    parts.append('<text x="%d" y="%d" font-size="13" font-weight="600" fill="%s">%s</text>' % (
        bx + 14, TOP + 16, dark, html.escape("%s boundary" % CLOUD_TITLE[cloud])))
    edge_parts, label_parts = [], []
    bottom = max(n["y"] + NODE_H for n in nodes)
    arc_count = 0
    for src, dst, label in build_edges(nodes):
        a, b = by_id[src], by_id[dst]
        x1, y1 = a["x"] + NODE_W, a["y"] + 46
        x2, y2 = b["x"], b["y"] + 46
        if b["col"] - a["col"] >= 2:
            arc_count += 1
            yb = bottom + 14 + arc_count * 22
            x1, x2 = a["x"] + NODE_W / 2 + arc_count * 6, b["x"] + NODE_W / 2 - arc_count * 6
            y1, y2 = a["y"] + NODE_H, b["y"] + NODE_H
            d = "M%.0f %.0f L%.0f %.0f L%.0f %.0f L%.0f %.0f" % (x1, y1, x1, yb, x2, yb, x2, y2)
            mx, my = (x1 + x2) / 2, yb + 4
            edge_parts.append('<path d="%s" fill="none" stroke="#5b6b7b" stroke-width="1.6" stroke-linejoin="round" '
                              'marker-end="url(#arrow)"/>' % d)
            w = len(label) * 6.1 + 10
            label_parts.append('<rect x="%.0f" y="%.0f" width="%.0f" height="16" rx="3" fill="#ffffff" fill-opacity="0.92"/>'
                               '<text x="%.0f" y="%.0f" font-size="10.5" text-anchor="middle" fill="#2b3a48">%s</text>' % (
                                   mx - w / 2, my - 11, w, mx, my, html.escape(label)))
            continue
        if b["col"] <= a["col"]:
            x1, x2 = a["x"] + NODE_W / 2, b["x"] + NODE_W / 2
            y1, y2 = (a["y"] + NODE_H, b["y"]) if b["y"] > a["y"] else (a["y"], b["y"] + NODE_H)
            d = "M%.0f %.0f L%.0f %.0f" % (x1, y1, x2, y2)
        else:
            dx = max(40, (x2 - x1) / 2)
            d = "M%.0f %.0f C%.0f %.0f %.0f %.0f %.0f %.0f" % (x1, y1, x1 + dx, y1, x2 - dx, y2, x2, y2)
        edge_parts.append('<path d="%s" fill="none" stroke="#5b6b7b" stroke-width="1.6" marker-end="url(#arrow)"/>' % d)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        w = len(label) * 6.1 + 10
        label_parts.append('<rect x="%.0f" y="%.0f" width="%.0f" height="16" rx="3" fill="#ffffff" fill-opacity="0.92"/>'
                           '<text x="%.0f" y="%.0f" font-size="10.5" text-anchor="middle" fill="#2b3a48">%s</text>' % (
                               mx - w / 2, my - 11, w, mx, my, html.escape(label)))
    parts.extend(edge_parts)
    parts.extend(label_parts)
    for n in nodes:
        x, y = n["x"], n["y"]
        vendor_fill = fill if n["vendor"] else "#44525f"
        parts.append('<g><title>%s</title>' % html.escape("%s: %s" % (n["id"], n["service"])))
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="#ffffff" stroke="#cfd8e0"/>' % (x, y, NODE_W, NODE_H))
        ix, iy = x + (NODE_W - 52) / 2, y + 10
        parts.append('<rect x="%.0f" y="%.0f" width="52" height="52" rx="8" fill="%s"/>' % (ix, iy, vendor_fill))
        override = load_icon(icon_dir, n["service"], n["category"], n["glyph"])
        if override:
            parts.append('<image x="%.0f" y="%.0f" width="40" height="40" href="data:image/svg+xml;base64,%s"/>' % (
                ix + 6, iy + 6, override))
        else:
            parts.append('<g transform="translate(%.0f %.0f) scale(1)" %s>%s</g>' % (ix + 6, iy + 6, STROKE, GLYPHS[n["glyph"]]))
        if n["id"] not in ("USER", "EXT"):
            parts.append('<text x="%d" y="%d" font-size="10" fill="#566574">%s</text>' % (x + 8, y + 18, n["id"]))
        parts.append('<text x="%.0f" y="%d" font-size="10.5" text-anchor="middle" fill="#566574">%s</text>' % (
            x + NODE_W / 2, y + 78, html.escape(n["category"])))
        for i, line in enumerate(wrap(n["service"])):
            parts.append('<text x="%.0f" y="%d" font-size="12" font-weight="600" text-anchor="middle" fill="#14202b">%s</text>' % (
                x + NODE_W / 2, y + 94 + i * 14, html.escape(line)))
        parts.append("</g>")
    parts.append("</svg>")
    return brand.stamp_svg("\n".join(parts) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Draw the architecture as an SVG with service icons.")
    ap.add_argument("placements")
    ap.add_argument("--out", required=True)
    ap.add_argument("--evidence")
    ap.add_argument("--icon-dir")
    ap.add_argument("--title")
    args = ap.parse_args(argv)
    try:
        with open(args.placements, "r", encoding="utf-8") as fh:
            placements = json.load(fh)
        evidence = None
        if args.evidence:
            with open(args.evidence, "r", encoding="utf-8") as fh:
                evidence = json.load(fh)
        svg = render(placements, evidence, args.icon_dir, args.title)
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
