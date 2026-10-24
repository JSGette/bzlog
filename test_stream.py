"""BEP transport integrity regressions, independent of a Bazel installation."""

import json
from pathlib import Path
import tempfile
import unittest

from bzlog import Container, iter_events, tail_events, render_text


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'bep.json'

    def consume(self, contents):
        self.path.write_text(contents, encoding='utf-8')
        c = Container()
        for event in iter_events(str(self.path), c.stream):
            c.consume(event)
        return c.finish(self.path.stat().st_size)

    def test_pretty_json_braces_and_escaped_quotes_in_strings(self):
        events = [{'progress': {'stderr': 'literal { and \\"quoted } text'}},
                  {'finished': {'exitCode': {'code': 0}}, 'lastMessage': True}]
        report = self.consume('\n'.join(json.dumps(e, indent=2) for e in events))
        self.assertEqual(report['invocation']['stream']['events_read'], 2)
        self.assertEqual(report['invocation']['stream']['status'], 'complete')

    def test_finished_without_last_message_is_incomplete(self):
        report = self.consume(json.dumps({'finished': {'exitCode': {'code': 0}}}))
        self.assertEqual(report['invocation']['stream']['status'], 'incomplete')
        self.assertIn('stream: incomplete', render_text(report))

    def test_empty_file_is_unavailable(self):
        self.assertEqual(self.consume('')['invocation']['stream']['status'], 'unavailable')

    def test_last_message_without_finished_is_incomplete(self):
        report = self.consume('{"lastMessage":true,"progress":{}}\n')
        self.assertEqual(report['invocation']['stream']['status'], 'incomplete')

    def test_malformed_line_does_not_hide_later_event(self):
        report = self.consume('not json\n{"finished":{},"lastMessage":true}\n')
        stream = report['invocation']['stream']
        self.assertEqual(stream['malformed_records'], 1)
        self.assertEqual(stream['status'], 'degraded')
        self.assertEqual(stream['events_read'], 1)

    def test_trailing_partial_object_is_explicit(self):
        report = self.consume('{"finished":{},"lastMessage":true}\n{"progress":')
        self.assertTrue(report['invocation']['stream']['trailing_fragment'])
        self.assertEqual(report['invocation']['stream']['status'], 'degraded')

    def test_handler_errors_are_counted(self):
        report = self.consume('{"action":null}\n{"finished":{},"lastMessage":true}\n')
        self.assertEqual(report['invocation']['stream']['skipped_events'], 1)
        self.assertEqual(report['invocation']['stream']['status'], 'degraded')

    def test_non_object_records_are_malformed(self):
        report = self.consume('[]\n42\nnull\n{"finished":{},"lastMessage":true}\n')
        self.assertEqual(report['invocation']['stream']['malformed_records'], 3)

    def test_live_utf8_character_split_between_flushes(self):
        text = json.dumps({'progress': {'stderr': 'échec'}}, ensure_ascii=False).encode()
        split = text.index('é'.encode()) + 1
        self.path.write_bytes(text[:split])
        calls = 0
        def alive():
            nonlocal calls
            calls += 1
            if calls == 1:
                with self.path.open('ab') as out:
                    out.write(text[split:] + b'\n')
                return True
            return False
        events = list(tail_events(str(self.path), alive))
        self.assertEqual(events[0]['progress']['stderr'], 'échec')

    def test_live_final_record_without_newline(self):
        self.path.write_text('{"finished":{},"lastMessage":true}')
        self.assertEqual(len(list(tail_events(str(self.path), lambda: False))), 1)

    def test_writer_exits_between_initial_stat_and_poll(self):
        def finished_writer():
            self.path.write_text('{"finished":{},"lastMessage":true}\n')
            return False
        self.assertEqual(len(list(tail_events(str(self.path), finished_writer))), 1)

    def test_missing_middle_event_is_detected_despite_terminal_marker(self):
        events = [{'id': {'started': {}}, 'children': [{'targetCompleted': {'label': '//:x'}}]},
                  {'id': {'buildFinished': {}}, 'finished': {}, 'lastMessage': True}]
        report = self.consume('\n'.join(json.dumps(e) for e in events))
        self.assertEqual(report['invocation']['stream']['status'], 'incomplete')
        self.assertEqual(report['invocation']['stream']['unfulfilled_events'], 1)

    def test_reannounced_and_out_of_order_children_are_not_missing(self):
        child = {'targetCompleted': {'label': '//:x', 'configuration': {'id': 'cfg'}}}
        events = [{'id': child, 'completed': {'success': True}},
                  {'id': {'started': {}}, 'children': [child, child]},
                  {'id': {'buildFinished': {}}, 'finished': {}, 'lastMessage': True}]
        report = self.consume('\n'.join(json.dumps(e) for e in events))
        self.assertEqual(report['invocation']['stream']['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
