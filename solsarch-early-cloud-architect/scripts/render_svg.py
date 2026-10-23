#!/usr/bin/env python3
"""Draw the plan as standalone SVG pictures with a service icon on every box.

Two files are produced: an infrastructure picture (zones, boxes, arrows) and a service
sheet (one card per service). Each icon is a simple glyph for the kind of service, on a
tile in the chosen cloud's brand colour. These are generic category icons, not the cloud
vendors' official icon packs. SVG opens in any browser, document or slide tool and needs no
network access. Output is identical for identical plans.

Standard library only. Library use: render_svg.infrastructure(plan) and render_svg.service_sheet(plan).
"""

import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brand as solsarch_brand  # noqa: E402
from pathlib import Path
from xmltext import escape

PALETTE = Path(__file__).resolve().parent.parent / "assets" / "cloud-vendor-palette.json"
INK, MUTED, LINE, PAPER, CARD = "#14202e", "#5a6878", "#c9d3de", "#ffffff", "#f4f7fa"
NEUTRAL = {"fill": "#e8edf2", "stroke": "#5f6c7b", "text": "#14202e"}
TILE_W, TILE_H, H_GAP, MARGIN = 236, 84, 28, 32
FONT = "font-family=\"Segoe UI, Helvetica, Arial, sans-serif\""

# 24 x 24 line glyphs, drawn with the tile's text colour.
GLYPHS = {
    "user": '<circle cx="12" cy="8" r="3.4"/><path d="M5 20c0-4 3-6.5 7-6.5s7 2.5 7 6.5"/>',
    "compute": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
    "function": '<path d="M8 4h3l6 16h-3z"/><path d="M5 20l5-9"/>',
    "database": '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6"/>'
                '<path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    "cache": '<path d="M13 3L5 13h6l-1 8 8-10h-6z"/>',
    "queue": '<path d="M4 6h10M4 12h14M4 18h10M16 9l3 3-3 3"/>',
    "gateway": '<path d="M12 3v6M12 9l-7 6M12 9l7 6M12 9v12"/><circle cx="5" cy="17" r="2"/><circle cx="12" cy="20" r="1.6"/>'
               '<circle cx="19" cy="17" r="2"/>',
    "edge": '<circle cx="12" cy="12" r="8"/><path d="M4 12h16M12 4c3 3 3 13 0 16M12 4c-3 3-3 13 0 16"/>',
    "network": '<rect x="3" y="5" width="18" height="14" rx="2" stroke-dasharray="3 2"/><circle cx="9" cy="12" r="1.7"/>'
               '<circle cx="15" cy="12" r="1.7"/><path d="M10.7 12h2.6"/>',
    "egress": '<path d="M4 8v10h12M10 14L20 4M14 4h6v6"/>',
    "secrets": '<circle cx="8" cy="12" r="3.6"/><path d="M11.6 12H21M17 12v3M20 12v2"/>',
    "registry": '<rect x="3" y="12" width="8" height="8"/><rect x="13" y="12" width="8" height="8"/><rect x="8" y="3" width="8" height="8"/>',
    "observability": '<path d="M4 20V4M4 20h16M8 16l4-5 3 3 5-7"/>',
    "storage": '<ellipse cx="12" cy="6" rx="7" ry="2.2"/><path d="M5 6l2 14h10l2-14"/>',
    "static": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 9h18"/>',
    "identity": '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="9" cy="12" r="2"/><path d="M14 10h4M14 14h4"/>',
    "search": '<circle cx="10" cy="10" r="6"/><path d="M15 15l5 5"/>',
    "vector": '<circle cx="6" cy="7" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="12" cy="18" r="2"/>'
              '<path d="M8 7h8M7.2 8.8l3.6 7.4M16.8 8.8l-3.6 7.4"/>',
    "gpu": '<rect x="6" y="6" width="12" height="12" rx="1"/><rect x="9.5" y="9.5" width="5" height="5"/>'
           '<path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3"/>',
    "external": '<path d="M7 18a4 4 0 0 1-.5-8 5.5 5.5 0 0 1 10.5 1.5A3.5 3.5 0 0 1 17 18z"/>',
}
KEY_ICON = {
    "compute_web": "compute", "compute_api": "compute", "compute_app": "compute", "compute_worker": "compute",
    "database": "database", "database_mysql": "database", "database_mongo": "database", "vector": "vector",
    "cache": "cache", "queue_kafka": "queue", "queue_rabbitmq": "queue", "search": "search", "storage": "storage",
    "static_site": "static", "network": "network", "egress": "egress", "ingress": "gateway", "edge": "edge",
    "identity": "identity", "secrets": "secrets", "registry": "registry", "observability": "observability", "gpu": "gpu",
}
DATA_ORDER = ("database", "database_mysql", "database_mongo", "vector", "cache", "queue_kafka", "queue_rabbitmq", "search", "storage")
APP_ORDER = ("compute_web", "compute_api", "compute_app", "compute_worker", "gpu")
SHEET_ORDER = ("edge", "ingress", "static_site") + APP_ORDER + DATA_ORDER + ("network", "egress", "identity", "secrets", "registry", "observability")


