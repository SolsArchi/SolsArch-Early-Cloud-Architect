#!/usr/bin/env python3
"""A short executive deck and summary for people who will not read the full report.

    executive.py --repo REPO

Writes three files from the same content, so they always agree:
    docs/solsarch-executive-deck.pptx     opens in PowerPoint, Keynote and Google Slides
    docs/solsarch-executive-deck.html     one slide per printed page; print to PDF from any browser
    docs/solsarch-executive-summary.pdf   two pages of text, for email

The content comes only from files the other steps wrote (plan, design, costs, scorecard, gate, capability checklist). Every
amount carries its label (live price, estimate, unpriced). The files carry the SolsArch mark. Standard library only.
"""
import argparse
import html
import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import brand  # noqa: E402

FIXED_TIME = (2026, 1, 1, 0, 0, 0)
NAVY, BLUE, GREY, WHITE = "14202E", "1F6FEB", "566574", "FFFFFF"


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def money(value, currency="USD"):
    return "%s %s" % (currency, format(int(round(value)), ","))


def content(repo):
    state = Path(repo) / ".solsarch"
    plan, design, scan = (read(state / n) for n in ("plan.json", "design.json", "scan.json"))
    if not (plan and design and scan):
        raise FileNotFoundError("Run the scan, plan and finish steps first.")
    costs, card, manifest, gate = (read(state / n) for n in ("costs.json", "ops-scorecard.json", "iac-manifest.json", "validation.json"))
    findings = scan.get("findings", [])
    blockers = [f for f in findings if f["severity"] in ("critical", "high")]
    rules = design.get("rules", [])
    violated = [r for r in rules if r["status"] == "violated"]
    bent = [r for r in rules if r["status"] == "bent"]
    verdict = ("Not ready for production: %d blocker(s) to fix first." % len(blockers)) if blockers else (
        "Fix %d design issue(s) before production." % len(violated) if violated else "No blockers found in the scanned files.")
    slides = []
    slides.append({"title": "%s on %s" % (plan.get("project") or "Your application", plan["cloud_name"]),
                   "bullets": ["Architecture review prepared by SolsArch Early Cloud Architect", "Plan %s | %s" % (plan["fingerprint"], plan["compute_label"]),
                               "Account type: %s" % (design.get("profile", {}).get("account_type", "standard")).replace("_", " ")], "kind": "title"})
    slides.append({"title": "Verdict", "bullets": [verdict] + ["%d service(s) chosen from evidence in the code" % len(plan["decisions"]),
                                                                 "%d design rule(s) met, %d bent with a stated risk, %d violated" % (
                                                                     sum(1 for r in rules if r["status"] == "met"), len(bent), len(violated))]})
    slides.append({"title": "What the code contains", "bullets": ["%s: %s" % (u["name"], ", ".join(u.get("frameworks") or u.get("languages") or [u["kind"]])) for u in design["units"]]
                   + ["Archetype: %s" % design["archetype"]["name"].replace("_", " ")] + ["Calls out to: %s" % ", ".join(e["name"] for e in plan.get("external_services", []))] * bool(plan.get("external_services"))})
    slides.append({"title": "Recommended architecture", "table": [(d["role"], d["service"]) for d in plan["decisions"]][:14],
                   "bullets": ["Every service is tied to evidence in the code or to a design rule."]})
    risk = ["%s %s (%s)" % (f["id"], f["title"], f["severity"]) for f in findings][:6] + ["%s %s" % (r["id"], r["name"]) for r in violated][:4]
    slides.append({"title": "Risks to fix before launch", "bullets": risk or ["None found by the pre-deployment checks."] + ["Bent rules accepted with a stated risk: %s" % ", ".join(r["id"] for r in bent)] * bool(bent)})
    if costs and costs.get("totals"):
        t = costs["totals"]
        label = "all lines priced" if not t["unpriced_lines"] else "%d line(s) could not be priced, so the figure may be low" % t["unpriced_lines"]
        rng = costs.get("range")
        bullets = ["Estimated monthly cost: %s (%s)" % (money(t["monthly"], costs.get("currency", "USD")), label), "Priced from live vendor prices: %d%% of lines" % round(100 * t.get("live_share", 0))]
        if rng:
            bullets.append("Likely range: %s to %s (10th to 90th percentile)" % (money(rng["p10"]), money(rng["p90"])))
        bullets.append("Estimates depend on traffic assumptions listed in docs/solsarch-costs.md")
        slides.append({"title": "Cost", "bullets": bullets})
    if card:
        rows = [(d["discipline"], "n/a" if not d["applicable"] else "Level %d, %s" % (d["level"], d["label"])) for d in card["disciplines"]]
        slides.append({"title": "Operations maturity: level %d, %s" % (card["overall_level"], card["overall_label"]), "table": rows,
                       "bullets": ["Counted from files in the repository, not guessed."]})
    if manifest:
        p = manifest["parity"]
        slides.append({"title": "Infrastructure code delivered", "bullets": [f["label"] + " (%d files)" % f["files"] for f in manifest["families"]] +
                       ["Capability checklist: %s%% of %d applicable checks" % (p["score"], p["applicable"])] + (["Gaps to close: %s" % ", ".join(p["gaps"])] if p["gaps"] else [])})
    slides.append({"title": "Next steps", "bullets": ["Fix the risks above, then rebuild the report", "Follow docs/solsarch-deploy-guide.md step by step (terminal or console)",
                                                       "Run the generated pipeline: checks on every pull request, approval before deploy", "Set a budget alert before the first deploy",
                                                       "Re-run SolsArch after major changes; the same code gives the same result"]})
    return {"project": plan.get("project") or "application", "fingerprint": plan["fingerprint"], "verdict": verdict, "slides": slides}


