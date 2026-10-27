"""Periodic stderr feedback without changing Bazel's stream or process outcome."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import bzlog


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(bzlog.time, "monotonic", return_value=0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.container = bzlog.Container()
        self.heartbeat = bzlog.BuildHeartbeat(60, self.container)

    def observe(self, event):
        self.container.consume(event)
        self.heartbeat.observe(event)

    def tick(self, when):
        self.now.return_value = when
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.heartbeat.tick()
        return stderr.getvalue()

    def test_duration_validation(self):
        for value, expected in [("0", 0), ("60", 60), (".5s", .5), ("2m", 120), ("1.5h", 5400)]:
            self.assertEqual(bzlog.heartbeat_duration(value), expected)
        for value in ("-1", "nan", "inf", "1e2", "1ms", "", "1m2s", "9" * 1000):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                bzlog.heartbeat_duration(value)

    def test_deadline_elapsed_unknown_counts_and_flush(self):
        self.assertEqual(self.tick(59), "")
        text = self.tick(60)
        self.assertIn("[bzlog 00:01:00] waiting for Bazel", text)
        self.assertIn("actions: ?/?, running: ?", text)
        self.assertIn("0 action events observed (0 failed)", text)
        self.assertEqual(self.tick(60), "")
        self.assertIn("[bzlog 01:02:03]", self.tick(3723))
        self.now.return_value = 3783
        with patch("builtins.print") as output:
            self.heartbeat.tick()
        self.assertTrue(output.call_args.kwargs["flush"])
        self.assertIs(output.call_args.kwargs["file"], sys.stderr)

    def test_split_ansi_progress_comma_counts_and_running(self):
        self.observe({"progress": {"stderr": "\x1b[3"}})
        self.observe({"progress": {"stderr": "2m[14,551 / 17,"}})
        self.observe({"progress": {"stderr": "368] Compiling; 16 actions running\x1b[0m\n"}})
        text = self.tick(60)
        self.assertIn("last progress: 14,551/17,368 actions, 16 running", text)
        self.assertNotIn("\x1b", text)
        self.observe({"progress": {"stderr": "[14,552 / 17,368] SlowSmoke target.out; 1s linux-sandbox\n"}})
        self.assertIn("14,552/17,368 actions, ? running", self.tick(120))

    def test_carriage_return_updates_take_latest_snapshot(self):
        self.observe({"progress": {"stderr": "[1 / 3] 2 running\r[2 / 3] 1 running\r"}})
        self.assertIn("2/3 actions, 1 running", self.tick(60))

    def test_observed_event_counts_do_not_claim_all_actions_and_latest_is_new_only(self):
        self.observe({"action": {"success": True}})
        self.observe({"id": {"actionCompleted": {"label": "//lib"}},
                      "action": {"failureDetail": {"message": "failed\nwith details"}}})
        self.observe({"completed": {"success": True}})
        text = self.tick(60)
        self.assertIn("2 action events observed (1 failed), 1 target completions observed", text)
        self.assertIn("latest: action_failure //lib failed with details", text)
        self.assertIn("actions: ?/?", text)
        self.assertNotIn("latest:", self.tick(120))

    def test_malformed_progress_and_bounded_partial_line_do_not_crash_heartbeat(self):
        for event in ({"progress": None}, {"progress": []}, {"progress": {"stderr": 3}},
                      {"progress": {"stderr": "[, / ,]"}}):
            self.heartbeat.observe(event)
        self.heartbeat.observe({"progress": {"stderr": "x" * 10000}})
        self.assertLessEqual(len(self.heartbeat.fragment), 4096)
        self.assertIn("actions: ?/?", self.tick(60))

    def test_oversized_progress_numbers_preserve_last_useful_snapshot(self):
        self.observe({"progress": {"stderr": "[1 / 3] 2 running\n"}})
        self.observe({"progress": {"stderr": "[" + "9" * 5000 + " / 3]\n"}})
        self.assertIn("1/3 actions, 2 running", self.tick(60))
        self.observe({"progress": {"stderr": "[2 / 3] " + "9" * 5000 + " running\n"}})
        self.assertIn("2/3 actions, ? running", self.tick(120))

    def test_disabled_heartbeat_emits_nothing(self):
        self.heartbeat = bzlog.BuildHeartbeat(0, self.container)
        self.assertEqual(self.tick(100000), "")

    def test_cli_defaults_overrides_and_bazel_argument_boundary(self):
        for args, expected in [([], 0), (["--format", "text"], 60),
                               (["--format", "text", "--heartbeat=0"], 0),
                               (["--heartbeat", ".1s"], .1)]:
            with self.subTest(args=args), patch.object(bzlog, "run_bazel", return_value=(0, 0)) as run:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(bzlog.main([*args, "build", "//target"]), 0)
                self.assertEqual(run.call_args.args[-1], expected)
        own, forwarded = bzlog.split_argv(["--heartbeat", "1m", "run", "//x", "--", "--heartbeat=5s"])
        self.assertEqual(own, ["--heartbeat", "1m"])
        self.assertEqual(forwarded, ["run", "//x", "--", "--heartbeat=5s"])


@unittest.skipUnless(os.name == "posix", "fake executable uses a POSIX shebang")
class LiveHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-heartbeat-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = Path(bzlog.__file__).resolve()
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                        TMPDIR=str(self.root))

    def fake(self, body):
        executable = self.root / "bazel"
        executable.write_text(f"#!{sys.executable}\nimport json, sys, time, signal\n" + body)
        executable.chmod(0o755)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(self.script), *args], env=self.env,
                              cwd=self.root, capture_output=True, text=True, timeout=10)

    def test_redirected_heartbeat_is_flushed_before_bep_and_stdout_waits_for_exit(self):
        self.fake("time.sleep(1)\n"
                  "path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--build_event_json_file='))\n"
                  "with open(path, 'w') as out:\n"
                  "    out.write(json.dumps({'finished': {'exitCode': {'code': 0}}, 'lastMessage': True}) + '\\n')\n")
        stdout_path, stderr_path = self.root / "stdout", self.root / "stderr"
        bep = self.root / "not-created.jsonl"
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            process = subprocess.Popen([sys.executable, str(self.script), "--format", "text", "--heartbeat=.05s",
                                        "build", "//x", f"--build_event_json_file={bep}"],
                                       env=self.env, cwd=self.root, stdout=stdout, stderr=stderr)
            try:
                deadline = time.monotonic() + 5
                while not stderr_path.read_text() and time.monotonic() < deadline and process.poll() is None:
                    time.sleep(.02)
                self.assertIn("waiting for Bazel", stderr_path.read_text())
                self.assertIsNone(process.poll())
                self.assertFalse(bep.exists())
                self.assertEqual(stdout_path.read_text(), "")
                self.assertEqual(process.wait(timeout=5), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        self.assertNotIn("[bzlog", stdout_path.read_text())
        self.assertIn("->", stdout_path.read_text())

    def test_progress_idle_polling_and_nonzero_exit_preserve_json_report(self):
        self.fake("path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--build_event_json_file='))\n"
                  "with open(path, 'w') as out:\n"
                  "    out.write(json.dumps({'progress': {'stderr': '[4 / 9] SlowSmoke; 2 running\\n'}}) + '\\n')\n"
                  "    out.flush()\n"
                  "    time.sleep(.25)\n"
                  "    out.write(json.dumps({'finished': {'exitCode': {'code': 3}}, 'lastMessage': True}) + '\\n')\n"
                  "sys.exit(3)\n")
        result = self.run_cli("--heartbeat=.05s", "build", "//x")
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("4/9 actions, 2 running", result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["exit"]["code"], 3)
        self.assertEqual(json.loads(result.stdout)["invocation"]["stream"]["status"], "complete")

    def test_disabled_text_and_default_json_have_no_heartbeat(self):
        self.fake("time.sleep(.15)\n")
        for flags in ([], ["--format", "text", "--heartbeat=0"]):
            with self.subTest(flags=flags):
                result = self.run_cli(*flags, "build", "//x")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def test_saved_bep_does_not_emit_heartbeat(self):
        bep = self.root / "saved.jsonl"
        bep.write_text(json.dumps({"finished": {"exitCode": {"code": 0}}, "lastMessage": True}) + "\n")
        result = self.run_cli("--format", "text", "--heartbeat=.000001s", str(bep))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_heartbeat_does_not_change_fail_fast_signal_result(self):
        self.fake("signal.signal(signal.SIGINT, lambda *_: sys.exit(8))\n"
                  "time.sleep(.15)\n"
                  "path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--build_event_json_file='))\n"
                  "with open(path, 'w') as out:\n"
                  "    out.write(json.dumps({'aborted': {'reason': 'ANALYSIS_FAILURE', 'description': 'disk full'}}) + '\\n')\n"
                  "    out.flush()\n"
                  "    time.sleep(5)\n")
        result = self.run_cli("--heartbeat=.05s", "--fail-fast", "1", "build", "//x")
        self.assertEqual(result.returncode, 8, result.stderr)
        self.assertIn("[bzlog", result.stderr)
        self.assertEqual(json.loads(result.stdout)["invocation"]["stopped_early"]["reason"], "fail_fast")


if __name__ == "__main__":
    unittest.main()
