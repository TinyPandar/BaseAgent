"""Offline checks for scripts/rebind_corpus_version.py (AC18/AC19 line-drift detection)."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from applications.project_assistant.corpus import collect_corpus, compute_corpus_version

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rebind_corpus_version.py"
spec = importlib.util.spec_from_file_location("rebind_corpus_version", SCRIPT)
rebind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebind)


def digest(*lines):
    return sha256("\n".join(lines).encode()).hexdigest()


class RebindCorpusVersionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.doc = self.root / "guide.md"
        self.doc.write_text("# Guide\n\nalpha\nbeta\n", encoding="utf-8")
        self.questions = self.root / "questions.json"
        self.write({"guide.md:3-4": digest("alpha", "beta")}, ["guide.md:3-4"],
                   compute_corpus_version(collect_corpus(self.root)))

    def write(self, hashes, evidence, version):
        dataset = {"schema_version": 1, "corpus_version": version, "corpus_version_method": "method",
                   "evidence_sha256": hashes,
                   "questions": [{"id": "q1", "expected_evidence": evidence}, {"id": "q2", "expected_evidence": []}]}
        self.questions.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run_script(self, *options):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = rebind.main([*options, "--questions", str(self.questions), "--root", str(self.root)])
        return code, stdout.getvalue() + stderr.getvalue()

    def test_clean_dataset_passes_check(self):
        code, output = self.run_script("--check")
        self.assertEqual(code, 0, output)
        self.assertIn("(match)", output)
        self.assertIn("all 1 hashes match", output)

    def test_line_drift_is_reported_then_rebound_without_new_hashes(self):
        self.doc.write_text("# Guide\n\nintro\nmore\n\nalpha\nbeta\n", encoding="utf-8")
        code, output = self.run_script("--check")
        self.assertEqual(code, 1)
        self.assertIn("line drift: guide.md:3-4 -> guide.md:6-7 (hash unchanged)", output)
        self.assertIn("(differs)", output)
        code, output = self.run_script("--write")
        self.assertEqual(code, 0, output)
        dataset = json.loads(self.questions.read_text(encoding="utf-8"))
        self.assertEqual(dataset["evidence_sha256"], {"guide.md:6-7": digest("alpha", "beta")})
        self.assertEqual(dataset["questions"][0]["expected_evidence"], ["guide.md:6-7"])
        self.assertEqual(dataset["corpus_version"], compute_corpus_version(collect_corpus(self.root)))
        self.assertEqual(self.run_script("--check")[0], 0)

    def test_changed_text_is_refused(self):
        self.doc.write_text("# Guide\n\nalpha\ngamma\n", encoding="utf-8")
        code, output = self.run_script("--write")
        self.assertEqual(code, 1)
        self.assertIn("needs review: guide.md:3-4: cited text changed", output)
        self.assertIn("refusing to write", output)
        self.assertIn("guide.md:3-4", self.questions.read_text(encoding="utf-8"))

    def test_ambiguous_move_is_refused(self):
        self.doc.write_text("# Guide\n\nalpha\nbeta\nx\nalpha\nbeta\n", encoding="utf-8")
        self.write({"guide.md:1-2": digest("alpha", "beta")}, ["guide.md:1-2"], "old")
        code, output = self.run_script("--check")
        self.assertEqual(code, 1)
        self.assertIn("matches 2 places", output)


if __name__ == "__main__":
    unittest.main()
