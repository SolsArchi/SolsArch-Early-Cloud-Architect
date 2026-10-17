import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

import diagram_sanitizer as ds


class FlowchartTests(unittest.TestCase):
    def test_quotes_unquoted_labels_and_shapes(self):
        src = "flowchart TD\n  A[Web App (Next.js)] --> B[(Postgres (RDS))]\n  B --> C{Cache hit?}\n"
        out, dtype, issues = ds.sanitize(src)
        self.assertEqual(dtype, "flowchart")
        self.assertEqual(issues, [])
        self.assertIn('A["Web App (Next.js)"]', out)
        self.assertIn('B[("Postgres (RDS)")]', out)
        self.assertIn('C{"Cache hit?"}', out)

    def test_edge_labels_and_reserved_end(self):
        out, _, issues = ds.sanitize("graph LR\n  start[Start] -->|HTTPS 443| end[Done]\n")
        self.assertEqual(issues, [])
        self.assertIn('-->|"HTTPS 443"|', out)
        self.assertIn('end_node["Done"]', out)

    def test_subgraph_titles(self):
        src = ("flowchart TB\n  subgraph VPC [VPC 10.0.0.0/16]\n    A[API]\n  end\n"
               "  subgraph Public Subnet\n    B[ALB]\n  end\n")
        out, _, issues = ds.sanitize(src)
        self.assertEqual(issues, [])
        self.assertIn('subgraph VPC["VPC 10.0.0.0/16"]', out)
        self.assertIn('subgraph sg_1["Public Subnet"]', out)

    def test_angle_brackets_quotes_and_line_breaks(self):
        out, _, issues = ds.sanitize('flowchart TD\n  A[Say "hi" <b>x</b><br>Line 2]\n')
        self.assertEqual(issues, [])
        self.assertIn('A["Say #quot;hi#quot; #lt;b#gt;x#lt;/b#gt;<br/>Line 2"]', out)

    def test_fences_smart_quotes_and_zero_width(self):
        src = "```mermaid\nflowchart TD\n  A[“Quoted”​] --> B\n```\n"
        out, _, issues = ds.sanitize(src)
        self.assertEqual(issues, [])
        self.assertTrue(out.startswith("flowchart TD"))
        self.assertIn('A["Quoted"]', out)

    def test_style_lines_untouched(self):
        src = "flowchart TD\n  A[One]\n  classDef aws fill:#FF9900,stroke:#232F3E\n  class A aws\n"
        out, _, _ = ds.sanitize(src)
        self.assertIn("classDef aws fill:#FF9900,stroke:#232F3E", out)
        self.assertIn("class A aws", out)

    def test_idempotent(self):
        sources = [
            "flowchart TD\n  A[Web (x)] -->|a b| B[(DB)]\n  subgraph S [Net]\n    C{Q?}\n  end\n",
            "sequenceDiagram\n  participant A as Client; app\n  A->>B: hi; there <now>\n",
        ]
        for src in sources:
            once, _, _ = ds.sanitize(src)
            twice, _, _ = ds.sanitize(once)
            self.assertEqual(once, twice)

    def test_reports_unbalanced_structure(self):
        _, _, issues = ds.sanitize("flowchart TD\n  subgraph S\n    A[One]\n")
        self.assertTrue(any("subgraph" in i for i in issues))
        _, _, issues = ds.sanitize('flowchart TD\n  A --> "dangling\n')
        self.assertTrue(any("quote" in i for i in issues))
        out, _, issues = ds.sanitize('flowchart TD\n  A["unterminated]\n')
        self.assertEqual(issues, [])
        self.assertIn('A["#quot;unterminated"]', out)


class OtherDiagramTests(unittest.TestCase):
    def test_sequence_semicolons_and_angles(self):
        src = "sequenceDiagram\n  participant API as API; v2\n  Client->>API: GET /a; then <b>\n  Note over API: ok\n"
        out, dtype, issues = ds.sanitize(src)
        self.assertEqual(dtype, "sequencediagram")
        self.assertEqual(issues, [])
        self.assertIn("participant API as API#59; v2", out)
        self.assertIn("Client->>API: GET /a#59; then #lt;b#gt;", out)

    def test_c4_balance_check(self):
        good = 'C4Context\n  Person(u, "User", "Browses")\n  System(s, "Shop", "Sells")\n  Rel(u, s, "Uses")\n'
        self.assertEqual(ds.sanitize(good)[2], [])
        bad = 'C4Context\n  Person(u, "User", "Browses"\n'
        self.assertTrue(ds.sanitize(bad)[2])

    def test_unknown_and_empty(self):
        self.assertTrue(ds.sanitize("hello world\n")[2])
        self.assertTrue(ds.sanitize("\n\n")[2])
        self.assertTrue(ds.sanitize("flowchart TD\n")[2])

    def test_markdown_blocks(self):
        md = "# R\n\n```mermaid\nflowchart TD\n  A[One (1)] --> B\n```\n\ntext\n\n```python\nx = 1\n```\n"
        out, report = ds.sanitize_markdown(md)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]["issues"], [])
        self.assertTrue(report[0]["changed"])
        _, again = ds.sanitize_markdown(out)
        self.assertFalse(again[0]["changed"])
        self.assertIn('A["One (1)"]', out)
        self.assertIn("```python\nx = 1\n```", out)


class CliTests(unittest.TestCase):
    def run_cli(self, *args, stdin=None):
        script = str(helpers.SCRIPTS / "diagram_sanitizer.py")
        return subprocess.run([sys.executable, "-I", script, *args], input=stdin,
                              capture_output=True, text=True)

    def test_stdin_to_stdout(self):
        done = self.run_cli("-", stdin="flowchart TD\n  A[X (y)] --> B\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('A["X (y)"]', done.stdout)

    def test_exit_code_one_on_issues_and_json(self):
        done = self.run_cli("-", "--json", stdin="flowchart TD\n  subgraph S\n    A[One]\n")
        self.assertEqual(done.returncode, 1)
        payload = json.loads(done.stdout)
        self.assertFalse(payload["ok"])

    def test_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.mmd"
            path.write_text("flowchart TD\n  A[X (y)] --> B\n", encoding="utf-8")
            done = self.run_cli(str(path), "--in-place")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn('A["X (y)"]', path.read_text(encoding="utf-8"))

    def test_missing_file(self):
        self.assertEqual(self.run_cli("does-not-exist.mmd").returncode, 2)


if __name__ == "__main__":
    unittest.main()
