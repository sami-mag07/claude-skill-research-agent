"""End-to-end tests for the source ledger and verify gate (sources.py), run as a CLI like the agent does."""
import os
import subprocess
import sys
import tempfile
import importlib.util
import unittest
from pathlib import Path

CLI = os.path.join(os.path.dirname(__file__), "..", "skills", "research-agent", "references", "sources.py")


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.ledger = os.path.join(self.d, "sources.json")
        self.page = os.path.join(self.d, "page.txt")
        with open(self.page, "w", encoding="utf-8") as f:
            f.write("The   programme pays\n€630 per month  and closes on 1 October.")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, CLI, "--ledger", self.ledger, *args],
                              capture_output=True, text=True)

    def draft(self, text):
        path = os.path.join(self.d, "draft.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_add_is_idempotent(self):
        self.assertEqual(self.run_cli("add", "https://a.org").stdout.strip(), "[s01]")
        self.assertEqual(self.run_cli("add", "https://b.org").stdout.strip(), "[s02]")
        self.assertEqual(self.run_cli("add", "https://a.org").stdout.strip(), "[s01]")

    def test_quote_must_appear_verbatim_whitespace_normalised(self):
        self.run_cli("add", "https://a.org")
        ok = self.run_cli("quote", "[s01]", "--from", self.page, "--text", "programme pays €630 per month")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        made_up = self.run_cli("quote", "1", "--from", self.page, "--text", "pays €700 per month")
        self.assertNotEqual(made_up.returncode, 0)

    def test_gate_is_red_until_every_citation_is_backed(self):
        self.run_cli("add", "https://a.org")
        self.run_cli("add", "https://b.org")
        self.run_cli("quote", "1", "--from", self.page, "--text", "closes on 1 October")
        draft = self.draft("Deadline is 1 October [s01]. Budget is large [s02].")
        red = self.run_cli("verify", draft)
        self.assertNotEqual(red.returncode, 0)
        self.assertIn("[s02]", red.stdout)
        relaxed = self.run_cli("verify", draft, "--min-coverage", "0.5")
        self.assertEqual(relaxed.returncode, 0, relaxed.stdout + relaxed.stderr)

    def test_numbers_mode_catches_changed_figure_but_passes_correct_report(self):
        self.run_cli("add", "https://a.org")
        self.run_cli("quote", "1", "--from", self.page, "--text", "programme pays €630 per month")
        good = self.draft("It pays 630 euros a month [s01].")
        self.assertEqual(self.run_cli("verify", good, "--numbers").returncode, 0)
        bad = self.draft("It pays 900 euros a month [s01].")
        self.assertEqual(self.run_cli("verify", bad).returncode, 0)  # plain gate cannot see it
        red = self.run_cli("verify", bad, "--numbers")
        self.assertNotEqual(red.returncode, 0)
        self.assertIn("900", red.stdout)

    def test_draft_without_citations_fails(self):
        self.assertNotEqual(self.run_cli("verify", self.draft("No sources at all.")).returncode, 0)


class ParseId(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).parents[1] / "skills" / "research-agent" / "references" / "sources.py"
        spec = importlib.util.spec_from_file_location("research_sources", path)
        cls.sources = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.sources)

    def test_parse_id_canonicalises_bracketed_and_zero_padded_values(self):
        self.assertEqual(self.sources.parse_id("[s01]"), "1")
        self.assertEqual(self.sources.parse_id("00042"), "42")
        self.assertEqual(self.sources.parse_id(0), "0")

    def test_parse_id_rejects_values_without_digits(self):
        for raw in ("", "source", None, []):
            with self.subTest(raw=raw):
                with self.assertRaises(SystemExit):
                    self.sources.parse_id(raw)


if __name__ == "__main__":
    unittest.main()
