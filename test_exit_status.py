"""CLI exit-status regressions. Run with python3 test_exit_status.py."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().with_name("bzlog.py")


class ExitStatusTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, *args, env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True, text=True, env=env, timeout=10,
        )

    def wrap(self, status, output_format="json", write_bep=True, bep_path=None):
        # Exercise the real wrapper and CLI, replacing only the Bazel binary.
        fake = self.root / "bazel"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys\n"
            "print('ERROR: No space left on device', file=sys.stderr)\n"
            f"if {write_bep!r}:\n"
            "    path = next(a.split('=', 1)[1] for a in sys.argv\n"
            "                if a.startswith('--build_event_json_file='))\n"
            "    with open(path, 'w') as out:\n"
            "        out.write(json.dumps({'finished': {'exitCode': "
            "{'code': 0, 'name': 'SUCCESS'}}}) + '\\n')\n"
            f"if {status} < 0:\n"
            f"    os.kill(os.getpid(), {-status})\n"
            f"sys.exit({status})\n"
        )
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                   TMPDIR=str(self.root))
        extra = [f"--build_event_json_file={bep_path}"] if bep_path else []
        return self.invoke("--format", output_format, "build", "//:target", *extra, env=env)

    @unittest.skipUnless(os.name == "posix", "fake executable uses a POSIX shebang")
    def test_wrapper_preserves_process_status_and_emits_report(self):
        for output_format in ("json", "text"):
            for status in (0, 1, 3, 8, 37):
                with self.subTest(output_format=output_format, status=status):
                    result = self.wrap(status, output_format)
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertTrue(result.stdout.strip())
                    if output_format == "json":
                        report = json.loads(result.stdout)
                        # The process status takes precedence over the BEP code.
                        self.assertEqual(report["invocation"]["exit"]["code"], status)
                        self.assertEqual(report["invocation"]["overall_success"], status == 0)
                    elif status:
                        self.assertNotIn("-> SUCCESS", result.stdout)

    @unittest.skipUnless(os.name == "posix", "fake executable uses a POSIX shebang")
    def test_failure_before_bep_is_written(self):
        result = self.wrap(2, write_bep=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["invocation"]["exit"]["code"], 2)
        diagnostic = report["diagnostics"][0]
        evidence = self.invoke("show", report["evidence_sidecar"], diagnostic["evidence"])
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertIn("No space left on device", evidence.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX signal exit convention")
    def test_signal_termination_uses_shell_status(self):
        result = self.wrap(-signal.SIGTERM, write_bep=False)
        expected = 128 + signal.SIGTERM
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["exit"]["code"], expected)

    @unittest.skipUnless(os.name == "posix", "fake executable uses a POSIX shebang")
    def test_missing_requested_bep_file_still_reports_startup_failure(self):
        result = self.wrap(2, write_bep=False, bep_path=self.root / "not-created.json")
        self.assertEqual(result.returncode, 2, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("No space left on device", report["invocation"]["failure_context"])
        self.assertEqual(report["diagnostics"][0]["kind"], "invocation_failure")

    @unittest.skipUnless(os.name == "posix", "POSIX signals and executable shebang")
    def test_fail_fast_waits_for_the_analysis_root_cause(self):
        cause = {"configuredLabel": {"label": "//dep"}}
        events = [
            {"id": {"targetConfigured": {"label": "//target"}}, "children": [cause],
             "aborted": {"reason": "ANALYSIS_FAILURE"}},
            {"id": cause, "aborted": {"reason": "ANALYSIS_FAILURE", "description": "disk full"}},
        ]
        fake = self.root / "bazel"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, signal, sys, time\n"
            "signal.signal(signal.SIGINT, lambda *_: sys.exit(8))\n"
            "path = next(a.split('=', 1)[1] for a in sys.argv\n"
            "            if a.startswith('--build_event_json_file='))\n"
            "with open(path, 'w') as out:\n"
            f"    for event in {events!r}:\n"
            "        out.write(json.dumps(event) + '\\n')\n"
            "        out.flush()\n"
            "        time.sleep(0.25)\n"
            "time.sleep(5)\n"
        )
        fake.chmod(0o755)
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                   TMPDIR=str(self.root))
        result = self.invoke("--fail-fast", "1", "build", "//target", env=env)
        self.assertEqual(result.returncode, 8, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["description"], "disk full")
        self.assertEqual(report["invocation"]["stopped_early"]["distinct_causes"], 1)

    def test_post_hoc_failed_build_still_exits_zero(self):
        bep = self.root / "failed.json"
        bep.write_text(json.dumps({
            "finished": {"exitCode": {"code": 3, "name": "BUILD_FAILURE"}},
        }) + "\n")
        result = self.invoke(str(bep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["exit"]["code"], 3)


if __name__ == "__main__":
    unittest.main()