# --------------------------------------------------------------------------- HTML
def html_deck(c):
    parts = []
    for s in c["slides"]:
        body = ""
        if s.get("table"):
            body += "<table>" + "".join("<tr><td>%s</td><td>%s</td></tr>" % (html.escape(a), html.escape(b)) for a, b in s["table"]) + "</table>"
        body += "<ul>" + "".join("<li>%s</li>" % html.escape(b) for b in s.get("bullets", [])) + "</ul>"
        parts.append('<section class="slide%s"><h2>%s</h2>%s%s</section>' % (" title" if s.get("kind") == "title" else "", html.escape(s["title"]), body, brand.html_badge(36)))
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>%s</title><style>"
            "@page{size:landscape;margin:0}body{margin:0;font-family:system-ui,sans-serif;background:#e9edf2;color:#14202e}"
            ".slide{position:relative;box-sizing:border-box;width:min(1100px,100%%);min-height:620px;margin:16px auto;background:#fff;padding:48px 56px;border-top:10px solid #1f6feb;page-break-after:always;break-after:page}"
            ".slide.title{background:#14202e;color:#fff}.slide h2{margin:0 0 24px;font-size:2rem}li{margin:10px 0;font-size:1.25rem}"
            "table{border-collapse:collapse;width:100%%;margin-bottom:16px}td{border-bottom:1px solid #cfd8e0;padding:8px 6px}"
            ".solsarch-mark{position:absolute;right:20px;bottom:16px}@media print{body{background:#fff}.slide{margin:0;width:100%%}}</style></head><body>%s</body></html>\n" % (
                html.escape(c["project"]), "".join(parts)))


# --------------------------------------------------------------------------- PDF
def pdf_escape(text):
    text = text.encode("cp1252", "replace").decode("cp1252")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def wrap(text, width):
    lines, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            lines.append(line)
            line = word
        else:
            line = (line + " " + word).strip()
    return lines + ([line] if line else [])


