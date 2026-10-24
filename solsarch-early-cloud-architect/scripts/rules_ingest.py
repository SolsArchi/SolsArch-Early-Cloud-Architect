#!/usr/bin/env python3
"""Read the company-rules documents that sit beside a codebase: json, yaml, md, txt, html, docx and pdf.

    rules_ingest.py PATH [PATH ...]      print what was read from each file (text length, kind, problems)

The files are untrusted input. Every reader has a size cap, nothing is fetched from the network, no XML or PDF parser is used
(docx and pdf text is pulled out with zipfile, zlib and regular expressions), and a document that cannot be read is reported with
the reason instead of being skipped. Standard library only.
"""
import hashlib
import html
import json
import re
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 2 * 1024 * 1024
MAX_FILES = 200
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_UNPACKED = 8 * 1024 * 1024
KINDS = {".json": "json", ".yaml": "yaml", ".yml": "yaml", ".md": "text", ".markdown": "text", ".txt": "text", ".rst": "text",
         ".html": "html", ".htm": "html", ".docx": "docx", ".pdf": "pdf"}
DEFAULT_LOCATIONS = (".solsarch/rules", "docs/policies", "policies", "docs/standards", "standards")
DEFAULT_FILES = ("solsarch.rules.json", "solsarch.rules.yaml", "solsarch.rules.yml", "solsarch.rules.md")


class IngestError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- html
class _Text(HTMLParser):
    SKIP = {"script", "style", "head"}
    BREAK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BREAK:
            self.parts.append(chr(10))
        if tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BREAK:
            self.parts.append(chr(10))

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_text(raw):
    parser = _Text()
    parser.feed(raw)
    return "".join(parser.parts)


