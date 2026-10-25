"""Execution-log framing, conservative comparisons, and converter/CLI failures."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bzlog
import bzlog_execlog as execlog


def spawn(output="bazel-out/k8/bin/pkg/a.o", **updates):
    result = {"mnemonic": "CppCompile", "targetLabel": "//pkg:a", "listedOutputs": [output],
              "commandArgs": ["compiler", "-c", "pkg/a.cc"],
              "environmentVariables": [{"name": "MODE", "value": "old-secret"}],
              "platform": {"properties": [{"name": "OSFamily", "value": "Linux"}]},
              "inputs": [{"path": "pkg/a.cc", "digest": {"hash": "aa", "sizeBytes": "12"}}],
              "runner": "linux-sandbox", "cacheable": True, "remoteCacheable": True}
    result.update(updates)
    return result


def actions(*rows):
    return [execlog.normalize(row) for row in rows]


class ExecLogTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-execlog-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "spawn.json"

    def invoke(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = bzlog.main(["exec-log", *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_concatenated_pretty_ndjson_and_split_unicode(self):
        rows = [spawn(commandArgs=['quoted \\" { [ }', "à文字"]), spawn("b.o")]
        raw = "".join(json.dumps(row, indent=2, ensure_ascii=False) for row in rows).encode()
        class SmallReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))
        self.assertEqual(list(execlog.json_records(SmallReads(raw))), rows)
        raw = b"\n".join(json.dumps(row).encode() for row in rows)
        self.assertEqual(list(execlog.json_records(io.BytesIO(raw))), rows)

    def test_malformed_truncated_nonobject_duplicate_and_oversized_records_fail(self):
        for raw in (b"[]", b'{"a":1,"a":2}', b'{"a":', b'{"x":"\xff"}',
                    b'{"valid":1}trailing', b'{"x":]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                list(execlog.json_records(io.BytesIO(raw)))
        with self.assertRaisesRegex(ValueError, "exceeds"):
            list(execlog.json_records(io.BytesIO(b'{"long":"value"}'), max_record_bytes=10))

    def test_proto_defaults_and_snake_case_are_supported(self):
        row = execlog.normalize({"mnemonic": "TestRunner", "target_label": "//a", "listed_outputs": ["a"],
                                 "timeout_millis": "12", "exit_code": "0"})
        self.assertFalse(row.cache_hit)
        self.assertFalse(row.cacheable)
        self.assertEqual(row.timeout, 12)
        self.assertEqual(row.exit_code, 0)
        self.assertIsNone(row.digest)

    def test_invalid_field_types_and_ambiguous_paths_are_rejected(self):
        for row in ({}, {"finished": {}}, spawn(cacheHit="false"), spawn(exitCode=True),
                    spawn(commandArgs="compiler"), spawn(listedOutputs=[None]),
                    spawn(target_label="//conflicting"),
                    spawn(inputs=[{"path": "a"}, {"path": "a"}]),
                    spawn(digest={"hash": "aa", "sizeBytes": -1}),
                    spawn(environmentVariables=[{"name": "A"}, {"name": "A"}])):
            with self.subTest(row=row), self.assertRaises(ValueError):
                execlog.normalize(row)

    def test_summary_separates_hits_from_execution_and_cache_policy(self):
        data = actions(spawn(cacheHit=True, runner="remote cache hit"),
                       spawn("b", cacheHit=True, runner="disk cache hit"),
                       spawn("c", cacheable=False, remoteCacheable=False), spawn("d"))
        result = execlog.summarize(data, 1)
        self.assertEqual(result["spawns"], 4)
        self.assertEqual(result["cache_hits"], 2)
        self.assertEqual(result["without_cache_hit"], 2)
        self.assertEqual(result["not_cacheable"], 1)
        self.assertEqual(result["actions_without_cache_hit"]["omitted"], 1)
        self.assertFalse(result["actions_without_cache_hit"]["items"][0]["cacheable"])

    def test_input_argument_environment_platform_and_policy_differences(self):
        before = spawn()
        after = spawn(commandArgs=["compiler", "secret-argument"],
                      environmentVariables=[{"name": "MODE", "value": "new-secret"}],
                      platform={"properties": [{"name": "OSFamily", "value": "Windows"}]},
                      inputs=[{"path": "pkg/a.cc", "digest": {"hash": "bb", "sizeBytes": 12}},
                              {"path": "pkg/new.h"}], timeoutMillis="10", cacheable=False)
        result = execlog.compare(actions(before), actions(after), 25)
        row = result["changed"]["items"][0]
        self.assertEqual(set(row["fields"]), {"arguments", "environment", "platform", "inputs", "timeout", "cache_policy"})
        self.assertEqual(row["input_paths"]["items"], ["pkg/a.cc", "pkg/new.h"])
        self.assertEqual(row["environment_names"]["items"], ["MODE"])
        self.assertEqual(row["platform_names"]["items"], ["OSFamily"])
        self.assertEqual(row["inputs_without_digest"]["current"], 1)
        self.assertIsNone(row["action_digest_changed"])
        self.assertNotIn("secret", json.dumps(result))

    def test_digests_symlinks_tools_and_added_removed_inputs(self):
        before = spawn(inputs=[{"path": "link", "symlinkTargetPath": "first"}, {"path": "removed"},
                               {"path": "tool", "digest": {"hash": "cc"}}])
        after = spawn(inputs=[{"path": "link", "symlinkTargetPath": "second"}, {"path": "added"},
                              {"path": "tool", "digest": {"hash": "cc"}, "isTool": True}])
        row = execlog.compare(actions(before), actions(after), 25)["changed"]["items"][0]
        self.assertEqual(row["input_paths"]["items"], ["added", "link", "removed", "tool"])

    def test_order_metrics_runner_and_actual_output_changes_are_not_input_changes(self):
        before = spawn(environmentVariables=[{"name": "A", "value": "1"}, {"name": "B", "value": "2"}],
                       inputs=[{"path": "a", "digest": {"hash": "aa"}}, {"path": "b"}],
                       listedOutputs=["b.o", "a.o"])
        after = copy.deepcopy(before)
        after["environmentVariables"].reverse()
        after["inputs"].reverse()
        after["listedOutputs"].reverse()
        after.update(runner="remote cache hit", cacheHit=True, metrics={"totalTime": "5s"},
                     actualOutputs=[{"path": "a.o", "digest": {"hash": "ff"}}])
        result = execlog.compare(actions(before), actions(after), 25)
        self.assertEqual(result["no_recorded_changes"], 1)
        self.assertEqual(result["changed"]["total"], 0)

    def test_digest_only_changes_and_unavailable_digests(self):
        before = spawn(digest={"hash": "aa", "hashFunctionName": "SHA256", "sizeBytes": "5"})
        after = spawn(digest={"hash": "bb", "hashFunctionName": "SHA256", "sizeBytes": 5})
        row = execlog.compare(actions(before), actions(after), 25)["changed"]["items"][0]
        self.assertEqual(row["fields"], [])
        self.assertTrue(row["action_digest_changed"])
        self.assertEqual(execlog.compare(actions(before), actions(spawn()), 25)["no_recorded_changes"], 1)

    def test_repeated_platform_property_names_preserve_multiplicity(self):
        properties = [{"name": "feature", "value": "a"}, {"name": "feature", "value": "b"}]
        before = spawn(platform={"properties": properties})
        reordered = spawn(platform={"properties": properties[::-1]})
        self.assertEqual(execlog.compare(actions(before), actions(reordered), 25)["no_recorded_changes"], 1)
        changed = spawn(platform={"properties": [properties[0], properties[0]]})
        row = execlog.compare(actions(before), actions(changed), 25)["changed"]["items"][0]
        self.assertEqual(row["platform_names"]["items"], ["feature"])

    def test_reader_never_follows_growth_beyond_the_initial_size(self):
        first, appended = json.dumps(spawn()).encode(), json.dumps(spawn("second")).encode()
        stream = io.BytesIO(first + appended)
        records = list(execlog.json_records(stream, byte_limit=len(first)))
        self.assertEqual(len(records), 1)
        self.assertEqual(stream.tell(), len(first))

    def test_duplicate_matches_outputless_spawns_and_configuration_changes_are_explicit(self):
        before = actions(spawn(), spawn(), spawn("old"), spawn(listedOutputs=[]))
        after = actions(spawn(), spawn("bazel-out/other-config/pkg/a.o"), spawn(listedOutputs=[]))
        result = execlog.compare(before, after, 25)
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["ambiguous"]["total"], 1)
        self.assertEqual(result["only_in_baseline"]["total"], 1)
        self.assertEqual(result["only_in_current"]["total"], 1)
        self.assertEqual(result["without_outputs"], {"baseline": 1, "current": 1})
        self.assertNotIn("fixed", json.dumps(result))

    def test_row_and_detail_limits_do_not_truncate_counts(self):
        before = actions(*(spawn(str(i), inputs=[]) for i in range(30)))
        after = actions(*(spawn(str(i), inputs=[{"path": str(j)} for j in range(20)]) for i in range(30)))
        result = execlog.compare(before, after, 3)
        self.assertEqual(result["changed"]["total"], 30)
        self.assertEqual(result["changed"]["omitted"], 27)
        self.assertEqual(result["changed"]["items"][0]["input_paths"]["omitted"], 10)

    def test_cli_json_comparison_and_text(self):
        baseline = self.root / "before.json"
        baseline.write_text(json.dumps(spawn()))
        self.log.write_text(json.dumps(spawn(commandArgs=["changed"])))
        code, out, err = self.invoke(self.log, "--input-format", "json", "--baseline", baseline)
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["schema"], "bzlog/exec-log-v1")
        self.assertEqual(report["comparison"]["changed"]["total"], 1)
        code, out, err = self.invoke(self.log, "--input-format", "json", "--baseline", baseline, "--format", "text")
        self.assertEqual(code, 0, err)
        self.assertIn("arguments", out)
        self.assertIn("does not prove", out)

    def test_empty_json_can_mean_local_action_cache_reuse(self):
        self.log.touch()
        code, out, err = self.invoke(self.log, "--input-format", "json")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["summary"]["spawns"], 0)
        self.assertIn("local action cache", " ".join(report["notices"]))

    def test_no_partial_report_after_a_bad_record_or_missing_file(self):
        self.log.write_text(json.dumps(spawn()) + '{"truncated":')
        for path in (self.log, self.root / "missing"):
            code, out, err = self.invoke(path, "--input-format", "json")
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertNotIn("Traceback", err)

    def test_missing_converter_and_invalid_limits(self):
        self.log.write_text(json.dumps(spawn()))
        code, out, err = self.invoke(self.log)
        self.assertEqual(code, 2)
        self.assertIn("--converter", err)
        for args in (("--timeout", "nan"), ("--timeout", "0"), ("--max-actions", "0"),
                     ("--max-record-bytes", "-1"), ("--baseline-format", "json")):
            with self.subTest(args=args), self.assertRaises(SystemExit) as raised:
                self.invoke(self.log, *args)
            self.assertEqual(raised.exception.code, 2)

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO")
    def test_json_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.log)
        proc = subprocess.run([sys.executable, bzlog.__file__, "exec-log", str(self.log),
                               "--input-format", "json"], capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("regular file", proc.stderr)

    def test_mutating_json_file_is_rejected(self):
        self.log.write_text(json.dumps(spawn()))
        original = execlog.json_records
        def changing(stream, max_record_bytes, byte_limit=None):
            yield from original(stream, max_record_bytes, byte_limit)
            self.log.write_text("changed")
        with patch.object(execlog, "json_records", side_effect=changing):
            with self.assertRaisesRegex(ValueError, "changed"):
                execlog.read_json(self.log, execlog.MAX_RECORD_BYTES)

    def test_reduced_real_bazel_logs_preserve_hits_and_changed_source(self):
        root = Path(__file__).parent / "testdata/bazel-9.3/execlog"
        before = execlog.read_json(root / "baseline.json", execlog.MAX_RECORD_BYTES)
        cached = execlog.read_json(root / "disk-hit.json", execlog.MAX_RECORD_BYTES)
        changed = execlog.read_json(root / "changed.json", execlog.MAX_RECORD_BYTES)
        self.assertEqual(execlog.summarize(cached, 25)["cache_hits"], 2)
        self.assertEqual(execlog.compare(before, cached, 25)["no_recorded_changes"], 2)
        result = execlog.compare(before, changed, 25)
        compile_row = next(row for row in result["changed"]["items"] if row["mnemonic"] == "CppCompile")
        self.assertEqual(compile_row["fields"], ["arguments", "environment", "inputs"])
        self.assertEqual(compile_row["input_paths"]["items"], ["src/test/cpp/util/md5_test.cc"])
        self.assertTrue(compile_row["action_digest_changed"])


@unittest.skipUnless(os.name == "posix", "POSIX executable")
class ConverterTest(unittest.TestCase):
    setUp = ExecLogTest.setUp
    invoke = ExecLogTest.invoke

    def converter(self, body):
        executable = self.root / "converter with spaces"
        executable.write_text(f"#!{sys.executable}\nimport json,sys,time\nfrom pathlib import Path\n" + body)
        executable.chmod(0o755)
        return executable

    def test_converter_argv_and_cleanup_without_shell_interpretation(self):
        source = self.root / "$(literal) compact.log"
        source.write_bytes(b"compact bytes")
        converter = self.converter(
            "assert sys.argv[1] == '--input' and sys.argv[3] == '--output'\n"
            "assert sys.argv[2].startswith('compact:')\n"
            f"Path(sys.argv[4].split(':',1)[1]).write_text({json.dumps(spawn())!r})\n")
        with patch.object(tempfile, "tempdir", str(self.root)):
            code, out, err = self.invoke(source, "--converter", converter)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["summary"]["spawns"], 1)
        self.assertEqual(list(self.root.glob("bzlog-execlog-*")), [])
        self.assertEqual(source.read_bytes(), b"compact bytes")

    def test_converter_failure_timeout_missing_output_and_bad_output(self):
        self.log.write_bytes(b"compact")
        for body, expected in (("print('decoder error',file=sys.stderr)\nsys.exit(4)\n", "exited 4"),
                               ("time.sleep(5)\n", "exceeded"), ("pass\n", "No such file"),
                               ("Path(sys.argv[4].split(':',1)[1]).write_text('broken')\n", "Expected")):
            with self.subTest(body=body):
                converter = self.converter(body)
                with patch.object(tempfile, "tempdir", str(self.root)):
                    code, out, err = self.invoke(self.log, "--converter", converter, "--timeout", "0.2")
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn(expected, err)
                self.assertEqual(list(self.root.glob("bzlog-execlog-*")), [])

    def test_converter_input_mutation_is_rejected(self):
        self.log.write_bytes(b"compact")
        converter = self.converter(
            "Path(sys.argv[2].split(':',1)[1]).write_bytes(b'changed input')\n"
            f"Path(sys.argv[4].split(':',1)[1]).write_text({json.dumps(spawn())!r})\n")
        code, out, err = self.invoke(self.log, "--converter", converter)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("changed", err)


if __name__ == "__main__":
    unittest.main()
