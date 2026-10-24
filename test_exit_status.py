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

    def wrap(self, status, output_format="json", write_bep=True):
        # Exercise the real wrapper and CLI, replacing only the Bazel binary.
        fake = self.root / "bazel"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, signal, sys\n"
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
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])
        return self.invoke("--format", output_format, "build", "//:target", env=env)

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

    @unittest.skipUnless(os.name == "posix", "fake executable uses a POSIX shebang")
    def test_failure_before_bep_is_written(self):
        result = self.wrap(2, write_bep=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["exit"]["code"], 2)

    @unittest.skipUnless(os.name == "posix", "POSIX signal exit convention")
    def test_signal_termination_uses_shell_status(self):
        result = self.wrap(-signal.SIGTERM, write_bep=False)
        expected = 128 + signal.SIGTERM
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["exit"]["code"], expected)

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
