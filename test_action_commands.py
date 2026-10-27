"""Failed action command retention, rendering, and evidence lifecycle."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bzlog
from test_diagnostics import action, report


def failed_compile(source="src/raw_api.c", output="obj/raw_api.o"):
    event = action("//lib:ffi", "ffi.h:92:10: fatal error: 'stddef.h' file not found")
    event["id"]["actionCompleted"]["primaryOutput"] = output
    event["action"]["commandLine"] = ["clang", "-c", source, "-o", output, "-nostdinc"]
    return event


class ActionCommandsTest(unittest.TestCase):
    def test_complete_argv_preserves_boundaries_and_late_flags(self):
        event = failed_compile("src/with space.c")
        argv = ["clang", "", "argument with spaces", "literal$(value)", "quote'\"\\\n"]
        argv += ["-Iinclude/" + str(i) for i in range(1000)] + ["-nostdinc", "src/with space.c"]
        event["action"]["commandLine"] = argv
        result = report(event)
        entry = result["evidence"][result["diagnostics"][0]["command_evidence"]]
        self.assertEqual(entry["kind"], "command_line")
        self.assertFalse(entry["truncated"])
        self.assertEqual(json.loads(entry["text"]), argv)

    def test_missing_argv_keeps_full_failure_message_as_message(self):
        event = failed_compile()
        del event["action"]["commandLine"]
        message = "clang failed: exec env -\n" + "  LONG_ENV=value\n" * 400 + "clang src/raw_api.c -nostdinc"
        event["action"]["failureDetail"] = {"message": message, "spawn": {"code": "EXECUTION_FAILED"}}
        result = report(event)
        diagnostic = result["diagnostics"][0]
        entry = result["evidence"][diagnostic["failure_message_evidence"]]
        self.assertEqual(entry["kind"], "failure_message")
        self.assertEqual(entry["text"], message)
        self.assertFalse(entry["truncated"])
        self.assertLessEqual(len(diagnostic["failure_message"]), 300)

    def test_argv_and_full_failure_message_are_both_preserved(self):
        event = failed_compile()
        event["action"]["failureDetail"] = {"message": "short failure summary"}
        result = report(event)
        entry = result["evidence"][result["diagnostics"][0]["command_evidence"]]
        self.assertEqual(entry["kind"], "command_line")
        self.assertEqual(json.loads(entry["text"]), event["action"]["commandLine"])
        failure = result["evidence"][result["diagnostics"][0]["failure_message_evidence"]]
        self.assertEqual(failure["kind"], "failure_message")
        self.assertEqual(failure["text"], "short failure summary")

    def test_text_identifies_primary_output_from_both_bep_locations(self):
        for payload in (False, True):
            with self.subTest(payload=payload):
                event = failed_compile()
                if payload:
                    event["action"]["primaryOutput"] = {"name": "obj/payload.o"}
                result = report(event)
                output = "obj/payload.o" if payload else "obj/raw_api.o"
                self.assertEqual(result["diagnostics"][0]["primary_output"], output)
                self.assertIn("output: " + output, bzlog.render_text(result))
                self.assertNotIn("primary_input", result["diagnostics"][0])

    def test_shared_stderr_does_not_hide_distinct_commands_or_outputs(self):
        for change in ("command", "output"):
            with self.subTest(change=change):
                first, second = failed_compile(), failed_compile()
                if change == "command":
                    second["action"]["commandLine"].append("-DSECOND")
                else:
                    second["id"]["actionCompleted"]["primaryOutput"] = "obj/other.o"
                result = report(first, second)
                self.assertEqual(len(result["diagnostics"]), 2)
                self.assertTrue(all(d["command_evidence"] in result["evidence"] for d in result["diagnostics"]))

    def test_duplicate_action_does_not_duplicate_command_evidence(self):
        event = failed_compile()
        result = report(event, event)
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(sum(e["kind"] == "command_line" for e in result["evidence"].values()), 1)

    def test_diagnostic_limit_collects_unused_command_evidence(self):
        with patch.object(bzlog, "MAX_DIAGNOSTICS", 1):
            result = report(failed_compile(), failed_compile("src/second.c", "obj/second.o"))
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(sum(e["kind"] == "command_line" for e in result["evidence"].values()), 1)
        self.assertNotIn("src/second.c", json.dumps(result["evidence"]))


class CommandEvidenceCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-command-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bep = self.root / "action.jsonl"
        self.event = failed_compile()
        self.event["action"]["commandLine"] += ["-Iinclude/" + str(i) for i in range(1000)] + ["-DLAST_FLAG"]
        self.event["action"]["failureDetail"] = {"message": "(cd execroot && env CUSTOM=value clang ... -DLAST_FLAG)"}
        self.bep.write_text(json.dumps(self.event) + "\n")
        patcher = patch.object(tempfile, "tempdir", str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bzlog.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_inline_ref_and_none_command_evidence(self):
        for mode in ("inline", "ref", "none"):
            with self.subTest(mode=mode):
                code, stdout, stderr = self.invoke("--evidence", mode, str(self.bep))
                self.assertEqual(code, 0, stderr)
                result = json.loads(stdout)
                diagnostic = result["diagnostics"][0]
                if mode == "none":
                    self.assertNotIn("command_evidence", diagnostic)
                    self.assertNotIn("failure_message_evidence", diagnostic)
                else:
                    ref = diagnostic["command_evidence"]
                    if mode == "ref":
                        code, text, stderr = self.invoke("show", result["evidence_sidecar"], ref)
                        self.assertEqual(code, 0, stderr)
                    else:
                        text = result["evidence"][ref]["text"]
                    self.assertEqual(json.loads(text), self.event["action"]["commandLine"])
                    failure_ref = diagnostic["failure_message_evidence"]
                    if mode == "ref":
                        code, text, stderr = self.invoke("show", result["evidence_sidecar"], failure_ref)
                        self.assertEqual(code, 0, stderr)
                        self.assertIn(self.event["action"]["failureDetail"]["message"], text)
                    else:
                        self.assertEqual(result["evidence"][failure_ref]["text"],
                                         self.event["action"]["failureDetail"]["message"])

    def test_text_ref_exposes_command_show_link_and_output(self):
        code, stdout, stderr = self.invoke("--format", "text", str(self.bep))
        self.assertEqual(code, 0, stderr)
        self.assertIn("output: obj/raw_api.o", stdout)
        self.assertIn("command: bzlog show ", stdout)
        self.assertIn("failure details: bzlog show ", stdout)
        self.assertLess(len(stdout), 1500)

    def test_bundle_saves_command_despite_none_output(self):
        destination = self.root / "bundle"
        code, stdout, stderr = self.invoke("--bundle", str(destination), "--evidence", "none", str(self.bep))
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("command_evidence", json.loads(stdout)["diagnostics"][0])
        self.assertNotIn("failure_message_evidence", json.loads(stdout)["diagnostics"][0])
        saved = json.loads((destination / "report.json").read_text())
        ref = saved["diagnostics"][0]["command_evidence"]
        self.assertEqual(json.loads(saved["evidence"][ref]["text"]), self.event["action"]["commandLine"])
        failure_ref = saved["diagnostics"][0]["failure_message_evidence"]
        self.assertEqual(saved["evidence"][failure_ref]["text"], self.event["action"]["failureDetail"]["message"])
        code, text, stderr = self.invoke("show", str(destination), ref)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(text), self.event["action"]["commandLine"])
        code, _, stderr = self.invoke("verify", str(destination))
        self.assertEqual(code, 0, stderr)


if __name__ == "__main__":
    unittest.main()
