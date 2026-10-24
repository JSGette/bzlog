"""CLI evidence rendering, persistence, and retrieval regressions."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bzlog


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-evidence-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bep = self.root / "failure.json"
        self.bep.write_text(json.dumps({
            "id": {"configuredLabel": {"label": "//dep"}},
            "aborted": {"reason": "ANALYSIS_FAILURE", "description": "No space left on device"},
        }) + "\n")
        # Main normally persists into the process's temporary directory.
        patcher = patch.object(tempfile, "tempdir", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bzlog.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_inline_text_contains_evidence(self):
        self.bep.write_text("\n".join(json.dumps(event) for event in [
            {"id": {"targetConfigured": {"label": "//a"}},
             "aborted": {"reason": "ANALYSIS_FAILURE"}},
            {"progress": {"stderr": "ERROR: fetch failed\nTraceback:\nNo space left on device"}},
        ]) + "\n")
        code, stdout, stderr = self.invoke("--format", "text", "--evidence", "inline", str(self.bep))
        self.assertEqual(code, 0, stderr)
        self.assertIn("No space left on device", stdout)
        self.assertNotIn("bzlog-show", stdout)

    def test_evidence_files_are_unique_and_both_remain_readable(self):
        _, first, _ = self.invoke(str(self.bep))
        first = json.loads(first)
        self.bep.write_text(self.bep.read_text().replace("No space left on device", "Permission denied"))
        _, second, _ = self.invoke(str(self.bep))
        second = json.loads(second)
        self.assertNotEqual(first["evidence_sidecar"], second["evidence_sidecar"])
        for result, message in [(first, "No space left on device"), (second, "Permission denied")]:
            code, stdout, stderr = self.invoke("show", result["evidence_sidecar"],
                                               result["diagnostics"][0]["evidence"])
            self.assertEqual(code, 0, stderr)
            self.assertIn(message, stdout)
            if os.name == "posix":
                self.assertEqual(Path(result["evidence_sidecar"]).stat().st_mode & 0o777, 0o600)

    def test_text_references_a_real_show_command(self):
        _, stdout, _ = self.invoke("--format", "text", str(self.bep))
        self.assertIn("log: bzlog show ", stdout)
        self.assertNotIn("bzlog-show", stdout)

    def test_persistence_failure_falls_back_to_inline(self):
        with patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            code, stdout, stderr = self.invoke(str(self.bep))
        result = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertNotIn("evidence_sidecar", result)
        self.assertIn(result["diagnostics"][0]["evidence"], result["evidence"])
        self.assertIn("keeping it inline", stderr)

    def test_none_removes_evidence_references(self):
        _, stdout, _ = self.invoke("--evidence", "none", str(self.bep))
        result = json.loads(stdout)
        self.assertNotIn("evidence", result)
        self.assertNotIn("evidence_sidecar", result)
        self.assertNotIn("evidence", result["diagnostics"][0])

    def test_show_rejects_missing_or_invalid_evidence(self):
        sidecar = self.root / "evidence.json"
        for contents in ("not json", "{}", '{"e1": {"text": 42}}'):
            sidecar.write_text(contents)
            code, stdout, stderr = self.invoke("show", str(sidecar), "e1")
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertIn("cannot read", stderr)
        code, _, _ = self.invoke("show", str(self.root / "missing.json"), "e1")
        self.assertEqual(code, 2)

    def test_show_discloses_truncation(self):
        sidecar = self.root / "evidence.json"
        sidecar.write_text(json.dumps({"e1": {"text": "excerpt", "truncated": True}}))
        code, stdout, stderr = self.invoke("show", str(sidecar), "e1")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "excerpt\n")
        self.assertIn("truncated", stderr)


if __name__ == "__main__":
    unittest.main()
