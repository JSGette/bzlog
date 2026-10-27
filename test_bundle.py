"""Portable full evidence, capture provenance, and bundle failure behavior."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bzlog
from test_results import result, summary


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-bundle-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.destination = self.root / "bundle"

    def bundle(self):
        return bzlog.InvocationBundle(str(self.destination), "posthoc")

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = bzlog.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_new_directory_is_private_and_never_overwritten(self):
        b = self.bundle()
        before = (b.root / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.bundle()
        self.assertEqual((b.root / "manifest.json").read_bytes(), before)
        self.assertEqual(json.loads(before)["state"], "collecting")
        if os.name == "posix":
            self.assertEqual(b.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((b.root / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_full_binary_bytes_empty_files_and_blob_deduplication(self):
        b = self.bundle()
        data = bytes(range(256)) * 1000
        a = b.capture_stream(io.BytesIO(data), "test.log")
        other = b.capture_stream(io.BytesIO(data), "action_stderr")
        empty = b.capture_stream(io.BytesIO(b""), "test.xml")
        self.assertEqual(a["path"], other["path"])
        self.assertEqual((b.root / a["path"]).read_bytes(), data)
        self.assertEqual(a["bytes"], len(data))
        self.assertEqual(empty["status"], "saved")
        self.assertEqual(empty["bytes"], 0)

    def test_missing_remote_invalid_and_absent_are_distinct(self):
        b = self.bundle()
        cases = [({"uri": "missing.log"}, "missing"),
                 ({"uri": "bytestream://cache/blobs/abc/12"}, "remote_only"),
                 ({"uri": "file://other-host/log"}, "remote_only"),
                 ({"uri": "file:///%00"}, "unreadable"),
                 ({"contents": "%%%"}, "invalid_inline"), ({}, "no_reference")]
        for reference, expected in cases:
            with self.subTest(reference=reference):
                self.assertEqual(b.capture_reference(reference, "test.log", str(self.root))["status"], expected)

    def test_relative_and_encoded_file_paths_resolve_against_root(self):
        b = self.bundle()
        source = self.root / "a b.log"
        source.write_bytes(b"full log")
        for uri in ("a b.log", source.as_uri()):
            entry = b.capture_reference({"uri": uri}, "test.log", str(self.root))
            self.assertEqual(entry["status"], "saved")
            self.assertEqual((b.root / entry["path"]).read_bytes(), b"full log")

    @unittest.skipUnless(os.name == "posix", "POSIX special files")
    def test_fifo_and_directory_are_not_read(self):
        b = self.bundle()
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        self.assertEqual(b.capture_file(str(fifo), "test.log")["status"], "not_regular")
        self.assertEqual(b.capture_file(str(self.root), "test.log")["status"], "unreadable")

    def test_write_failure_is_disclosed_without_hiding_diagnostics(self):
        b = self.bundle()
        c = bzlog.Container(bundle=b)
        with patch.object(b, "_blob", side_effect=OSError("disk full")):
            c.consume(result("FAILED", log="real failure"))
        c.consume(summary("FAILED"))
        report = c.finish(0)
        self.assertEqual(report["summary"]["tests_failed"], 1)
        self.assertEqual(c.stream.skipped_events, 0)
        self.assertEqual(b.manifest["artifacts"][0]["status"], "write_error")
        b.finalize(report)
        self.assertEqual(report["bundle"]["capture_status"], "partial")

    def test_archive_failure_does_not_hide_usable_test_summary(self):
        b = self.bundle()
        c = bzlog.Container(bundle=b)
        with patch.object(b, "capture_event", side_effect=ValueError("invalid file reference")):
            c.consume(summary("FAILED"))
        self.assertEqual(c.finish(0)["summary"]["tests_failed"], 1)
        self.assertEqual(c.stream.skipped_events, 0)
        self.assertTrue(b.manifest["errors"])

    def test_report_and_evidence_write_failure_keep_inline_evidence(self):
        b = self.bundle()
        c = bzlog.Container(bundle=b)
        c.consume(result("FAILED"))
        c.consume(summary("FAILED"))
        report = c.finish(0)
        original = b._write_json
        def fail_reports(name, value):
            if name != "manifest.json":
                raise OSError("disk full")
            return original(name, value)
        with patch.object(b, "_write_json", side_effect=fail_reports), redirect_stderr(io.StringIO()):
            b.finalize(report)
        self.assertFalse(b.evidence_saved)
        self.assertTrue(report["evidence"])
        self.assertEqual(report["bundle"]["capture_status"], "partial")

    def test_analysis_invocation_evidence_survives_bundle_and_none_output(self):
        bep = self.root / "analysis.jsonl"
        context = "ERROR: /workspace/repo.bzl:2:9: fetch failed\nTraceback:\nNo space left on device"
        bep.write_text("\n".join(json.dumps(event) for event in [
            {"id": {"configuredLabel": {"label": "//dep"}},
             "aborted": {"reason": "ANALYSIS_FAILURE", "description": "RepositoryFunctionException"}},
            {"progress": {"stderr": context}},
        ]) + "\n")
        code, stdout, stderr = self.invoke("--bundle", str(self.destination), "--evidence", "none", str(bep))
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("invocation_evidence", json.loads(stdout)["diagnostics"][0])
        saved = json.loads((self.destination / "report.json").read_text())
        ref = saved["diagnostics"][0]["invocation_evidence"]
        self.assertEqual(saved["evidence"][ref]["text"], context)
        code, excerpt, stderr = self.invoke("show", str(self.destination), ref)
        self.assertEqual(code, 0, stderr)
        self.assertIn(context, excerpt)
        code, _, stderr = self.invoke("verify", str(self.destination))
        self.assertEqual(code, 0, stderr)

    def test_reused_log_path_preserves_each_attempt_and_diagnostic_snapshot(self):
        b = self.bundle()
        c = bzlog.Container(bundle=b)
        source = self.root / "test.log"
        first = b"first attempt failure\n" * 500
        for attempt, data in ((1, first), (2, b"second attempt failure\n")):
            source.write_bytes(data)
            ev = result("FAILED", attempt=attempt)
            ev["testResult"]["testActionOutput"] = [{"name": "test.log", "uri": source.as_uri()}]
            c.consume(ev)
            c.consume(ev)  # Duplicate BEP events must not create new snapshots.
        source.unlink()
        c.consume(summary("FAILED"))
        report = c.finish(0)
        b.finalize(report)
        logs = [a for a in b.manifest["artifacts"] if a["kind"] == "test.log"]
        self.assertEqual(len(logs), 2)
        self.assertNotEqual(logs[0]["path"], logs[1]["path"])
        self.assertEqual((b.root / logs[0]["path"]).read_bytes(), first)
        self.assertEqual(logs[0]["event_id"]["testResult"]["attempt"], 1)
        self.assertIn("first attempt failure", json.dumps(report["evidence"]))

    def test_file_changed_during_copy_is_not_claimed_complete(self):
        b = self.bundle()
        source = self.root / "changing.log"
        source.write_bytes(b"before")
        original = b._blob
        def change(stream, limit=None):
            saved = original(stream, limit)
            source.write_bytes(b"after mutation")
            return saved
        with patch.object(b, "_blob", side_effect=change):
            artifact = b.capture_file(str(source), "test.log")
        self.assertEqual(artifact["status"], "changed_during_copy")
        self.assertEqual((b.root / artifact["path"]).read_bytes(), b"before")

    def test_posthoc_bundle_preserves_raw_bep_and_moves_without_original_logs(self):
        source = self.root / "source.log"
        source.write_text("a useful failure\n" * 500)
        ev = result("FAILED")
        ev["testResult"]["testActionOutput"] = [{"name": "test.log", "uri": source.as_uri()}]
        events = [ev, summary("FAILED"), {"finished": {"exitCode": {"code": 3}}, "lastMessage": True}]
        bep = self.root / "input.json"
        raw = "\n".join(json.dumps(event) for event in events) + "\n"
        bep.write_text(raw)
        code, stdout, stderr = self.invoke("--bundle", str(self.destination), "--evidence", "none", str(bep))
        self.assertEqual(code, 0, stderr)
        self.assertNotIn("evidence", json.loads(stdout))
        manifest = json.loads((self.destination / "manifest.json").read_text())
        capture = next(a for a in manifest["artifacts"] if a["kind"] == "bep_json")
        self.assertEqual((self.destination / capture["path"]).read_text(), raw)
        moved = self.root / "moved bundle"
        shutil.move(str(self.destination), moved)
        source.unlink()
        bep.unlink()
        saved = json.loads((moved / "report.json").read_text())
        self.assertEqual(saved["bundle"]["manifest"], "manifest.json")
        self.assertNotIn("evidence_sidecar", saved)
        ref = saved["diagnostics"][0]["evidence"]
        self.assertIn(ref, saved["evidence"])
        code, stdout, stderr = self.invoke("show", str(moved), ref)
        self.assertEqual(code, 0, stderr)
        self.assertIn("useful failure", stdout)
        code, stdout, stderr = self.invoke("verify", str(moved))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["integrity"], "ok")

    def test_verification_detects_tampering(self):
        b = self.bundle()
        a = b.capture_stream(io.BytesIO(b"original"), "test.log")
        b.finalize(bzlog.Container().finish(0))
        (b.root / a["path"]).write_bytes(b"modified")
        code, stdout, _ = self.invoke("verify", str(b.root))
        self.assertEqual(code, 2)
        self.assertIn("mismatch", stdout)

    def test_verification_rejects_escape_and_saved_artifact_without_path(self):
        b = self.bundle()
        b.finalize(bzlog.Container().finish(0))
        for artifact in ({"id": "bad", "status": "saved", "path": "../outside"},
                         {"id": "bad", "status": "saved"}):
            manifest = dict(b.manifest, artifacts=[artifact])
            (b.root / "manifest.json").write_text(json.dumps(manifest))
            code, stdout, _ = self.invoke("verify", str(b.root))
            self.assertEqual(code, 2, stdout)

    def test_verification_rejects_collecting_and_malformed_manifest(self):
        b = self.bundle()
        for manifest in (b.manifest, [], {"schema": "future"}):
            (b.root / "manifest.json").write_text(json.dumps(manifest))
            code, stdout, _ = self.invoke("verify", str(b.root))
            self.assertEqual(code, 2, stdout)

    def test_verification_requires_an_artifact_list(self):
        b = self.bundle()
        b.finalize(bzlog.Container().finish(0))
        for artifacts in ("", {}, None):
            manifest = dict(b.manifest, artifacts=artifacts)
            (b.root / "manifest.json").write_text(json.dumps(manifest))
            code, stdout, _ = self.invoke("verify", str(b.root))
            self.assertEqual(code, 2, stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX special files")
    def test_verification_rejects_fifo_manifest_without_blocking(self):
        b = self.bundle()
        (b.root / "manifest.json").unlink()
        os.mkfifo(b.root / "manifest.json")
        proc = subprocess.run([sys.executable, bzlog.__file__, "verify", str(b.root)],
                              capture_output=True, text=True, timeout=3)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["integrity"], "failed")

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_verification_rejects_symlink_loops_without_traceback(self):
        b = self.bundle()
        (b.root / "manifest.json").unlink()
        (b.root / "manifest.json").symlink_to("manifest.json")
        code, stdout, _ = self.invoke("verify", str(b.root))
        self.assertEqual(code, 2, stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX symlinks")
    def test_verification_rejects_symlink_escape_and_fifo(self):
        b = self.bundle()
        b.finalize(bzlog.Container().finish(0))
        outside = self.root / "outside.txt"
        outside.write_text("do not follow")
        (b.root / "escape").symlink_to(outside)
        os.mkfifo(b.root / "fifo")
        for name in ("escape", "fifo"):
            manifest = dict(b.manifest, artifacts=[{"id": "bad", "status": "saved", "path": name}])
            (b.root / "manifest.json").write_text(json.dumps(manifest))
            code, stdout, _ = self.invoke("verify", str(b.root))
            self.assertEqual(code, 2, stdout)

    def test_partial_capture_can_have_valid_saved_bytes(self):
        b = self.bundle()
        b.capture_reference({"uri": "bytestream://cache/blob"}, "test.log", None)
        b.finalize(bzlog.Container().finish(0))
        code, stdout, _ = self.invoke("verify", str(b.root))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["capture_status"], "partial")

    def test_profile_is_deferred_until_after_events(self):
        b = self.bundle()
        source = self.root / "profile.gz"
        ev = {"id": {"buildToolLogs": {}}, "buildToolLogs": {"log": [
            {"name": "command.profile.gz", "uri": source.as_uri()}]}}
        b.capture_event(ev, None)
        self.assertEqual(b.manifest["artifacts"], [])
        source.write_bytes(b"\x1f\x8bprofile content")
        b.finalize(bzlog.Container().finish(0))
        profile = next(a for a in b.manifest["artifacts"] if a["kind"] == "profile")
        self.assertEqual(profile["status"], "saved")
        self.assertTrue(profile["path"].endswith(".gz"))

    def test_companion_requires_effective_metadata_and_a_fresh_file(self):
        b = self.bundle()
        stale, fresh = self.root / "stale.bep", self.root / "fresh.bep"
        stale.write_bytes(b"old invocation")
        b.prepare_companions(["--build_event_binary_file=" + str(stale), "--profile=" + str(fresh)])
        b.capture_event({"structuredCommandLine": {"commandLineLabel": "canonical", "sections": [
            {"sectionLabel": "command options", "optionList": {"option": [
                {"optionName": "build_event_binary_file", "optionValue": str(stale)},
                {"optionName": "profile", "optionValue": str(fresh)}]}}]}}, None)
        fresh.write_bytes(b"new invocation")
        b.finish_captures()
        self.assertEqual([a["status"] for a in b.manifest["artifacts"]], ["not_produced", "saved"])

    def test_flag_looking_test_argument_is_never_copied(self):
        b = self.bundle()
        source = self.root / "unrelated"
        source.write_text("unrelated data")
        b.prepare_companions(["--test_arg", "--profile=" + str(source)])
        source.write_text("changed unrelated data")
        b.finish_captures()
        self.assertEqual(b.manifest["artifacts"], [])

    def test_manifest_save_failure_is_reported_and_initial_state_remains(self):
        b = self.bundle()
        report = bzlog.Container().finish(0)
        original = b._write_json
        def fail_manifest(name, value):
            if name == "manifest.json":
                raise OSError("disk full")
            return original(name, value)
        with patch.object(b, "_write_json", side_effect=fail_manifest), redirect_stderr(io.StringIO()):
            b.finalize(report)
        self.assertFalse(report["bundle"]["manifest_saved"])
        self.assertEqual(json.loads((b.root / "manifest.json").read_text())["state"], "collecting")


@unittest.skipUnless(os.name == "posix", "POSIX fake executable")
class BundleWrapperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bzlog-bundle-wrapper-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "capture"
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"], TMPDIR=str(self.root))
        self.script = str(Path(__file__).with_name("bzlog.py"))

    def fake(self, body):
        executable = self.root / "bazel"
        executable.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n" + body)
        executable.chmod(0o755)

    def run_cli(self, *args):
        p = subprocess.run([sys.executable, self.script, "--bundle", str(self.bundle), *args],
                           cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        return p, json.loads(p.stdout) if p.stdout.strip() else None

    def test_wrapper_retains_temporary_bep_full_console_exit_and_durable_sidecar(self):
        events = [result("FAILED", log="kept failure"), summary("FAILED"),
                  {"finished": {"exitCode": {"code": 3}}, "lastMessage": True}]
        self.fake("path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--build_event_json_file='))\n"
                  f"Path(path).write_text('\\n'.join(json.dumps(e) for e in {events!r}) + '\\n')\n"
                  "print('whole console line\\n' * 10000)\nsys.exit(3)\n")
        p, report = self.run_cli("test", "//tests:one")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(Path(report["evidence_sidecar"]), self.bundle / "evidence.json")
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        self.assertEqual(manifest["capture_status"], "complete")
        console = next(a for a in manifest["artifacts"] if a["kind"] == "console")
        self.assertGreater(console["bytes"], 65536)
        self.assertNotIn(b"omitted", (self.bundle / console["path"]).read_bytes())
        self.assertTrue(any(a["kind"] == "bep_json" and a["status"] == "saved" for a in manifest["artifacts"]))
        self.assertEqual(list(self.root.glob("bzlog-*.json")), [])

    def test_startup_failure_does_not_archive_stale_bep(self):
        old = self.root / "old.json"
        old.write_text('{"finished":{"exitCode":{}},"lastMessage":true}\n')
        self.fake("print('startup failed', file=sys.stderr)\nsys.exit(2)\n")
        p, report = self.run_cli("test", "//tests:one", "--build_event_json_file=" + str(old))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(report["invocation"]["stream"]["status"], "unavailable")
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        self.assertEqual(next(a for a in manifest["artifacts"] if a["kind"] == "bep_json")["status"], "not_produced")
        self.assertEqual(report["bundle"]["capture_status"], "partial")

    def test_existing_bundle_refused_before_bazel_runs(self):
        self.bundle.mkdir()
        self.fake("Path('launched').touch()\n")
        p, _ = self.run_cli("test", "//tests:one")
        self.assertEqual(p.returncode, 2)
        self.assertFalse((self.root / "launched").exists())

    def test_missing_executable_still_finalizes_partial_bundle(self):
        self.env["PATH"] = str(self.root)
        p, report = self.run_cli("test", "//tests:one")
        self.assertEqual(p.returncode, 127, p.stderr)
        self.assertTrue(report["bundle"]["manifest_saved"])
        self.assertEqual(report["bundle"]["capture_status"], "partial")

    def test_invalid_deferred_reference_preserves_report_and_bazel_exit(self):
        events = [{"finished": {"exitCode": {"code": 3}}},
                  {"id": {"buildToolLogs": {}}, "buildToolLogs": {"log": [
                      {"name": "profile", "uri": "file://[invalid"},
                      {"name": "valid", "contents": "dmFsaWQ="}]}, "lastMessage": True}]
        self.fake("path = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--build_event_json_file='))\n"
                  f"Path(path).write_text('\\n'.join(json.dumps(e) for e in {events!r}) + '\\n')\n"
                  "sys.exit(3)\n")
        p, report = self.run_cli("test", "//tests:one")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(report["invocation"]["exit"]["code"], 3)
        self.assertEqual(report["bundle"]["capture_status"], "partial")
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        logs = [a for a in manifest["artifacts"] if a["kind"] == "build_tool_log"]
        self.assertEqual([a["status"] for a in logs], ["invalid_uri", "saved"])


if __name__ == "__main__":
    unittest.main()