def palette(cloud):
    try:
        import providers
        return providers.palette()[cloud]
    except (ImportError, KeyError, ValueError):
        pass
    try:
        data = json.loads(PALETTE.read_text(encoding="utf-8"))["providers"]
        return data[cloud]
    except (OSError, ValueError, KeyError):
        return {"fill": "#4285F4", "stroke": "#174EA6", "text": "#FFFFFF"}


def wrap(text, width, lines):
    parts = textwrap.wrap(text, width) or [""]
    if len(parts) > lines:
        parts = parts[:lines]
        parts[-1] = parts[-1][:max(0, width - 1)].rstrip() + "..."
    return parts


def icon_tile(x, y, size, glyph, colors):
    scale = size / 24.0
    inner = GLYPHS[glyph]
    return ('<g transform="translate(%s %s)"><rect width="%s" height="%s" rx="%s" fill="%s" stroke="%s"/>'
            '<g transform="scale(%s)" fill="none" stroke="%s" stroke-width="1.6" stroke-linecap="round" '
            'stroke-linejoin="round">%s</g></g>' % (x, y, size, size, size // 6, colors["fill"], colors["stroke"],
                                                    round(scale, 3), colors["text"], inner))


def text(x, y, content, size=13, weight="400", fill=INK, anchor="start"):
    return '<text x="%s" y="%s" font-size="%s" font-weight="%s" fill="%s" text-anchor="%s" %s>%s</text>' % (
        x, y, size, weight, fill, anchor, FONT, escape(content))


def tile(x, y, glyph, colors, title, role):
    parts = ['<rect x="%s" y="%s" width="%s" height="%s" rx="8" fill="%s" stroke="%s"/>' % (x, y, TILE_W, TILE_H, PAPER, LINE),
             icon_tile(x + 14, y + 22, 40, glyph, colors)]
    title_lines = wrap(title, 21, 2)
    for i, line in enumerate(title_lines):
        parts.append(text(x + 66, y + 28 + i * 16, line, 14, "600"))
    start = 28 + len(title_lines) * 16 + 2
    for i, line in enumerate(wrap(role, 28, 3 - len(title_lines))):
        parts.append(text(x + 66, y + start + i * 14, line, 11, "400", MUTED))
    return "".join(parts)


def zone(x, y, w, h, label, dashed=False, fill=CARD):
    return ('<rect x="%s" y="%s" width="%s" height="%s" rx="10" fill="%s" stroke="%s" %s/>%s' % (
        x, y, w, h, fill, LINE, 'stroke-dasharray="6 4"' if dashed else "", text(x + 14, y + 20, label, 12, "600", MUTED)))