# ---------------------------------------------------------------- docx
def docx_text(data):
    import io
    import zipfile
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise IngestError("not a valid .docx file")
    info = None
    for item in archive.infolist():
        if item.filename == "word/document.xml":
            info = item
    if info is None:
        raise IngestError("no word/document.xml inside the .docx file")
    if info.file_size > MAX_UNPACKED:
        raise IngestError("the document text is larger than %d MB once unpacked" % (MAX_UNPACKED // (1024 * 1024)))
    with archive.open(info) as handle:
        xml = handle.read(MAX_UNPACKED + 1)
    if len(xml) > MAX_UNPACKED:
        raise IngestError("the document text is larger than %d MB once unpacked" % (MAX_UNPACKED // (1024 * 1024)))
    text = xml.decode("utf-8", errors="replace")
    text = re.sub(r"<w:tab\s*/>", " ", text)
    text = re.sub(r"<w:(?:br|cr)\b[^>]*/>", chr(10), text)
    text = re.sub(r"</w:tc>", " | ", text)
    text = re.sub(r"</w:p>", chr(10), text)
    text = re.sub(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", lambda m: m.group(1) + chr(1), text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace(chr(1), "")
    return html.unescape(text)


# ---------------------------------------------------------------- pdf
_PDF_ESCAPES = {"n": chr(10), "r": chr(10), "t": " ", "b": "", "f": "", "(": "(", ")": ")", chr(92): chr(92)}


def _pdf_string(body):
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == chr(92) and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567":
                j = i + 1
                while j < len(body) and j < i + 4 and body[j] in "01234567":
                    j += 1
                out.append(chr(int(body[i + 1:j], 8) & 255))
                i = j
                continue
            out.append(_PDF_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _pdf_literals(content):
    """Yield the text strings in a content stream, with a newline wherever the text moves down a line."""
    out = []
    pos = 0
    length = len(content)
    while pos < length:
        ch = content[pos]
        if ch == "(":
            depth, j = 1, pos + 1
            while j < length and depth:
                if content[j] == chr(92):
                    j += 2
                    continue
                if content[j] == "(":
                    depth += 1
                elif content[j] == ")":
                    depth -= 1
                j += 1
            out.append(_pdf_string(content[pos + 1:j - 1]))
            pos = j
            continue
        if content.startswith("ET", pos) or content.startswith("T*", pos) or content.startswith("Td", pos) or content.startswith("TD", pos):
            out.append(chr(10))
            pos += 2
            continue
        if ch == "]" or content.startswith("'", pos):
            pos += 1
            continue
        pos += 1
    text = "".join(out)
    text = re.sub(r"[ \t]*" + chr(10) + r"[ \t" + chr(10) + r"]*", chr(10), text)
    return text


def pdf_text(data):
    if not data.startswith(b"%PDF"):
        raise IngestError("not a PDF file")
    if b"/Encrypt" in data[:4096] or b"/Encrypt" in data[-4096:]:
        raise IngestError("the PDF is encrypted; supply the rules as text or JSON")
    pages = []
    budget = MAX_UNPACKED
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = match.group(1)
        try:
            decoder = zlib.decompressobj()
            body = decoder.decompress(raw, budget)
            if decoder.unconsumed_tail:
                raise IngestError("a PDF stream is larger than %d MB once unpacked" % (MAX_UNPACKED // (1024 * 1024)))
        except zlib.error:
            body = raw
        budget -= len(body)
        if budget <= 0:
            raise IngestError("the PDF is larger than %d MB once unpacked" % (MAX_UNPACKED // (1024 * 1024)))
        content = body.decode("latin-1", errors="replace")
        if "Tj" not in content and "TJ" not in content:
            continue
        text = _pdf_literals(content).strip()
        if text:
            pages.append(text)
    if not pages:
        raise IngestError("no text layer found in the PDF (scanned or font-encoded); supply the rules as text or JSON")
    return (chr(10) + chr(10)).join(pages)


# ---------------------------------------------------------------- yaml (the subset a rules file needs)
def _scalar(text):
    t = text.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if t[0] in "\"'" and len(t) >= 2 and t[-1] == t[0]:
        inner = t[1:-1]
        if t[0] == "'":
            return inner.replace("''", "'")
        return re.sub(chr(92) * 2 + "(.)", lambda m: chr(10) if m.group(1) == "n" else m.group(1), inner)
    if t in ("true", "True", "TRUE", "yes", "Yes"):
        return True
    if t in ("false", "False", "FALSE", "no", "No"):
        return False
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(x) for x in _split_flow(t[1:-1])]
    if t.startswith("{") and t.endswith("}"):
        result = {}
        for item in _split_flow(t[1:-1]):
            key, sep, value = item.partition(":")
            if sep:
                result[str(_scalar(key))] = _scalar(value)
        return result
    return t


def _split_flow(text):
    parts, depth, quote, current = [], 0, "", []
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if "".join(current).strip():
        parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _strip_comment(line):
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def parse_yaml(text):
    """Maps, lists, nested lists of maps, inline [a, b] and {a: b}, quoted strings, comments and | or > blocks. Nothing else."""
    rows = []
    raw_lines = text.replace(chr(13), "").split(chr(10))
    idx = 0
    while idx < len(raw_lines):
        raw = raw_lines[idx]
        idx += 1
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise IngestError("tabs are not allowed for indentation (line %d)" % idx)
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        block = re.match(r"^(.*?:)\s*([|>])[+-]?$", body)
        if block:
            lines = []
            while idx < len(raw_lines):
                nxt = raw_lines[idx]
                if nxt.strip() and len(nxt) - len(nxt.lstrip(" ")) <= indent:
                    break
                lines.append(nxt.strip())
                idx += 1
            joiner = chr(10) if block.group(2) == "|" else " "
            body = block.group(1) + " " + _quote(joiner.join(lines).strip())
        rows.append((indent, body, idx))
    value, pos = _block(rows, 0, rows[0][0] if rows else 0)
    if pos != len(rows):
        raise IngestError("unexpected indentation near line %d" % rows[pos][2])
    return value


def _quote(text):
    return '"' + text.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"').replace(chr(10), chr(92) + "n") + '"'


def _block(rows, pos, indent):
    if pos >= len(rows):
        return None, pos
    if rows[pos][1].startswith("- ") or rows[pos][1] == "-":
        return _list(rows, pos, indent)
    return _map(rows, pos, indent)


def _map(rows, pos, indent):
    result = {}
    while pos < len(rows):
        ind, body, number = rows[pos]
        if ind < indent:
            break
        if ind > indent:
            raise IngestError("unexpected indentation at line %d" % number)
        if body.startswith("- "):
            break
        key, sep, rest = _split_key(body)
        if not sep:
            raise IngestError("expected 'key: value' at line %d" % number)
        pos += 1
        if rest.strip() == "":
            if pos < len(rows) and (rows[pos][0] > indent or (rows[pos][0] == indent and rows[pos][1].startswith("- "))):
                child_indent = rows[pos][0]
                result[key], pos = _block(rows, pos, child_indent)
            else:
                result[key] = None
        else:
            result[key] = _scalar(rest)
    return result, pos


def _split_key(body):
    quote = ""
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == ":" and (i + 1 == len(body) or body[i + 1] == " "):
            return str(_scalar(body[:i])), ":", body[i + 1:]
    return body, "", ""


def _list(rows, pos, indent):
    result = []
    while pos < len(rows):
        ind, body, number = rows[pos]
        if ind < indent:
            break
        if ind > indent or not (body.startswith("- ") or body == "-"):
            break
        item = body[1:].strip()
        pos += 1
        if item == "":
            if pos < len(rows) and rows[pos][0] > indent:
                value, pos = _block(rows, pos, rows[pos][0])
                result.append(value)
            else:
                result.append(None)
            continue
        key, sep, rest = _split_key(item)
        if sep:
            child_indent = indent + (len(body) - len(body[1:].lstrip()))
            nested = [(child_indent, item, number)]
            while pos < len(rows) and rows[pos][0] > indent:
                nested.append(rows[pos])
                pos += 1
            value, _ = _map(nested, 0, child_indent)
            result.append(value)
        else:
            result.append(_scalar(item))
    return result, pos


# ---------------------------------------------------------------- one file
def read_document(path, name=None):
    """Return {name, kind, sha256, bytes, text, data, problem}. text is always a string; data is the parsed JSON or YAML, or None."""
    path = Path(path)
    doc = {"name": name or path.name, "kind": KINDS.get(path.suffix.lower(), "unsupported"), "sha256": "", "bytes": 0,
           "text": "", "data": None, "problem": None}
    try:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            doc["problem"] = "larger than %d MB; split it or supply the rules as JSON" % (MAX_FILE_BYTES // (1024 * 1024))
            doc["bytes"] = size
            return doc
        raw = path.read_bytes()
    except OSError as error:
        doc["problem"] = "cannot be read: %s" % error.__class__.__name__
        return doc
    doc["bytes"] = len(raw)
    doc["sha256"] = sha256(raw)
    kind = doc["kind"]
    try:
        if kind == "unsupported":
            raise IngestError("file type %s is not supported (use json, yaml, md, txt, html, docx or pdf)" % (path.suffix or "without extension"))
        if kind == "docx":
            doc["text"] = docx_text(raw)
        elif kind == "pdf":
            doc["text"] = pdf_text(raw)
        else:
            text = raw.decode("utf-8-sig", errors="replace")
            if kind == "html":
                text = html_text(text)
            doc["text"] = text
            if kind == "json":
                doc["data"] = json.loads(raw.decode("utf-8-sig"))
            elif kind == "yaml":
                doc["data"] = parse_yaml(text)
    except (IngestError, ValueError, RecursionError) as error:
        doc["problem"] = str(error) or error.__class__.__name__
        if kind not in ("json", "yaml"):
            doc["text"] = ""
    doc["text"] = doc["text"][:MAX_TEXT_CHARS].replace(chr(13), "")
    return doc


# ---------------------------------------------------------------- discovery
def discover(repo, extra=()):
    """The rules files for a project: every explicit path, else the usual folders. Returns (list of (display name, path), problems)."""
    root = Path(repo).resolve()
    found, problems, seen = [], [], set()

    def add(path, label):
        real = path.resolve()
        if real in seen:
            return
        seen.add(real)
        found.append((label, real))

    def walk(folder, label_root, explicit):
        for p in sorted(folder.rglob("*"), key=lambda x: x.as_posix()):
            if p.is_file() and p.suffix.lower() in KINDS and ".git" not in p.parts and not p.is_symlink():
                add(p, p.relative_to(label_root).as_posix())

    for item in extra or ():
        target = Path(item)
        if not target.is_absolute():
            target = (Path.cwd() / target)
        if target.is_dir():
            walk(target, target.parent if target.parent != target else target, True)
        elif target.is_file():
            add(target, target.name)
        else:
            problems.append("%s does not exist" % item)
    if not extra:
        for loc in DEFAULT_LOCATIONS:
            folder = root / loc
            if folder.is_dir() and not folder.is_symlink():
                walk(folder, root, False)
        for name in DEFAULT_FILES:
            file = root / name
            if file.is_file() and not file.is_symlink():
                add(file, name)
    if len(found) > MAX_FILES:
        problems.append("%d rules files found; only the first %d are read" % (len(found), MAX_FILES))
        found = found[:MAX_FILES]
    return found, problems


def read_all(repo, extra=()):
    found, problems = discover(repo, extra)
    docs, total = [], 0
    for label, path in found:
        doc = read_document(path, label)
        total += doc["bytes"]
        if total > MAX_TOTAL_BYTES:
            problems.append("stopped reading at %s: the rules files are larger than %d MB in total" % (label, MAX_TOTAL_BYTES // (1024 * 1024)))
            break
        docs.append(doc)
    return docs, problems


def main(argv=None):
    paths = list(argv if argv is not None else sys.argv[1:])
    docs, problems = read_all(".", paths) if paths else ([], ["give one or more files or folders"])
    files = []
    for d in docs:
        row = {k: d[k] for k in ("name", "kind", "sha256", "bytes", "problem")}
        row["chars"] = len(d["text"])
        files.append(row)
    print(json.dumps({"files": files, "problems": problems}, indent=2))
    return 0 if docs and not problems and not any(d["problem"] for d in docs) else 1


if __name__ == "__main__":
    sys.exit(main())
