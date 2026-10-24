"""Fail-fast must react at the same point after skipping irrelevant events."""

import unittest

from bzlog import Container, may_add_failure
from test_results import result as attempt, summary


def first_stop(events, gated):
    c = Container()
    for i, event in enumerate(events):
        c.handle(event)
        if gated and not may_add_failure(event):
            continue
        causes = [d for d in c._resolved_diagnostics()
                  if d['kind'] not in ('test_flaky', 'test_recovered',
                                       'test_attempt_failure', 'test_incomplete')
                  and (d.get('errors') or d.get('cases') or d.get('evidence') or
                       d.get('description') or d.get('failure_message'))]
        if causes:
            return i
    return None


class FailFastTest(unittest.TestCase):
    def test_event_gate_preserves_first_actionable_failure(self):
        root = {'configuredLabel': {'label': '//:dep'}}
        parent = {'id': {'configuredLabel': {'label': '//:parent'}}, 'children': [root],
                  'aborted': {'reason': 'ANALYSIS_FAILURE'}}
        cause = {'id': root, 'aborted': {'reason': 'ANALYSIS_FAILURE', 'description': 'broken dep'}}
        traces = [
            [attempt('FAILED'), attempt('PASSED', attempt=2), summary('FLAKY')],
            [attempt('FAILED'), summary('FAILED')],
            [summary('FAILED'), attempt('FAILED')],
            [parent, cause], [cause, parent],
            [{'action': {'success': False, 'failureDetail': {'message': 'disk full'}}}],
            [{'id': {'targetSummary': {'label': '//:test'}},
              'targetSummary': {'overallTestStatus': 'FAILED_TO_BUILD'}}],
        ]
        noise = [{'progress': {'stderr': ''}}, {'action': {'success': True}}] * 10
        for trace in traces:
            with self.subTest(trace=trace):
                events = [e for event in trace for e in [*noise, event]]
                self.assertEqual(first_stop(events, True), first_stop(events, False))
        self.assertIsNone(first_stop(traces[0], True))


if __name__ == '__main__':
    unittest.main()
