"""Replay reduced streams captured against Bazel's actual C++ test targets."""

from pathlib import Path
import unittest

from bzlog import Container, iter_events

FIXTURES = Path(__file__).parent / 'testdata/bazel-9.3'


class RealBepTest(unittest.TestCase):
    def report(self, name):
        path = FIXTURES / (name + '.jsonl')
        c = Container()
        for event in iter_events(str(path), c.stream):
            c.consume(event)
        return c.finish(path.stat().st_size)

    def test_cached_shards_count_targets_once(self):
        r = self.report('cpp-cached')
        self.assertEqual(r['summary'], {'tests_total': 3, 'tests_passed': 3})
        self.assertEqual(r['diagnostics'], [])
        self.assertEqual(r['invocation']['exit']['code'], 0)
        self.assertEqual(r['invocation']['stream']['status'], 'complete')

    def test_compiler_cause_is_preserved(self):
        r = self.report('compiler-failure')
        d = r['diagnostics'][0]
        self.assertEqual(d['kind'], 'compile_error')
        self.assertIn('BZLOG controlled compiler failure', d['errors'][0]['message'])
        self.assertEqual(d['target'], '//src/test/cpp/util:md5_test')
        self.assertEqual(r['summary']['targets_failed'], 1)

    def test_gtest_failure_is_final(self):
        r = self.report('gtest-failure')
        self.assertEqual(r['summary']['tests_failed'], 1)
        self.assertEqual(r['invocation']['exit']['code'], 3)
        d = r['diagnostics'][0]
        self.assertEqual(d['overall_status'], 'FAILED')
        self.assertIn('ControlledFailure', r['evidence'][d['evidence']]['text'])

    def test_framework_distillation_discloses_removed_lines(self):
        from bzlog import Evidence
        evidence = Evidence()
        ref = evidence.add('test.log', 'failed case\nassertion', framework='gtest', total_lines=100)
        self.assertTrue(evidence.store[ref]['distilled'])
        self.assertTrue(evidence.store[ref]['truncated'])

    def test_failed_attempt_followed_by_pass_is_flaky(self):
        r = self.report('flaky-standalone')
        self.assertEqual(r['summary'], {'tests_total': 1, 'tests_flaky': 1})
        self.assertEqual(r['diagnostics'][0]['kind'], 'test_flaky')
        self.assertEqual(r['invocation']['exit']['code'], 0)

    def test_loading_failure_keeps_explanation(self):
        r = self.report('missing-target')
        self.assertIn('bzlog_missing_target', r['diagnostics'][0]['description'])
        self.assertEqual(r['invocation']['stream']['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
