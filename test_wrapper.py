"""Wrapper argument ownership, stale streams, and replay metadata."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('bzlog.py')


@unittest.skipUnless(os.name == 'posix', 'executable shebang uses POSIX')
class WrapperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = self.root / 'bazel'
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ['PATH'],
                        TMPDIR=str(self.root))

    def fake_bazel(self, body):
        self.fake.write_text(f'#!{sys.executable}\nimport json, sys, time, signal\n' + body)
        self.fake.chmod(0o755)

    def invoke(self, *args):
        p = subprocess.run([sys.executable, str(SCRIPT), '--evidence', 'inline', *args],
                           cwd=self.root, env=self.env, capture_output=True, text=True, timeout=10)
        return p, json.loads(p.stdout)

    def writer(self):
        self.fake_bazel(
            "args = sys.argv[1:]\n"
            "args = args[:args.index('--')] if '--' in args else args\n"
            "path = None\n"
            "for i, a in enumerate(args):\n"
            "    if a.startswith('--build_event_json_file='): path = a.split('=', 1)[1]\n"
            "    elif a == '--build_event_json_file': path = args[i + 1]\n"
            "with open(path, 'w') as out:\n"
            "    out.write(json.dumps({'finished': {'exitCode': {'code': 0}}, 'lastMessage': True}) + '\\n')\n")

    def test_separate_value_bep_path_and_last_option_win(self):
        self.writer()
        for args in [('--build_event_json_file', 'requested.json'),
                     ('--build_event_json_file=ignored.json', '--build_event_json_file', 'requested.json'),
                     ('--build_event_json_file', 'ignored.json', '--build_event_json_file=requested.json')]:
            with self.subTest(args=args):
                p, r = self.invoke('build', '//:target', *args)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertTrue((self.root / 'requested.json').exists())
                self.assertEqual(r['invocation']['stream']['status'], 'complete')
                self.assertFalse((self.root / 'ignored.json').exists())

    def test_program_arguments_do_not_select_bep_path(self):
        self.writer()
        p, r = self.invoke('run', '//:target', '--', '--build_event_json_file=program.json')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r['invocation']['stream']['status'], 'complete')
        self.assertFalse((self.root / 'program.json').exists())

    def test_stale_failure_does_not_cancel_new_build(self):
        old = self.root / 'stale.json'
        old.write_text(json.dumps({'aborted': {'reason': 'ANALYSIS_FAILURE', 'description': 'OLD FAILURE'}}) + '\n')
        self.writer()
        body = self.fake.read_text().replace('args = sys.argv[1:]', 'time.sleep(0.2)\nargs = sys.argv[1:]')
        self.fake.write_text(body)
        p, r = self.invoke('--fail-fast', '1', 'build', '//:target', '--build_event_json_file=stale.json')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(r['diagnostics'], [])
        self.assertNotIn('stopped_early', r['invocation'])

    def test_startup_failure_does_not_read_untouched_old_bep(self):
        old = self.root / 'old.json'
        content = '{"finished":{"exitCode":{"code":0}},"lastMessage":true}\n'
        old.write_text(content)
        self.fake_bazel("print('ERROR: startup failed', file=sys.stderr)\nsys.exit(2)\n")
        p, r = self.invoke('build', '//:target', '--build_event_json_file=old.json')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(r['invocation']['stream']['status'], 'unavailable')
        self.assertEqual(r['stats']['input_bytes'], 0)
        self.assertEqual(old.read_text(), content)

    def test_missing_bazel_returns_report_without_traceback_or_temp_leak(self):
        self.env['PATH'] = str(self.root)
        p, r = self.invoke('build', '//:target')
        self.assertEqual(p.returncode, 127)
        self.assertNotIn('Traceback', p.stderr)
        self.assertEqual(r['invocation']['stream']['status'], 'unavailable')
        self.assertEqual(list(self.root.glob('bzlog-*.json')), [])

    def test_record_command_is_opt_in_and_quotes_original_arguments(self):
        self.writer()
        args = ['--host_jvm_args=-Xmx1g', 'run', '//:target', '--', 'a b', '$(literal)']
        p, r = self.invoke('--record-command', *args)
        replay = r['invocation']['replay']
        self.assertEqual(replay['argv'], ['bazel', *args])
        self.assertEqual(shlex.split(replay['command']), replay['argv'])
        self.assertEqual(replay['cwd'], str(self.root))
        _, ordinary = self.invoke(*args)
        self.assertNotIn('replay', ordinary['invocation'])


if __name__ == '__main__':
    unittest.main()
