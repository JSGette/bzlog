#!/usr/bin/env python3
"""Replay healthy test events through fail-fast reconciliation, no Bazel needed.

Usage: python3 benchmarks/fail_fast.py --baseline /tmp/bzlog-baseline.py
This measures the reconciliation loop, not whole-build throughput.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def measure(module, n, repeats):
    samples = []
    resolutions = 0
    for _ in range(repeats):
        c = module.Container()
        resolutions = 0
        start = time.perf_counter()
        for i in range(n):
            tid = {'label': f'//pkg:test{i}', 'configuration': {'id': 'cfg'}}
            for event in ({'id': {'testResult': tid}, 'testResult': {'status': 'PASSED'}},
                          {'id': {'testSummary': tid}, 'testSummary': {'overallStatus': 'PASSED'}},
                          {'id': {'progress': {'opaqueCount': i}}, 'progress': {'stderr': ''}}):
                c.handle(event)
                gate = getattr(module, 'may_add_failure', None)
                if gate is None or gate(event):
                    resolutions += 1
                    causes = sum(bool(d.get('errors') or d.get('cases') or d.get('evidence') or
                                      d.get('description') or d.get('failure_message'))
                                 for d in c._resolved_diagnostics()
                                 if d['kind'] not in ('test_flaky', 'test_recovered',
                                                      'test_attempt_failure', 'test_incomplete'))
                    assert causes == 0
        samples.append(time.perf_counter() - start)
        assert c.finish(0)['summary'] == {'tests_total': n, 'tests_passed': n}
    return {'tests': n, 'events': n * 3, 'reconciliations': resolutions,
            'median_seconds': statistics.median(samples), 'samples_seconds': samples}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--baseline', type=Path)
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--tests', type=int, nargs='+', default=[500, 1000, 2000])
    args = ap.parse_args()
    paths = {'current': Path(__file__).resolve().parents[1] / 'bzlog.py'}
    if args.baseline:
        paths['baseline'] = args.baseline
    result = {name: [measure(load(path, name), n, args.repeats) for n in args.tests]
              for name, path in paths.items()}
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
