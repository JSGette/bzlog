"""Execution evidence must not be inferred from final pass/fail alone."""
import unittest
from pathlib import Path

from bzlog import Container, iter_events, render_text
from test_results import result, summary


def configured(label="//tests:one", configuration="cfg"):
    return {"id": {"targetConfigured": {"label": label}},
            "children": [{"targetCompleted": {"label": label, "configuration": {"id": configuration}}}],
            "configured": {"testSize": "SMALL"}}


def completed(success=False, label="//tests:one", configuration="cfg"):
    return {"id": {"targetCompleted": {"label": label, "configuration": {"id": configuration}}},
            "completed": {"success": success}}


def audit(*events):
    c = Container(audit_tests=True)
    for event in events:
        c.handle(event)
    return c.finish(0)["test_audit"]


def final(status, total=1, cached=0, **kwargs):
    event = summary(status, **kwargs)
    event["testSummary"].update(totalRunCount=total, totalNumCached=cached)
    return event


class AuditTest(unittest.TestCase):
    def test_opt_in_and_existing_summary_unchanged(self):
        c = Container()
        c.handle(configured())
        self.assertNotIn("test_audit", c.finish(0))
        c = Container(audit_tests=True)
        c.handle(configured())
        self.assertEqual(c.finish(0)["summary"], {})

    def test_pass_is_not_execution(self):
        for cache in ("cached_locally", "cached_remotely"):
            with self.subTest(cache=cache):
                row = audit(result("PASSED", **{cache: True}), final("PASSED", cached=1))["tests"][0]
                self.assertEqual(row["execution"], "cached")
                self.assertEqual(row["attempts"][cache], 1)
                self.assertEqual(row["attempts"]["executed"], 0)

    def test_attempts_deduplicate_runs_shards_and_retries(self):
        first = result("FAILED", log="")
        events = [first, first, result("PASSED", attempt=2),
                  result("PASSED", run=2, shard=2, cached_locally=True), final("FLAKY", total=3, cached=1)]
        row = audit(*events)["tests"][0]
        self.assertEqual(row["status"], "FLAKY")
        self.assertEqual(row["execution"], "mixed")
        self.assertEqual(row["attempts"]["observed"], 3)
        self.assertEqual(row["attempts"]["executed"], 2)
        self.assertNotIn("issues", row)

    def test_local_and_remote_cache_flags_do_not_double_count(self):
        row = audit(result("PASSED", cached_locally=True, cached_remotely=True),
                    final("PASSED", cached=1))["tests"][0]
        self.assertEqual(row["attempts"]["cached"], 1)

    def test_missing_summary_keeps_execution_and_unknown_outcome_separate(self):
        row = audit(result("PASSED"))["tests"][0]
        self.assertEqual(row["execution"], "executed")
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertFalse(row["final"])

    def test_configurations_stay_separate_and_event_order_independent(self):
        events = [configured(configuration="a"), configured(configuration="b"),
                  result("PASSED", configuration="a"), final("PASSED", configuration="a"),
                  completed(configuration="b")]
        normal = audit(*events)
        self.assertEqual(normal, audit(*reversed(events)))
        self.assertEqual(normal["counts"]["total"], 2)
        self.assertEqual(normal["counts"]["executed"], 1)
        self.assertEqual(normal["counts"]["not_run"], 1)

    def test_build_success_without_test_results_is_unknown(self):
        row = audit(configured(), completed(True))["tests"][0]
        self.assertEqual(row["execution"], "unknown")
        self.assertFalse(row["final"])

    def test_build_failure_without_test_summary_is_not_run(self):
        row = audit(configured(), completed())["tests"][0]
        self.assertEqual(row["execution"], "not_run")
        self.assertEqual(row["status"], "FAILED_TO_BUILD")

    def test_skipped_is_explicit_but_interrupted_is_unknown(self):
        for reason, expected in (("SKIPPED", "not_run"), ("USER_INTERRUPTED", "unknown")):
            event = completed()
            event.pop("completed")
            event["aborted"] = {"reason": reason}
            self.assertEqual(audit(configured(), event)["tests"][0]["execution"], expected)

    def test_summary_only_and_mismatched_counts_are_disclosed(self):
        self.assertEqual(audit(final("PASSED"))["tests"][0]["execution"], "unknown")
        cached = audit(final("PASSED", total=4, cached=4))["tests"][0]
        self.assertEqual(cached["execution"], "cached")
        self.assertEqual(cached["attempts"]["observed"], 0)
        self.assertIn("attempt_count_mismatch", cached["issues"])
        row = audit(result("PASSED"), final("PASSED", cached=1))["tests"][0]
        self.assertEqual(row["execution"], "unknown")
        self.assertIn("cache_count_mismatch", row["issues"])

    def test_remote_failure_does_not_prove_the_test_started(self):
        row = audit(result("REMOTE_FAILURE", log=""), final("REMOTE_FAILURE"))["tests"][0]
        self.assertEqual(row["execution"], "unknown")

    def test_invalid_or_contradictory_summary_cannot_prove_not_run(self):
        for total, cached in (("broken", 0), (1, 2), (1, 0), (None, 0)):
            with self.subTest(total=total, cached=cached):
                row = audit(final("FAILED_TO_BUILD", total=total, cached=cached))["tests"][0]
                self.assertEqual(row["execution"], "unknown")
                self.assertTrue(row["issues"])

    def test_no_test_and_text_output_explain_scope(self):
        empty = audit()
        self.assertEqual(empty["counts"]["total"], 0)
        self.assertIn("filtered", empty["scope"])
        c = Container(audit_tests=True)
        c.handle(result("PASSED", cached_locally=True))
        self.assertIn("cached", render_text(c.finish(0)))

    def test_real_bazel_shards_and_filtered_targets(self):
        root = Path(__file__).parent / "testdata/bazel-9.3"
        for name, category, attempts in (("audit-repeated-shards", "executed", 8),
                                          ("audit-filtered", "unknown", 0),
                                          ("cpp-cached", "cached", 4)):
            with self.subTest(name=name):
                c = Container(audit_tests=True)
                for event in iter_events(str(root / (name + ".jsonl")), c.stream):
                    c.consume(event)
                r = c.finish(0)
                self.assertEqual(r["invocation"]["stream"]["status"], "complete")
                self.assertEqual(r["test_audit"]["counts"][category], 3)
                self.assertEqual(sum(row["attempts"]["observed"] for row in r["test_audit"]["tests"]), attempts)


if __name__ == "__main__":
    unittest.main()