def arrow(x1, y1, x2, y2, dashed=False, label=None):
    style = 'stroke-dasharray="5 4"' if dashed else ""
    out = '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.6" %s marker-end="url(#arrow)"/>' % (
        x1, y1, x2, y2, MUTED, style)
    if label:
        out += text((x1 + x2) // 2, (y1 + y2) // 2 - 10, label, 10, "400", MUTED, "middle")
    return out


def svg_document(width, height, body, title):
    return solsarch_brand.stamp_svg('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" role="img" '
            'aria-label="%s"><title>%s</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="%s"/></marker></defs>'
            '<rect width="%d" height="%d" fill="%s"/>%s</svg>\n' % (
                width, height, width, height, escape(title), escape(title), MUTED, width, height, PAPER, body))


def icon_for(decision, mode):
    if decision["key"].startswith("compute_") and mode == "serverless" and decision["cloud"] in ("aws", "azure"):
        return "function"
    return KEY_ICON.get(decision["key"], "compute")


def infrastructure(plan):
    cloud, mode = plan["cloud"], plan["compute_model"]
    brand = palette(cloud)
    by = {d["key"]: d for d in plan["decisions"]}
    rows = {
        "public": [k for k in ("edge", "ingress", "static_site") if k in by],
        "app": [k for k in APP_ORDER if k in by],
        "data": [k for k in DATA_ORDER if k in by],
        "platform": [k for k in ("secrets", "observability", "registry", "identity") if k in by],
    }
    if not (rows["app"] or rows["data"] or "static_site" in by):
        return None
    externals = plan.get("external_services", [])
    side = ([by["egress"]] if "egress" in by else []) + externals
    columns = max([1, 3] + [len(rows[r]) for r in rows])
    main_w = columns * TILE_W + (columns - 1) * H_GAP
    right_x = MARGIN + main_w + 70
    width = right_x + TILE_W + MARGIN if side else MARGIN * 2 + main_w
    body, pos = [], {}

    def place(keys, y):
        total = len(keys) * TILE_W + (len(keys) - 1) * H_GAP
        x0 = MARGIN + (main_w - total) // 2
        for i, key in enumerate(keys):
            pos[key] = (x0 + i * (TILE_W + H_GAP), y)

    y = MARGIN
    pos["user"] = (MARGIN + (main_w - TILE_W) // 2, y)
    y += TILE_H + 44
    zones = []
    if rows["public"]:
        zones.append(("Public zone", y - 6, y + 36 + TILE_H + 10, rows["public"], y + 30))
        place(rows["public"], y + 30)
        y += 36 + TILE_H + 10 + 44
    vpc_top = y - 10
    for label, name in (("Application", "app"), ("Data", "data")):
        if rows[name]:
            zones.append((label, y + 14, y + 14 + 36 + TILE_H + 10, rows[name], y + 14 + 34))
            place(rows[name], y + 14 + 34)
            y += 14 + 36 + TILE_H + 10 + 6
    vpc_bottom = y + 6
    network = by.get("network")
    if rows["app"] or rows["data"]:
        body.append(zone(MARGIN - 12, vpc_top, main_w + 24, vpc_bottom - vpc_top,
                         ((network["short"] if "network" in network["short"].lower() else network["short"] + " private network")
                          if network else "Private zone"), dashed=True, fill=PAPER))
    for label, top, bottom, keys, ty in zones:
        body.append(zone(MARGIN, top, main_w, bottom - top, label))
    y = vpc_bottom + 36
    if rows["platform"]:
        body.append(zone(MARGIN, y - 6, main_w, 36 + TILE_H + 10, "Platform services"))
        place(rows["platform"], y + 30)
        y += 36 + TILE_H + 10
    height = y + MARGIN
    if side:
        app_y = pos[rows["app"][0]][1] if rows["app"] else MARGIN + 150
        sy = app_y
        for item in side:
            key = item["key"]
            pos["side_" + key] = (right_x, sy)
            sy += TILE_H + 70
        last_bottom = sy - 70 + TILE_H
        height = max(height, last_bottom + MARGIN)
        body.append(zone(right_x - 12, app_y - 36, TILE_W + 24, last_bottom + 16 - (app_y - 36), "Outbound and outside services"))

    # tiles
    body.append(tile(pos["user"][0], pos["user"][1], "user", NEUTRAL, "Users", "People using the app"))
    for key in rows["public"] + rows["app"] + rows["data"] + rows["platform"]:
        d = by[key]
        body.append(tile(pos[key][0], pos[key][1], icon_for(d, mode), brand, d["short"], d["role"]))
    for item in side:
        key = item["key"]
        if key in by:
            d = by[key]
            body.append(tile(right_x, pos["side_" + key][1], icon_for(d, mode), brand, d["short"], d["role"]))
        else:
            body.append(tile(right_x, pos["side_" + key][1], "external", NEUTRAL, item["name"], "External service"))

    def bottom(key):
        x, yy = pos[key]
        return x + TILE_W // 2, yy + TILE_H

    def top(key):
        x, yy = pos[key]
        return x + TILE_W // 2, yy

    def link(a, b, **kw):
        if a in pos and b in pos:
            body.append(arrow(*bottom(a), *top(b), **kw))

    entry = "edge" if "edge" in by else ("ingress" if "ingress" in by else ("static_site" if "static_site" in by else None))
    if entry:
        body.append(arrow(*bottom("user"), *top(entry)))
    if "edge" in by and "ingress" in by:
        pos_e, pos_i = pos["edge"], pos["ingress"]
        body.append(arrow(pos_e[0] + TILE_W, pos_e[1] + TILE_H // 2, pos_i[0], pos_i[1] + TILE_H // 2))
    front = next((k for k in ("compute_web", "compute_api", "compute_app") if k in by), None)
    if "ingress" in by and front:
        link("ingress", front)
    if "compute_web" in by and "compute_api" in by:
        a, b = pos["compute_web"], pos["compute_api"]
        body.append(arrow(a[0] + TILE_W, a[1] + TILE_H // 2, b[0], b[1] + TILE_H // 2, label="calls"))
    backend = next((k for k in ("compute_api", "compute_app", "compute_web") if k in by), None)
    for key in rows["data"]:
        if backend and not (key == "vector" and by["vector"]["short"] == "pgvector"):
            link(backend, key)
    if "compute_worker" in by:
        for key in ("cache", "database"):
            if key in by:
                link("compute_worker", key)
    if "vector" in by and by["vector"]["short"] == "pgvector" and "database" in by:
        a, b = pos["database"], pos["vector"]
        body.append(arrow(b[0], b[1] + TILE_H // 2, a[0] + TILE_W, a[1] + TILE_H // 2, dashed=True, label="runs inside"))
    if "egress" in by and backend:
        bx, by_ = pos[backend]
        ex, ey = pos["side_egress"]
        body.append(arrow(bx + TILE_W, by_ + TILE_H // 2, ex, ey + TILE_H // 2, dashed=True))
        for item in externals:
            tx, ty = pos["side_" + item["key"]]
            body.append(arrow(ex + TILE_W // 2, ey + TILE_H, tx + TILE_W // 2, ty, dashed=True, label="HTTPS"))
    legend_y = height - 10
    body.append(text(MARGIN, legend_y, "Plan %s. Icons show the kind of service, in %s brand colours. Dashed arrows are optional or outbound links."
                     % (plan["fingerprint"], plan["cloud_name"]), 10, "400", MUTED))
    return svg_document(width, height + 14, "".join(body), "Cloud infrastructure for %s on %s" % (
        plan.get("project") or "the project", plan["cloud_name"]))


def service_sheet(plan):
    cloud, mode = plan["cloud"], plan["compute_model"]
    brand = palette(cloud)
    by = {d["key"]: d for d in plan["decisions"]}
    if not any(k in by for k in APP_ORDER + DATA_ORDER + ("static_site",)):
        return None  # nothing application-specific to show
    keys = [k for k in SHEET_ORDER if k in by]
    cards = [(by[k], icon_for(by[k], mode)) for k in keys]
    cols, card_w, card_h, gap = 3, 360, 112, 18
    rows_count = (len(cards) + cols - 1) // cols
    width = MARGIN * 2 + cols * card_w + (cols - 1) * gap
    top = MARGIN + 56
    height = top + rows_count * (card_h + gap) + 30 + (len(plan.get("external_services", [])) and 70 or 0)
    body = [text(MARGIN, MARGIN + 14, "Services chosen on %s" % plan["cloud_name"], 20, "700"),
            text(MARGIN, MARGIN + 36, plan["compute_label"] + ". Plan " + plan["fingerprint"], 12, "400", MUTED)]
    for i, (d, glyph) in enumerate(cards):
        x = MARGIN + (i % cols) * (card_w + gap)
        y = top + (i // cols) * (card_h + gap)
        body.append('<rect x="%d" y="%d" width="%d" height="%d" rx="10" fill="%s" stroke="%s"/>' % (x, y, card_w, card_h, PAPER, LINE))
        body.append(icon_tile(x + 16, y + 18, 48, glyph, brand))
        body.append(text(x + 80, y + 32, wrap(d["short"], 28, 1)[0], 15, "700"))
        for j, line in enumerate(wrap(d["service"], 40, 2)):
            body.append(text(x + 80, y + 50 + j * 14, line, 11, "400", MUTED))
        body.append(text(x + 80, y + 86, wrap(d["role"], 44, 1)[0], 11, "600", INK))
        chip = "found in your code" if d["confidence"] == "confirmed" else "design rule"
        body.append('<rect x="%d" y="%d" width="%d" height="18" rx="9" fill="%s"/>' % (x + card_w - 134, y + 90, 118, CARD))
        body.append(text(x + card_w - 75, y + 103, chip, 10, "400", MUTED, "middle"))
    ext = plan.get("external_services", [])
    if ext:
        y = top + rows_count * (card_h + gap) + 6
        body.append(text(MARGIN, y + 14, "Outside your cloud account (unchanged): " + ", ".join(x["name"] for x in ext), 12, "600", MUTED))
    return svg_document(width, height, "".join(body), "Services chosen on %s" % plan["cloud_name"])
