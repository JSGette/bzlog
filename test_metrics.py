"""Cache metrics use spawn outcomes, not mixed action and spawn totals."""

import unittest

import bzlog


def metrics(counts, kinds=None, action_cache=None):
    kinds = kinds or {}
    action_summary = {"runnerCount": [{"name": name, "count": count,
                                       "execKind": kinds.get(name, "")}
                                      for name, count in counts.items()]}
    if action_cache is not None:
        action_summary["actionCacheStatistics"] = action_cache
    return bzlog.Container._metrics({"actionSummary": action_summary})


class MetricsTest(unittest.TestCase):
    def test_90_hits_and_10_executions_report_90_percent(self):
        m = metrics({"total": 100, "remote cache hit": 90, "remote": 10})
        self.assertEqual(m["spawn_cache"]["hit_rate"], 0.9)
        self.assertEqual(m["spawn_cache"]["outcomes"], 100)
        self.assertNotIn("remote_cache_hit_rate", m)

    def test_internal_actions_and_action_cache_hits_are_excluded(self):
        m = metrics({"total": 150, "internal": 50, "action cache hit": 1000,
                     "remote cache hit": 90, "remote": 10})
        self.assertEqual(m["spawn_cache"]["hit_rate"], 0.9)
        self.assertEqual(m["spawn_cache"]["outcomes"], 100)
        self.assertEqual(m["runners"]["action cache hit"], 1000)

    def test_disk_and_remote_cache_hits_have_separate_counts(self):
        cache = metrics({"total": 100, "remote cache hit": 20, "disk cache hit": 30,
                         "linux-sandbox": 40, "remote": 10})["spawn_cache"]
        self.assertEqual(cache, {"remote_hits": 20, "disk_hits": 30, "other_hits": 0,
                                 "executions": 50, "remote_executions": 10,
                                 "outcomes": 100, "hit_rate": 0.5})

    def test_spawn_count_can_exceed_action_total(self):
        cache = metrics({"total": 50, "remote cache hit": 90, "worker": 10})["spawn_cache"]
        self.assertEqual(cache["hit_rate"], 0.9)
        self.assertEqual(cache["outcomes"], 100)

    def test_no_spawn_outcomes_means_no_rate(self):
        for counts in ({}, {"total": 0}, {"total": 10, "internal": 10, "action cache hit": 100}):
            with self.subTest(counts=counts):
                self.assertNotIn("spawn_cache", metrics(counts))

    def test_new_cache_runner_name_is_not_mislabeled_remote(self):
        cache = metrics({"custom cache hit": 8, "worker": 2})["spawn_cache"]
        self.assertEqual(cache["other_hits"], 8)
        self.assertEqual(cache["remote_hits"], 0)
        self.assertEqual(cache["hit_rate"], 0.8)

    def test_remote_execution_kind_supports_custom_runner_names(self):
        m = metrics({"custom-executor": 10}, {"custom-executor": "Remote"})
        self.assertEqual(m["spawn_cache"]["remote_executions"], 10)
        invocation = {"metrics": m}
        bzlog.prune_metrics(invocation)
        self.assertEqual(invocation["metrics"]["spawn_cache"]["hit_rate"], 0)

    def test_pure_local_execution_does_not_raise_cache_anomaly(self):
        m = metrics({"total": 100, "linux-sandbox": 100})
        self.assertEqual(m["spawn_cache"]["hit_rate"], 0)
        invocation = {"metrics": m}
        bzlog.prune_metrics(invocation)
        self.assertNotIn("metrics", invocation)

    def test_healthy_rate_is_pruned_but_low_observed_rate_is_kept(self):
        for hits, kept in [(90, False), (70, False), (40, True), (0, True)]:
            with self.subTest(hits=hits):
                invocation = {"metrics": metrics({"total": 100, "remote cache hit": hits,
                                                   "remote": 100 - hits})}
                bzlog.prune_metrics(invocation)
                self.assertEqual("metrics" in invocation, kept)
                if kept:
                    self.assertIn("runners", invocation["metrics"])

    def test_local_action_cache_statistics_remain_separate(self):
        m = metrics({"remote cache hit": 5, "remote": 5},
                    action_cache={"hits": 90, "misses": 10})
        self.assertEqual(m["spawn_cache"]["hit_rate"], 0.5)
        self.assertEqual(m["action_cache"]["hit_rate"], 0.9)

    def test_digest_changes_are_retained_without_an_automatic_anomaly(self):
        m = metrics({}, action_cache={"hits": 0, "misses": 1000,
                    "missDetails": [{"reason": "DIGEST_MISMATCH", "count": 1000}]})
        self.assertEqual(m["action_cache"]["miss_reasons"]["DIGEST_MISMATCH"], 1000)
        invocation = {"metrics": m}
        bzlog.prune_metrics(invocation)
        self.assertNotIn("metrics", invocation)

    def test_slow_build_timing_still_survives_pruning(self):
        invocation = {"metrics": {"timing": {"wall_ms": 120001}}}
        bzlog.prune_metrics(invocation)
        self.assertEqual(invocation["metrics"]["timing"]["wall_ms"], 120001)


if __name__ == "__main__":
    unittest.main()
