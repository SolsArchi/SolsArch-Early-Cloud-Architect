"""Company rules documents are read from json, yaml, md, txt, html, docx and pdf, and hostile files are refused with a reason."""
import io
import json
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

import helpers  # noqa: F401
import rules_ingest as ri


def docx_bytes(paragraphs):
    body = "".join("<w:p><w:r><w:t>%s</w:t></w:r></w:p>" % p for p in paragraphs)
    xml = '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>%s</w:body></w:document>' % body
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buffer.getvalue()


def pdf_bytes(lines, compress=True):
    content = "BT\n" + "".join("(%s) Tj\nT*\n" % line.replace("(", "\\(").replace(")", "\\)") for line in lines) + "ET\n"
    raw = zlib.compress(content.encode("latin-1")) if compress else content.encode("latin-1")
    return b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(raw)).encode() + b" >>\nstream\n" + raw + b"\nendstream\nendobj\n%%EOF\n"


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def test_markdown_and_text(self):
        doc = ri.read_document(self.put("a.md", "# Rules\n- Databases must not be public.\n"))
        self.assertIsNone(doc["problem"])
        self.assertIn("Databases must not be public", doc["text"])
        self.assertEqual(len(doc["sha256"]), 64)

    def test_html_drops_scripts_and_keeps_text(self):
        doc = ri.read_document(self.put("a.html", "<html><head><style>p{}</style></head><body><script>evil()</script><p>Data must stay in the EU.</p></body></html>"))
        self.assertIn("Data must stay in the EU.", doc["text"])
        self.assertNotIn("evil", doc["text"])

    def test_json_and_yaml_are_parsed(self):
        j = ri.read_document(self.put("r.json", json.dumps({"rules": [{"kind": "allowed_clouds", "value": ["aws"]}]})))
        self.assertEqual(j["data"]["rules"][0]["kind"], "allowed_clouds")
        y = ri.read_document(self.put("r.yaml", "rules:\n  - kind: allowed_clouds\n    value: [aws, azure]  # comment\n    level: blocking\n  - kind: required_tags\n    value:\n      - owner\n      - cost-center\nignore:\n  - R-x\n"))
        self.assertIsNone(y["problem"])
        self.assertEqual(y["data"]["rules"][0]["value"], ["aws", "azure"])
        self.assertEqual(y["data"]["rules"][1]["value"], ["owner", "cost-center"])
        self.assertEqual(y["data"]["ignore"], ["R-x"])

    def test_yaml_block_text_and_quotes(self):
        y = ri.parse_yaml('a: "x: y"\nb: \'it\'\'s\'\nnote: |\n  one\n  two\nn: 5\nflag: true\n')
        self.assertEqual(y["a"], "x: y")
        self.assertEqual(y["b"], "it's")
        self.assertEqual(y["note"], "one\ntwo")
        self.assertEqual((y["n"], y["flag"]), (5, True))

    def test_bad_yaml_is_reported_not_raised(self):
        doc = ri.read_document(self.put("bad.yaml", "a:\n\t- tab\n"))
        self.assertIn("tab", doc["problem"])

    def test_bad_json_is_reported(self):
        doc = ri.read_document(self.put("bad.json", "{not json"))
        self.assertTrue(doc["problem"])

    def test_docx_text(self):
        doc = ri.read_document(self.put("p.docx", docx_bytes(["Approved clouds are AWS and Azure.", "Data must stay in the EU &amp; UK."])))
        self.assertIsNone(doc["problem"])
        self.assertIn("Approved clouds are AWS and Azure.", doc["text"])
        self.assertIn("EU & UK", doc["text"])

    def test_pdf_text_compressed_and_plain(self):
        for compress in (True, False):
            doc = ri.read_document(self.put("p%s.pdf" % compress, pdf_bytes(["Databases must not be public (ever).", "Logs are kept 90 days."], compress)))
            self.assertIsNone(doc["problem"], doc["problem"])
            self.assertIn("Databases must not be public (ever).", doc["text"])
            self.assertIn("Logs are kept 90 days.", doc["text"])

    def test_pdf_without_text_says_so(self):
        doc = ri.read_document(self.put("scan.pdf", b"%PDF-1.4\n1 0 obj\n<<>>\nstream\nabc\nendstream\n%%EOF"))
        self.assertIn("no text layer", doc["problem"])

    def test_encrypted_pdf_is_refused(self):
        doc = ri.read_document(self.put("enc.pdf", b"%PDF-1.4\n/Encrypt 5 0 R\n"))
        self.assertIn("encrypted", doc["problem"])

    def test_unsupported_type_is_reported(self):
        doc = ri.read_document(self.put("a.exe", b"MZ"))
        self.assertIn("not supported", doc["problem"])

    def test_oversized_file_is_refused(self):
        path = self.put("big.txt", b"x" * 10)
        old = ri.MAX_FILE_BYTES
        ri.MAX_FILE_BYTES = 5
        try:
            doc = ri.read_document(path)
        finally:
            ri.MAX_FILE_BYTES = old
        self.assertIn("larger than", doc["problem"])
        self.assertEqual(doc["text"], "")

    def test_zip_bomb_docx_is_refused(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", b"<w:t>a</w:t>" * 1 + b" " * (ri.MAX_UNPACKED + 10))
        doc = ri.read_document(self.put("bomb.docx", buffer.getvalue()))
        self.assertIn("once unpacked", doc["problem"])

    def test_compressed_pdf_bomb_is_refused(self):
        raw = zlib.compress(b"BT (a) Tj ET " + b" " * (ri.MAX_UNPACKED + 10))
        data = b"%PDF-1.4\nstream\n" + raw + b"\nendstream\n"
        doc = ri.read_document(self.put("bomb.pdf", data))
        self.assertIn("once unpacked", doc["problem"])

    def test_invalid_docx_is_reported(self):
        doc = ri.read_document(self.put("x.docx", b"not a zip"))
        self.assertIn("not a valid", doc["problem"])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_folders_are_found_in_order(self):
        (self.root / "policies").mkdir()
        (self.root / "policies" / "b.md").write_text("Data must stay in the EU.", encoding="utf-8")
        (self.root / ".solsarch" / "rules").mkdir(parents=True)
        (self.root / ".solsarch" / "rules" / "a.json").write_text("{}", encoding="utf-8")
        (self.root / "solsarch.rules.yaml").write_text("budget_monthly: 100\n", encoding="utf-8")
        found, problems = ri.discover(self.root)
        self.assertEqual([name for name, _ in found], [".solsarch/rules/a.json", "policies/b.md", "solsarch.rules.yaml"])
        self.assertEqual(problems, [])

    def test_no_files_means_nothing_found(self):
        found, _ = ri.discover(self.root)
        self.assertEqual(found, [])

    def test_explicit_path_that_is_missing_is_reported(self):
        _, problems = ri.discover(self.root, [str(self.root / "nope.md")])
        self.assertTrue(any("does not exist" in p for p in problems))

    def test_explicit_folder_is_walked_and_unknown_types_skipped(self):
        d = self.root / "std"
        d.mkdir()
        (d / "a.md").write_text("x", encoding="utf-8")
        (d / "b.bin").write_bytes(b"\0")
        found, _ = ri.discover(self.root, [str(d)])
        self.assertEqual([n for n, _ in found], ["std/a.md"])

    def test_file_count_is_capped(self):
        d = self.root / "policies"
        d.mkdir()
        for n in range(5):
            (d / ("f%d.md" % n)).write_text("x", encoding="utf-8")
        old = ri.MAX_FILES
        ri.MAX_FILES = 3
        try:
            found, problems = ri.discover(self.root)
        finally:
            ri.MAX_FILES = old
        self.assertEqual(len(found), 3)
        self.assertTrue(problems)


if __name__ == "__main__":
    unittest.main()
