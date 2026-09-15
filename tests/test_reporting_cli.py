"""Offline integration checks for report exports and the paper-only CLI."""

import copy
import csv
from datetime import datetime
from html import escape
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from paper_scalper import run_experiment
from reporting import write_reports
from test_paper_scalper import entry_quotes, quote


PROJECT = Path(__file__).resolve().parents[1]
REPORT_FILES = {
    "result.json", "trades.csv", "proposals.csv",
    "events.jsonl", "report.md", "dashboard.html",
}


class HTMLStructure(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReportingTests(unittest.TestCase):
    def test_empty_report_bundle_has_machine_readable_headers_and_exact_json(self):
        result = run_experiment([])
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(result, temporary)
            self.assertEqual(set(paths), REPORT_FILES)
            self.assertEqual({path.name for path in Path(temporary).iterdir()}, REPORT_FILES)
            self.assertTrue(all(Path(path).is_absolute() for path in paths.values()))
            self.assertEqual(json.loads(Path(paths["result.json"]).read_text()), result)
            for filename, required in (
                ("trades.csv", {"quantity", "entry_price", "exit_price", "net_pnl", "exit_reason"}),
                ("proposals.csv", {"at", "action", "symbol", "quantity", "expires_at", "mode"}),
            ):
                reader = csv.DictReader(io.StringIO(Path(paths[filename]).read_text()))
                self.assertTrue(required.issubset(reader.fieldnames))
                self.assertEqual(list(reader), [])
            self.assertEqual(Path(paths["events.jsonl"]).read_text(), "")

    def test_open_position_is_preserved_across_all_exports(self):
        result = run_experiment(entry_quotes())
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(result, temporary)
            saved = json.loads(Path(paths["result.json"]).read_text())
            self.assertEqual(saved["open_position"], result["open_position"])
            self.assertEqual(saved["trades"], [])
            self.assertEqual(list(csv.DictReader(io.StringIO(Path(paths["trades.csv"]).read_text()))), [])
            proposals = list(csv.DictReader(io.StringIO(Path(paths["proposals.csv"]).read_text())))
            self.assertEqual([item["action"] for item in proposals], ["BUY"])
            self.assertIn("Still open", Path(paths["report.md"]).read_text())
            self.assertIn("Position remains open", Path(paths["dashboard.html"]).read_text())

    def test_external_strings_are_escaped_in_dashboard_and_markdown(self):
        result = run_experiment(entry_quotes() + [quote(3, "100.30")])
        payload = '</title><script>alert("external")</script><img src=x onerror=alert(1)>'
        result["symbol"] = payload
        result["status"] = payload
        result["data_source"] = payload
        result["assumptions"].append(payload)
        result["trades"][0]["exit_reason"] = payload
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(result, temporary)
            for filename in ("dashboard.html", "report.md"):
                document = Path(paths[filename]).read_text()
                self.assertNotIn(payload, document)
                self.assertIn(escape(payload), document)
            structure = HTMLStructure()
            structure.feed(Path(paths["dashboard.html"]).read_text())
            self.assertFalse(any(tag in {"script", "img", "iframe"} for tag, _ in structure.elements))
            self.assertFalse(any(name.lower().startswith("on") for _, attrs in structure.elements for name in attrs))
            csp = next(attrs["content"] for tag, attrs in structure.elements
                       if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
            self.assertIn("default-src 'none'", csp)
            # The archival JSON retains its original data without HTML transformations.
            self.assertEqual(json.loads(Path(paths["result.json"]).read_text()), result)

    def test_csv_formula_text_is_neutralized_without_changing_numeric_losses(self):
        result = run_experiment(entry_quotes() + [quote(62, "100.02")])
        template = result["proposals"][0]
        payloads = ["=1+1", "+SUM(1,1)", "-SUM(1,1)", "@SUM(1,1)", "\t=1+1", "  =1+1"]
        result["proposals"] = []
        for payload in payloads:
            proposal = copy.deepcopy(template)
            proposal["symbol"] = payload
            proposal["reason"] = payload
            result["proposals"].append(proposal)
        with tempfile.TemporaryDirectory() as temporary:
            paths = write_reports(result, temporary)
            proposals = list(csv.DictReader(io.StringIO(Path(paths["proposals.csv"]).read_text())))
            for row, payload in zip(proposals, payloads, strict=True):
                self.assertEqual(row["symbol"], "'" + payload)
                self.assertEqual(row["reason"], "'" + payload)
            trades = list(csv.DictReader(io.StringIO(Path(paths["trades.csv"]).read_text())))
            self.assertEqual(trades[0]["net_pnl"], result["trades"][0]["net_pnl"])
            self.assertTrue(trades[0]["net_pnl"].startswith("-"))


class PaperCliTests(unittest.TestCase):
    def run_cli(self, input_path, output_path, *extra):
        return subprocess.run(
            [sys.executable, str(PROJECT / "paper_scalper.py"),
             "--quotes", str(input_path), "--output", str(output_path), *extra],
            cwd=PROJECT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def write_quotes(self, directory, observations):
        input_path = directory / "quotes.jsonl"
        input_path.write_text("".join(json.dumps(item) + "\n" for item in observations))
        return input_path

    def test_replay_cli_produces_coherent_results_without_mutating_input(self):
        observations = entry_quotes() + [quote(3, "100.30")]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            input_path = self.write_quotes(directory, observations)
            original_input = input_path.read_bytes()
            output_path = directory / "results"
            process = self.run_cli(input_path, output_path)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("PAPER", process.stdout)
            self.assertEqual({path.name for path in output_path.iterdir()}, REPORT_FILES)
            result = json.loads((output_path / "result.json").read_text())
            generated_at = datetime.fromisoformat(result.pop("report_generated_at").replace("Z", "+00:00"))
            self.assertIsNotNone(generated_at.utcoffset())
            self.assertEqual(result, run_experiment(observations))
            self.assertEqual(len(result["trades"]), 1)
            self.assertIsNone(result["open_position"])
            self.assertEqual(input_path.read_bytes(), original_input)

    def test_malformed_jsonl_line_is_counted_and_does_not_invent_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            input_path = self.write_quotes(directory, entry_quotes())
            with input_path.open("a") as stream:
                stream.write('{"incomplete":\n')
            output_path = directory / "results"
            process = self.run_cli(input_path, output_path)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads((output_path / "result.json").read_text())
            self.assertEqual(result["accepted_quotes"], 3)
            self.assertEqual(result["rejected_quotes"], 1)
            self.assertEqual(result["trades"], [])
            self.assertIsInstance(result["open_position"], dict)

    def test_invalid_cli_config_fails_before_producing_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            input_path = self.write_quotes(directory, entry_quotes())
            config_path = directory / "config.json"
            config_path.write_text(json.dumps({"fee_per_order": "25"}))
            output_path = directory / "results"
            process = self.run_cli(input_path, output_path, "--config", str(config_path))
            self.assertEqual(process.returncode, 2)
            self.assertIn("Error:", process.stderr)
            self.assertFalse(output_path.exists())

    def test_bounded_watch_replay_does_not_duplicate_trades_and_labels_stale_feed(self):
        observations = entry_quotes() + [quote(3, "100.30")]
        for item in observations:
            item["observed_at"] = item["observed_at"].replace("2026-09-15", "2020-01-01")
            item["quote_at"] = item["quote_at"].replace("2026-09-15", "2020-01-01")
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            input_path = self.write_quotes(directory, observations)
            output_path = directory / "results"
            process = self.run_cli(input_path, output_path, "--watch", "--duration-seconds", "1", "--poll-seconds", "0.1")
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads((output_path / "result.json").read_text())
            self.assertEqual(result["run_mode"], "watch")
            self.assertEqual(result["feed_status"], "stale")
            self.assertEqual(len(result["trades"]), 1)
            self.assertEqual(len(result["proposals"]), 2)
            self.assertEqual(result["ending_equity"], run_experiment(observations)["ending_equity"])


if __name__ == "__main__":
    unittest.main()
