"""Narrowed previews preserve argv boundaries and never execute commands."""
import copy
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from bzlog import Container, command_layout, rerun_plan, SCHEMA
from test_audit import audit, final


def metadata(options=(), residual=("//...",), command="test"):
    return {"commandLineLabel": "original", "sections": [
        {"sectionLabel": "command", "chunkList": {"chunk": [command]}},
        {"sectionLabel": "command options", "optionList": {"option": [
            {"combinedForm": form, "optionName": name, "optionValue": value}
            for form, name, value in options]}},
        {"sectionLabel": "residual", "chunkList": {"chunk": list(residual)}}]}


def report(argv=None, structured=None, events=None):
    argv = argv or ["bazel", "test", "//..."]
    structured = structured or metadata()
    return {"schema": SCHEMA, "invocation": {
        "stream": {"status": "complete"}, "command": "test", "replay": {
            "argv": argv, "cwd": "/tmp/a workspace", "layout": command_layout(argv, structured)}},
        "test_audit": audit(*(events or [final("FAILED")]))}


class RerunTest(unittest.TestCase):
    def test_preserves_startup_options_config_and_literal_arguments(self):
        argv = ["bazel", "--output_base", "/tmp/one two", "test", "//...",
                "--config=ci", "--test_arg", "$(literal) a b", "--test_filter=Foo.*"]
        structured = metadata([
            ("--config=ci", "config", "ci"),
            ("--test_arg $(literal) a b", "test_arg", "$(literal) a b"),
            ("--test_filter=Foo.*", "test_filter", "Foo.*")])
        plan = rerun_plan(report(argv, structured))
        self.assertEqual(plan["argv"], argv[:4] + argv[5:] + ["--", "//tests:one"])
        self.assertEqual(shlex.split(plan["command"]), plan["argv"])
        self.assertEqual(plan["cwd"], "/tmp/a workspace")

    def test_option_value_looks_like_target_and_interleaved_targets(self):
        argv = ["bazel", "test", "--test_arg", "//...", "//...", "--keep_going", "-//skip/..."]
        structured = metadata([("--test_arg //...", "test_arg", "//..."),
                               ("--keep_going", "keep_going", "1")], ["//...", "-//skip/..."])
        plan = rerun_plan(report(argv, structured))
        self.assertEqual(plan["argv"], ["bazel", "test", "--test_arg", "//...", "--keep_going",
                                       "--", "//tests:one"])

    def test_delimiter_and_repeated_capture_flags_removed_only_as_options(self):
        argv = ["bazel", "test", "--profile=x", "--profile", "a b", "--test_arg=--profile=x",
                "--", "//..."]
        structured = metadata([("--profile=x", "profile", "x"), ("--profile a b", "profile", "a b"),
                               ("--test_arg=--profile=x", "test_arg", "--profile=x")])
        plan = rerun_plan(report(argv, structured))
        self.assertEqual(plan["argv"], ["bazel", "test", "--test_arg=--profile=x", "--", "//tests:one"])
        self.assertEqual(plan["omitted_capture_options"], ["profile"])

    def test_final_failures_only_and_full_inventory_beyond_diagnostic_cap(self):
        events = [final("FAILED", label=f"//tests:t{i}") for i in range(35)]
        events += [final("FLAKY", label="//tests:flaky"), final("PASSED", label="//tests:passed")]
        c = Container(audit_tests=True)
        for event in events:
            c.handle(event)
        r = report()
        r.update({k: v for k, v in c.finish(0).items() if k != "invocation"})
        self.assertEqual(len(r["diagnostics"]), 25)
        self.assertEqual(len(rerun_plan(r)["targets"]), 35)

    def test_same_label_different_configurations_deduplicated_in_command_only(self):
        plan = rerun_plan(report(events=[final("FAILED", configuration="a"),
                                        final("FAILED", configuration="b")]))
        self.assertEqual(plan["targets"], ["//tests:one"])
        self.assertEqual(len(plan["failed_tests"]), 2)

    def test_no_failures_needs_no_replay(self):
        r = report(events=[final("FLAKY")])
        del r["invocation"]["replay"]
        self.assertEqual(rerun_plan(r)["status"], "nothing_to_rerun")

    def test_refuses_incomplete_missing_audit_or_replay_and_invalid_layout(self):
        base = report()
        variants = []
        r = copy.deepcopy(base); r["invocation"]["stream"]["status"] = "incomplete"; variants.append(r)
        r = copy.deepcopy(base); del r["test_audit"]; variants.append(r)
        r = copy.deepcopy(base); del r["invocation"]["replay"]; variants.append(r)
        r = copy.deepcopy(base); r["invocation"]["replay"]["layout"]["target_indices"] = [999]; variants.append(r)
        for r in variants:
            with self.subTest(r=r), self.assertRaises(ValueError):
                rerun_plan(r)

    def test_ambiguous_hidden_or_missing_metadata_refused(self):
        for argv, data in [(["bazel", "test", "--unknown", "//..."], metadata()),
                           (["bazel", "test", "--test_arg", "test", "//..."], metadata()),
                           (["bazel", "test", "--target_pattern_file=x"], metadata(residual=[])),
                           (["bazel", "run", "//..."], metadata(command="run"))]:
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                command_layout(argv, data)

    def test_only_recording_stores_layout_not_bep_environment(self):
        c = Container()
        event = {"structuredCommandLine": metadata()}
        c.handle(event)
        self.assertNotIn("replay", c.invocation)
        c.invocation["replay"] = {"argv": ["bazel", "test", "//..."]}
        c.handle(event)
        self.assertIn("layout", c.invocation["replay"])
        self.assertNotIn("structuredCommandLine", c.invocation)

    def test_cli_only_previews_and_handles_bad_json(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            path.write_text(json.dumps(report()))
            cmd = [sys.executable, str(Path(__file__).with_name("bzlog.py")), "rerun", str(path)]
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["status"], "preview")
            path.write_text("[]")
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 2)
            self.assertNotIn("Traceback", run.stderr)

    def test_real_bazel_original_command_preserves_literal_env_and_removes_capture_flags(self):
        path = Path(__file__).parent / "testdata/bazel-9.3/audit-original-command.json"
        data = json.loads(path.read_text())
        plan = rerun_plan(report(data["argv"], data["structuredCommandLine"]))
        self.assertIn("BZLOG_LITERAL=$(literal) a b", plan["argv"])
        self.assertIn("--nocache_test_results", plan["argv"])
        self.assertEqual(plan["omitted_capture_options"],
                         ["build_event_binary_file", "build_event_json_file", "profile"])


if __name__ == "__main__":
    unittest.main()
