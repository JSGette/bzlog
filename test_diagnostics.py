"""Failure retention and causal grouping regressions.

Run with python3 test_diagnostics.py or python3 -m unittest discover.
"""

import base64
import itertools
import unittest

import bzlog


def abort(eid, description="", children=()):
    return {"id": eid, "children": list(children),
            "aborted": {"reason": "ANALYSIS_FAILURE", "description": description}}


def action(label, text=None, configuration=None):
    payload = {"type": "CppCompile", "exitCode": 1}
    if text is not None:
        payload["stderr"] = {"contents": base64.b64encode(text.encode()).decode()}
    eid = {"label": label}
    if configuration:
        eid["configuration"] = {"id": configuration}
    return {"id": {"actionCompleted": eid}, "action": payload}


def report(*events):
    container = bzlog.Container()
    for event in events:
        container.handle(event)
    return container.finish(0)


class DiagnosticTest(unittest.TestCase):
    def test_analysis_cause_survives_every_event_order(self):
        cause = {"configuredLabel": {"label": "//dep:dep", "configuration": {"id": "cfg"}}}
        parents = [{"targetConfigured": {"label": label}} for label in ("//a:a", "//b:b")]
        events = [abort(parent, children=[cause]) for parent in parents]
        events.append(abort(cause, "No space left on device"))
        for ordered in itertools.permutations(events):
            with self.subTest(order=[event["id"] for event in ordered]):
                result = report(*ordered)
                self.assertEqual(len(result["diagnostics"]), 1)
                diagnostic = result["diagnostics"][0]
                self.assertEqual(diagnostic["description"], "No space left on device")
                self.assertEqual(diagnostic["affected_count"], 3)
                self.assertEqual(set(diagnostic["also_affects"]), {"//a:a", "//b:b"})
                self.assertIn("No space left on device", bzlog.render_text(result))

    def test_indirect_cause_and_cycle_do_not_lose_message(self):
        parent = {"targetConfigured": {"label": "//parent"}}
        intermediate = {"configuredLabel": {"label": "//middle"}}
        cause = {"configuredLabel": {"label": "//cause"}}
        result = report(abort(parent, children=[intermediate]),
                        abort(intermediate, children=[parent, cause]),
                        abort(cause, "visibility violation"))
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(result["diagnostics"][0]["affected_count"], 3)

    def test_unresolved_cycle_keeps_diagnostics(self):
        a, b = {"configuredLabel": {"label": "//a"}}, {"configuredLabel": {"label": "//b"}}
        result = report(abort(a, children=[b]), abort(b, children=[a]))
        self.assertEqual(len(result["diagnostics"]), 2)
        self.assertTrue(all(d["description"] for d in result["diagnostics"]))

    def test_distinct_analysis_errors_do_not_merge(self):
        result = report(abort({"configuredLabel": {"label": "//a"}}, "disk full"),
                        abort({"configuredLabel": {"label": "//b"}}, "visibility violation"))
        self.assertEqual(len(result["diagnostics"]), 2)
        text = bzlog.render_text(result)
        self.assertIn("disk full", text)
        self.assertIn("visibility violation", text)

    def test_pattern_metadata_does_not_swallow_loading_failure(self):
        result = report({"id": {"pattern": {"pattern": ["//missing/..."]}},
                         "aborted": {"reason": "LOADING_FAILURE", "description": "BUILD file not found"}})
        self.assertEqual(result["invocation"]["patterns"], ["//missing/..."])
        self.assertIn("BUILD file not found", bzlog.render_text(result))

    def test_missing_evidence_does_not_merge_actions(self):
        result = report(action("//a"), action("//b"))
        self.assertEqual(len(result["diagnostics"]), 2)
        self.assertTrue(all(d["description"] for d in result["diagnostics"]))

    def test_events_without_ids_remain_distinct(self):
        result = report(abort({}, "disk full"), abort({}, "visibility violation"))
        self.assertEqual(len(result["diagnostics"]), 2)

    def test_identical_compiler_diagnostics_still_collapse(self):
        text = "include/common.h:7:2: error: missing symbol"
        result = report(action("//a", text), action("//b", text))
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(result["diagnostics"][0]["affected_count"], 2)

    def test_configurations_do_not_collapse(self):
        text = "include/common.h:7:2: error: missing symbol"
        result = report(action("//a", text, "host"), action("//a", text, "target"))
        self.assertEqual(len(result["diagnostics"]), 2)

    def test_identical_tails_do_not_merge_different_logs(self):
        suffix = "\n".join("teardown noise" for _ in range(50))
        result = report(action("//a", "disk full\n" + suffix),
                        action("//b", "permission denied\n" + suffix))
        self.assertEqual(len(result["diagnostics"]), 2)
        self.assertEqual(len(result["evidence"]), 2)

    def test_progress_retains_early_error_before_warning_fanout(self):
        result = report(abort({"targetConfigured": {"label": "//a"}}),
                        {"progress": {"stderr": "ERROR: repository fetch failed\nTraceback:\nNo space left "}},
                        {"progress": {"stderr": "on device\n" + "WARNING: target skipped\n" * 100}})
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["evidence_scope"], "invocation")
        text = bzlog.render_text(result)
        self.assertIn("No space left on device", text)
        self.assertIn("not attributed to this target", text)

    def test_empty_analysis_recovers_console_traceback_before_warning_fanout(self):
        traceback = ("ERROR: /workspace/toolchain.bzl:9:29: repository fetch failed\n"
                     "   Traceback (most recent call last):\n"
                     "        File \"/workspace/toolchain.bzl\", line 9, column 29\n"
                     "                ctx.download_and_extract(\n"
                     "Error in download_and_extract: No space left on device")
        c = bzlog.Container()
        c.handle(abort({"targetConfigured": {"label": "//a"}}))
        c.console_output = ("WARNING: startup warning\n" * 50 + traceback + "\n" +
                            "WARNING: errors encountered while analyzing target\n" * 53)
        result = c.finish(0)
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["description"], traceback)
        self.assertEqual(diagnostic["description_scope"], "invocation")
        self.assertEqual(diagnostic["evidence_scope"], "invocation")
        evidence = result["evidence"][diagnostic["evidence"]]
        self.assertIn(traceback, evidence["text"])
        self.assertTrue(evidence["truncated"])

    def test_empty_analysis_uses_progress_traceback_not_generic_failure_detail(self):
        result = report(abort({"targetConfigured": {"label": "//a"}}),
                        {"progress": {"stderr": "ERROR: /workspace/toolchain.bzl:9:29: fetch failed\n"}},
                        {"progress": {"stderr": "Traceback:\nNo space left on device\n"}},
                        {"finished": {"exitCode": {"code": 1},
                                      "failureDetail": {"message": "not all targets were analyzed"}}})
        diagnostic = result["diagnostics"][0]
        self.assertIn("No space left on device", diagnostic["description"])
        self.assertIn("/workspace/toolchain.bzl:9:29", diagnostic["description"])
        self.assertEqual(diagnostic["description_scope"], "invocation")

    def test_explicit_analysis_message_is_not_replaced_by_unrelated_console_error(self):
        c = bzlog.Container()
        c.handle(abort({"configuredLabel": {"label": "//a"}}, "visibility violation"))
        c.console_output = "ERROR: unrelated failure"
        diagnostic = c.finish(0)["diagnostics"][0]
        self.assertEqual(diagnostic["description"], "visibility violation")
        self.assertNotIn("description_scope", diagnostic)

    def test_repository_exception_keeps_direct_and_invocation_evidence(self):
        description = "RepositoryFunctionException: No space left on device"
        context = ("ERROR: /workspace/toolchain.bzl:9:29: repository fetch failed\n"
                   "Traceback:\nNo space left on device")
        result = report(abort({"configuredLabel": {"label": "//a"}}, description),
                        {"progress": {"stderr": context}})
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["description"], description)
        direct = result["evidence"][diagnostic["evidence"]]
        invocation = result["evidence"][diagnostic["invocation_evidence"]]
        self.assertEqual(direct["text"], description)
        self.assertEqual(invocation["text"], context)
        self.assertEqual(invocation["kind"], "invocation_log")
        self.assertIn("not attributed to this target", bzlog.render_text(result))

    def test_long_analysis_traceback_keeps_origin_and_terminal_cause(self):
        context = ("ERROR: /workspace/toolchain.bzl:9:29: repository fetch failed\n"
                   "Traceback:\n" + "    nested stack frame\n" * 120 +
                   "No space left on device\n" + "WARNING: skipped target\n" * 53)
        result = report(abort({"configuredLabel": {"label": "//a"}}),
                        {"progress": {"stderr": context}})
        diagnostic = result["diagnostics"][0]
        for text in (diagnostic["description"], result["evidence"][diagnostic["evidence"]]["text"]):
            self.assertIn("/workspace/toolchain.bzl:9:29", text)
            self.assertIn("No space left on device", text)
            self.assertIn("omitted", text)

    def test_failure_detail_without_action_is_reported(self):
        result = report({"finished": {"exitCode": {"code": 1},
                         "failureDetail": {"message": "repository fetch failed"}}})
        self.assertIn("repository fetch failed", bzlog.render_text(result))

    def test_failure_category_code_and_message_are_rendered(self):
        event = action("//a")
        event["action"]["failureDetail"] = {
            "message": "sandbox launch failed", "spawn": {"code": "EXECUTION_FAILED"}}
        text = bzlog.render_text(report(event))
        self.assertIn("sandbox launch failed", text)
        self.assertIn("spawn/EXECUTION_FAILED", text)

    def test_analysis_location_preserves_absolute_path(self):
        result = report(abort({"configuredLabel": {"label": "//a"}},
                              "ERROR: /workspace/toolchain.bzl:9:29: download failed"))
        error = result["diagnostics"][0]["errors"][0]
        self.assertEqual(error["file"], "/workspace/toolchain.bzl")
        self.assertEqual((error["line"], error["col"]), (9, 29))

    def test_finishing_twice_preserves_target_information(self):
        c = bzlog.Container()
        c.handle(abort({"configuredLabel": {"label": "//a"}}, "disk full"))
        self.assertEqual(c.finish(0), c.finish(0))

    def test_progress_buffer_is_bounded_and_keeps_both_ends(self):
        c = bzlog.Container()
        c.handle({"progress": {"stderr": "first error\n" + "x" * 100000}})
        c.handle({"progress": {"stderr": "\nlast error"}})
        self.assertLessEqual(len(c._progress_stderr.encode()), 65536)
        self.assertTrue(c._progress_stderr.startswith("first error"))
        self.assertTrue(c._progress_stderr.endswith("last error"))


if __name__ == "__main__":
    unittest.main()