def pdf(c):
    pages, cur, y = [], [], 760

    def newpage():
        nonlocal cur, y
        if cur:
            pages.append(cur)
        cur, y = [], 760

    def put(text, size=11, bold=False, indent=0):
        nonlocal y
        for line in wrap(text, int(95 - indent / 5 - (size - 11) * 3)):
            if y < 70:
                newpage()
            cur.append((50 + indent, y, size, bold, line))
            y -= size + 5

    put("%s: executive summary" % c["project"], 18, True)
    put("Plan %s | Prepared with SolsArch Early Cloud Architect" % c["fingerprint"], 9)
    y -= 8
    for s in c["slides"][1:]:
        if y < 150:
            newpage()
        put(s["title"], 13, True)
        for a, b in s.get("table", []):
            put("%s: %s" % (a, b), 10, False, 12)
        for b in s.get("bullets", []):
            put("- " + b, 10, False, 12)
        y -= 6
    newpage()
    objs = []

    def add(body):
        objs.append(body)
        return len(objs)

    add("<< /Type /Catalog /Pages 2 0 R >>")
    add("PAGES")
    f1 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for n, page in enumerate(pages, 1):
        ops = ["BT"]
        for x, yy, size, bold, line in page:
            ops.append("/F%d %d Tf 1 0 0 1 %d %d Tm (%s) Tj" % (2 if bold else 1, size, x, yy, pdf_escape(line)))
        ops.append("ET")
        ops.append("BT /F1 8 Tf 1 0 0 1 50 30 Tm (%s) Tj ET" % pdf_escape("Prepared with SolsArch Early Cloud Architect | page %d of %d" % (n, len(pages))))
        stream = "\n".join(ops)
        cid = add("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream.encode("cp1252", "replace")), stream))
        kids.append(add("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> >>" % (cid, f1, f2)))
    objs[1] = "<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join("%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += ("%d 0 obj\n%s\nendobj\n" % (i, body)).encode("cp1252", "replace")
    start = len(out)
    out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode("ascii")
    for off in offsets:
        out += ("%010d 00000 n \n" % off).encode("ascii")
    out += ("trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, start)).encode("ascii")
    return bytes(out)


# --------------------------------------------------------------------------- PPTX
EMU = 914400
W, H = 12192000, 6858000


def sp(idn, name, x, y, w, h, paras, fill=None, anchor="t"):
    fill_xml = '<a:solidFill><a:srgbClr val="%s"/></a:solidFill>' % fill if fill else "<a:noFill/>"
    ps = ""
    for text, size, bold, color in paras:
        ps += ('<a:p><a:r><a:rPr lang="en-US" sz="%d" b="%d" dirty="0"><a:solidFill><a:srgbClr val="%s"/></a:solidFill><a:latin typeface="Calibri"/></a:rPr><a:t>%s</a:t></a:r></a:p>'
               % (size * 100, 1 if bold else 0, color, escape(text)))
    if not ps:
        ps = '<a:p><a:endParaRPr lang="en-US"/></a:p>'
    return ('<p:sp><p:nvSpPr><p:cNvPr id="%d" name="%s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>%s<a:ln><a:noFill/></a:ln></p:spPr><p:txBody><a:bodyPr wrap="square" lIns="91440" tIns="45720" rIns="91440" bIns="45720" anchor="%s"><a:normAutofit/></a:bodyPr><a:lstStyle/>%s</p:txBody></p:sp>'
            % (idn, escape(name), x, y, w, h, fill_xml, anchor, ps))


def slide_xml(s, number):
    shapes = []
    title_slide = s.get("kind") == "title"
    bg = NAVY if title_slide else WHITE
    shapes.append(sp(2, "Top bar", 0, 0, W, 120000, [], fill=BLUE))
    if title_slide:
        shapes.append(sp(3, "Title", 700000, 2000000, W - 1400000, 1200000, [(s["title"], 40, True, WHITE)], anchor="b"))
        shapes.append(sp(4, "Subtitle", 700000, 3400000, W - 1400000, 1800000, [(b, 18, False, "C9D6E3") for b in s["bullets"]]))
    else:
        shapes.append(sp(3, "Title", 600000, 300000, W - 1200000, 800000, [(s["title"], 30, True, NAVY)], anchor="ctr"))
        y = 1250000
        if s.get("table"):
            rows = s["table"][:14]
            shapes.append(sp(4, "Table", 600000, y, W - 1200000, min(3400000, 280000 * len(rows) + 100000),
                             [("%s  |  %s" % (a, b), 14, False, NAVY) for a, b in rows]))
            y += min(3400000, 280000 * len(rows) + 100000) + 100000
        if s.get("bullets"):
            shapes.append(sp(5, "Body", 600000, y, W - 1200000, H - y - 600000, [("• " + b, 16, False, NAVY if not s.get("table") else GREY) for b in s["bullets"]]))
    shapes.append(sp(9, "Mark", 600000, H - 420000, W - 1200000, 320000,
                     [("Prepared with SolsArch Early Cloud Architect  |  slide %d" % number, 10, False, "C9D6E3" if title_slide else GREY)]))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            '<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="%s"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>%s</p:spTree></p:cSld>'
            '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>' % (bg, "".join(shapes)))


THEME = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="SolsArch"><a:themeElements>'
         '<a:clrScheme name="SolsArch"><a:dk1><a:srgbClr val="14202E"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F6FEB"/></a:dk2><a:lt2><a:srgbClr val="E9EDF2"/></a:lt2>'
         '<a:accent1><a:srgbClr val="1F6FEB"/></a:accent1><a:accent2><a:srgbClr val="2DA44E"/></a:accent2><a:accent3><a:srgbClr val="D29922"/></a:accent3><a:accent4><a:srgbClr val="CF222E"/></a:accent4>'
         '<a:accent5><a:srgbClr val="8250DF"/></a:accent5><a:accent6><a:srgbClr val="566574"/></a:accent6><a:hlink><a:srgbClr val="1F6FEB"/></a:hlink><a:folHlink><a:srgbClr val="8250DF"/></a:folHlink></a:clrScheme>'
         '<a:fontScheme name="SolsArch"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont></a:fontScheme>'
         '<a:fmtScheme name="SolsArch"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst>'
         '<a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="19050"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln><a:ln w="28575"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst>'
         '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
         '<a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>')
NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
EMPTY_TREE = ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
              '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>')


def pptx(c):
    n = len(c["slides"])
    files = {
        "[Content_Types].xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                                '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                                '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
                                '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
                                '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
                                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                                '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
                                + "".join('<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' % i for i in range(1, n + 1))
                                + "</Types>"),
        "_rels/.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>'
                        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>'),
        "docProps/core.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                              'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                              '<dc:title>%s executive deck</dc:title><dc:creator>SolsArch Early Cloud Architect</dc:creator></cp:coreProperties>' % escape(c["project"])),
        "docProps/app.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
                             '<Application>SolsArch Early Cloud Architect</Application><Slides>%d</Slides></Properties>' % n),
        "ppt/presentation.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:presentation %s><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
                                 '<p:sldIdLst>%s</p:sldIdLst><p:sldSz cx="%d" cy="%d"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>' % (
                                     NS, "".join('<p:sldId id="%d" r:id="rId%d"/>' % (255 + i, i + 1) for i in range(1, n + 1)), W, H)),
        "ppt/_rels/presentation.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
                                            + "".join('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide%d.xml"/>' % (i + 1, i) for i in range(1, n + 1))
                                            + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/></Relationships>' % (n + 2)),
        "ppt/theme/theme1.xml": THEME,
        "ppt/slideMasters/slideMaster1.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sldMaster %s>%s<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
                                              'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
                                              '<p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>' % (NS, EMPTY_TREE)),
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                                                         '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>'),
        "ppt/slideLayouts/slideLayout1.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<p:sldLayout %s type="blank" preserve="1">%s<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>' % (NS, EMPTY_TREE)),
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>'),
    }
    for i, s in enumerate(c["slides"], 1):
        files["ppt/slides/slide%d.xml" % i] = slide_xml(s, i)
        files["ppt/slides/_rels/slide%d.xml.rels" % i] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                                          '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>')
    return files


def write_pptx(files, target):
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        order = ["[Content_Types].xml"] + sorted(k for k in files if k != "[Content_Types].xml")
        for name in order:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name].encode("utf-8"))


def build(repo):
    c = content(repo)
    docs = Path(repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    write_pptx(pptx(c), docs / "solsarch-executive-deck.pptx")
    (docs / "solsarch-executive-deck.html").write_bytes(html_deck(c).encode("utf-8"))
    (docs / "solsarch-executive-summary.pdf").write_bytes(pdf(c))
    return {"status": "written", "slides": len(c["slides"]), "files": ["solsarch-executive-deck.pptx", "solsarch-executive-deck.html", "solsarch-executive-summary.pdf"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Executive deck and summary")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    try:
        out = build(args.repo)
    except FileNotFoundError as error:
        sys.stdout.write(json.dumps({"status": "error", "message": str(error)}) + "\n")
        return 2
    sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
