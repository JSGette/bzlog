"""Configured test outcomes, retries, runs, shards, and cached-result regressions."""

import base64
import itertools
import unittest

import bzlog


def result(status, label="//tests:one", configuration="cfg", run=1, shard=1,
           attempt=1, log="assertion failed", cached_locally=False, cached_remotely=False):
    return {
        "id": {"testResult": {"label": label, "configuration": {"id": configuration},
                              "run": run, "shard": shard, "attempt": attempt}},
        "testResult": {"status": status, "cachedLocally": cached_locally,
                       "executionInfo": {"cachedRemotely": cached_remotely},
                       "testActionOutput": ([] if log is None else [
                           {"name": "test.log", "contents": base64.b64encode(log.encode()).decode()}])},
    }


def summary(status, label="//tests:one", configuration="cfg"):
    return {"id": {"testSummary": {"label": label, "configuration": {"id": configuration}}},
            "testSummary": {"overallStatus": status}}


def report(*events):
    c = bzlog.Container()
    for event in events:
        c.handle(event)
    return c.finish(0)


class TestResultsTest(unittest.TestCase):
    def test_flaky_retry_has_evidence_but_is_not_a_final_failure(self):
        events = [result("FAILED"), result("PASSED", attempt=2), summary("FLAKY")]
        for order in itertools.permutations(events):
            with self.subTest(order=[list(e)[0] for e in order]):
                output = report(*order)
                self.assertEqual(output["summary"], {"tests_total": 1, "tests_flaky": 1})
                d = output["diagnostics"][0]
                self.assertEqual(d["kind"], "test_flaky")
                self.assertEqual(d["status"], "FAILED")
                self.assertEqual(d["overall_status"], "FLAKY")
                self.assertTrue(d["final"])
                self.assertIn("assertion failed", output["evidence"][d["evidence"]]["text"])
                self.assertIn("test status: FLAKY", bzlog.render_text(output))

    def test_passed_summary_is_authoritative_over_earlier_failure(self):
        output = report(result("FAILED"), summary("PASSED"))
        self.assertEqual(output["summary"], {"tests_total": 1, "tests_passed": 1})
        self.assertEqual(output["diagnostics"][0]["kind"], "test_recovered")

    def test_passing_test_has_no_diagnostic(self):
        output = report(result("PASSED"), summary("PASSED"))
        self.assertEqual(output["diagnostics"], [])
        self.assertEqual(output["evidence"], {})
        self.assertEqual(output["summary"], {"tests_total": 1, "tests_passed": 1})

    def test_configuration_is_part_of_test_identity(self):
        output = report(result("FAILED", configuration="linux"), summary("FAILED", configuration="linux"),
                        result("PASSED", configuration="mac"), summary("PASSED", configuration="mac"))
        self.assertEqual(output["summary"], {"tests_total": 2, "tests_passed": 1, "tests_failed": 1})
        self.assertEqual(output["diagnostics"][0]["configuration"], {"id": "linux"})

    def test_duplicate_results_and_summaries_do_not_inflate_counts(self):
        events = [result("FAILED"), summary("FAILED")]
        output = report(*(events + events))
        self.assertEqual(output["summary"], {"tests_total": 1, "tests_failed": 1})
        self.assertEqual(output["diagnostics"][0]["occurrences"], 1)

    def test_runs_shards_and_attempts_are_retained_when_grouped(self):
        events = [result("FAILED", run=run, shard=shard, attempt=attempt)
                  for run, shard, attempt in itertools.product((1, 2), repeat=3)]
        output = report(*events, summary("FAILED"))
        self.assertEqual(len(output["diagnostics"]), 1)
        d = output["diagnostics"][0]
        self.assertEqual(d["occurrences"], 8)
        self.assertNotIn("affected_count", d)
        self.assertEqual({(a["run"], a["shard"], a["attempt"]) for a in d["attempts"]},
                         set(itertools.product((1, 2), repeat=3)))

    def test_attempt_listing_is_bounded_with_an_omission_count(self):
        count = bzlog.MAX_TEST_ATTEMPTS + 3
        output = report(*(result("FAILED", attempt=a) for a in range(1, count + 1)), summary("FAILED"))
        d = output["diagnostics"][0]
        self.assertEqual(d["occurrences"], count)
        self.assertEqual(len(d["attempts"]), bzlog.MAX_TEST_ATTEMPTS)
        self.assertEqual(d["attempts_omitted"], 3)

    def test_identical_logs_do_not_merge_different_final_outcomes(self):
        output = report(result("FAILED", label="//a"), summary("FLAKY", label="//a"),
                        result("FAILED", label="//b"), summary("FAILED", label="//b"))
        self.assertEqual({d["kind"] for d in output["diagnostics"]}, {"test_flaky", "test_failure"})
        self.assertEqual(output["summary"], {"tests_total": 2, "tests_flaky": 1, "tests_failed": 1})

    def test_identical_failures_on_distinct_targets_can_still_group(self):
        output = report(result("FAILED", label="//a"), summary("FAILED", label="//a"),
                        result("FAILED", label="//b"), summary("FAILED", label="//b"))
        self.assertEqual(len(output["diagnostics"]), 1)
        d = output["diagnostics"][0]
        self.assertEqual((d["occurrences"], d["affected_count"]), (2, 2))
        self.assertEqual({a["target"] for a in d["attempts"]}, {"//a", "//b"})

    def test_missing_logs_do_not_merge_unrelated_tests(self):
        output = report(result("FAILED", label="//a", log=None), summary("FAILED", label="//a"),
                        result("FAILED", label="//b", log=None), summary("FAILED", label="//b"))
        self.assertEqual(len(output["diagnostics"]), 2)

    def test_cached_attempt_metadata_is_retained(self):
        output = report(result("FAILED", attempt=1, cached_locally=True),
                        result("FAILED", attempt=2, cached_remotely=True), summary("FAILED"))
        attempts = output["diagnostics"][0]["attempts"]
        self.assertTrue(attempts[0]["cached_locally"])
        self.assertFalse(attempts[0]["cached_remotely"])
        self.assertTrue(attempts[1]["cached_remotely"])
        text = bzlog.render_text(output)
        self.assertIn("[local cache]", text)
        self.assertIn("[remote cache]", text)

    def test_missing_summary_never_implies_final_success_or_failure(self):
        for status, kind in [("PASSED", "test_incomplete"), ("FAILED", "test_attempt_failure")]:
            with self.subTest(status=status):
                output = report(result(status))
                self.assertEqual(output["summary"], {"tests_total": 1, "tests_incomplete": 1})
                d = output["diagnostics"][0]
                self.assertEqual(d["kind"], kind)
                self.assertFalse(d["final"])
                self.assertEqual(d["overall_status"], "UNKNOWN")

    def test_all_final_statuses_are_classified(self):
        for status, bucket in [
            ("PASSED", "tests_passed"), ("FLAKY", "tests_flaky"),
            ("FAILED", "tests_failed"), ("TIMEOUT", "tests_failed"),
            ("REMOTE_FAILURE", "tests_failed"), ("FAILED_TO_BUILD", "tests_failed"),
            ("INCOMPLETE", "tests_incomplete"), ("NO_STATUS", "tests_incomplete"),
            ("TOOL_HALTED_BEFORE_TESTING", "tests_incomplete"), ("FUTURE_STATUS", "tests_incomplete"),
        ]:
            with self.subTest(status=status):
                output = report(summary(status))
                self.assertEqual(output["summary"], {"tests_total": 1, bucket: 1})
                if status != "PASSED":
                    self.assertEqual(output["diagnostics"][0]["overall_status"], status)

    def test_summary_only_failure_retains_referenced_log(self):
        event = summary("FAILED")
        event["testSummary"]["failed"] = [{"contents": base64.b64encode(b"cached test failure").decode()}]
        output = report(event)
        d = output["diagnostics"][0]
        self.assertEqual(d["kind"], "test_failure")
        self.assertEqual(output["evidence"][d["evidence"]]["text"], "cached test failure")

    def test_finishing_does_not_mutate_attempts(self):
        c = bzlog.Container()
        for event in [result("FAILED"), result("PASSED", attempt=2), summary("FLAKY")]:
            c.handle(event)
        self.assertEqual(c.finish(0), c.finish(0))

    def test_target_summary_covers_tests_that_failed_to_build(self):
        output = report({"id": {"targetSummary": {"label": "//tests:one", "configuration": {"id": "cfg"}}},
                         "targetSummary": {"overallTestStatus": "FAILED_TO_BUILD"}})
        self.assertEqual(output["summary"], {"tests_total": 1, "tests_failed": 1})
        self.assertEqual(output["diagnostics"][0]["overall_status"], "FAILED_TO_BUILD")
        self.assertTrue(output["diagnostics"][0]["final"])

    def test_non_test_target_summary_does_not_count_as_a_test(self):
        for payload in ({}, {"overallTestStatus": "NO_STATUS"}):
            output = report({"id": {"targetSummary": {"label": "//lib"}}, "targetSummary": payload})
            self.assertEqual(output["summary"], {})
            self.assertEqual(output["diagnostics"], [])

    def test_test_summary_takes_precedence_over_target_summary(self):
        fallback = {"id": {"targetSummary": {"label": "//tests:one", "configuration": {"id": "cfg"}}},
                    "targetSummary": {"overallTestStatus": "FAILED"}}
        for events in itertools.permutations([fallback, summary("PASSED")]):
            output = report(*events)
            self.assertEqual(output["summary"], {"tests_total": 1, "tests_passed": 1})
            self.assertEqual(output["diagnostics"], [])


if __name__ == "__main__":
    unittest.main()
