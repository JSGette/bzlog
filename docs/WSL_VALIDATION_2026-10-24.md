# WSL validation and implementation decisions — 2026-10-24

## Location and starting point

Work lives in `/home/jgett/REPOS/bzlog`, branch
`codex/wsl-bazel-validation`, with remote `https://github.com/JSGette/bzlog.git`.
The Downloads patch was applied using `git am --3way`, preserving its three
commits and authorship. Imported commits are `eb41c6c`, `63d2810`, and `efba6f3`.
The original Windows checkout was restored to its original main branch and its
existing PLAN.md and Python cache were preserved. Nothing was pushed.

Environment: WSL Ubuntu, Python 3.12.3, installed Bazel 9.3.0, Bazel source
`009e2761ff3e7a2a63167e821dc3bca996c46a4d`. The local source's .bazelversion is
9.3.0. The prior review used Bazel 10 source; these are **Bazel 9.3 runtime
results**, not a Bazel 10 runtime claim. No full Bazel test-suite claim is made.

The main Bazel checkout remained clean. Fault injection used a detached
worktree at `/home/jgett/REPOS/.work/bzlog-validation-20261024/bazel-fixture`;
injected source changes were restored. Separate output bases, four build jobs,
and a 3 GiB server heap kept this work isolated. No EngFlow configuration was
enabled. Existing dependency caches were available, so the first build was not
a cold-download benchmark.

## Implemented beyond the supplied patch

- Stream integrity reporting, including malformed records, handler failures,
  partial final records, terminal markers, and missing announced child events.
- Shared offline/live parser handling braces inside strings and UTF-8 split
  across writes; bounded read chunks instead of an unbounded live `read()`.
- Correct separate/equal BEP flag forms, last-option behavior, and program
  argument ownership after `--`.
- File freshness checks that prevent old BEP data triggering fail-fast or
  fabricating a report after startup fails. Existing files are not pre-truncated.
- Missing executable reporting and cleanup, with conventional 127/126 exits.
- Opt-in original argv/cwd recording; narrowed repro commands are labeled
  suggestions and no longer invent framework-specific filters.
- Proto3 default handling for successful exit codes and failed target completion.
- Explicit disclosure of framework-distilled excerpts.
- Fail-fast reconciliation only on events that can add failures.
- Real BEP corpus, focused regressions, and a reproducible loop benchmark.

## Runtime evidence

All times below are elapsed wrapper wall time on this machine. They are single
runs unless stated otherwise; cache/server state differs between runs.

| Case | Exit | Observed result | Seconds |
|---|---:|---|---:|
| First C++ test invocation | 0 | md5, numbers, strings: 3 passed; strings has 2 shards | 9.188 |
| Cached C++ repeat | 0 | Same 3 targets passed | 0.518 |
| Missing label in real test package | 1 | Loading diagnostic retains target and cause | 0.265 |
| Invalid build option | 2 | Invocation failure retained | 0.265 |
| Injected `#error` in md5_test.cc | 1 | Compiler message, file, line, source context retained | 6.553 |
| Injected gtest assertion | 3 | Final failure, case and evidence retained | 2.704 |
| Failed first attempt, successful retry | 0 | One flaky target; fail-fast did not cancel retry | 0.367 |
| Restored source | 0 | md5 and strings passed | 0.981 |
| Final cached run with fail-fast | 0 | 3 passed, complete stream, 565-byte report | 0.370 |
| Two runs per target, including shards | 0 | 3 targets passed, complete stream | 0.470 |
| Final loading failure with fail-fast | 1 | Complete failure stream | 0.269 |
| Invalid startup option with stale BEP | 2 | Unavailable current stream; old file preserved, 0 input bytes | 0.122 |

The flaky harness needed two corrections: Bazel accepts `standalone`, not
`local`, for TestActionContext; the spawned TestRunner also needed
`--strategy=TestRunner=local` to write the controlled marker outside its
sandbox. The failed harness attempts were preserved as such, not counted as
flaky-validation passes. The successful command uses both settings.

Raw streams, reports, exact argv, timings, and intermediate probes are under
`/home/jgett/REPOS/.work/bzlog-validation-20261024`. In particular:
`runs.json`, `failure-runs.json`, `final-runs.json`, and
`fail-fast-benchmark.json`. Contemporaneous inline reports preserve evidence
that later Bazel runs overwrote at the original log/XML paths.

The real-stream regressions independently verify omitted zero exit codes,
omitted false target-completion values, cached/sharded counting, compiler
causes, loading errors, and flaky reconciliation. The final suite contains
91 unittest cases, plus 4 original truncation checks.

## Measured optimization

Three repetitions per input size; medians. This isolates healthy-test
reconciliation in the wrapper's fail-fast loop against the imported patch
baseline, with equal final summaries. It excludes Bazel execution, JSON parsing,
graph-integrity tracking, rendering, and process startup.

| Tests / events | Imported patch | Current | Reconciliations before → after |
|---|---:|---:|---:|
| 500 / 1,500 | 0.071910 s | 0.003017 s | 1,500 → 0 |
| 1,000 / 3,000 | 0.285269 s | 0.005924 s | 3,000 → 0 |
| 2,000 / 6,000 | 1.150640 s | 0.011748 s | 6,000 → 0 |

The largest case is about 98x faster **in this loop**, not in a whole Bazel
build. Doubling tests previously quadrupled cost; the revised healthy path is
approximately linear. Failure-heavy workloads can still require repeated
reconciliation and should be benchmarked separately before further indexing.

```bash
cd /home/jgett/REPOS/bzlog
python3 -m unittest discover
python3 test_truncate.py
git show efba6f3:bzlog.py > /tmp/bzlog-patch-baseline.py
python3 benchmarks/fail_fast.py --baseline /tmp/bzlog-patch-baseline.py
```

## Alternatives and reasons for the choices

| Decision | Options considered | Choice and reason |
|---|---|---|
| Development location | Windows mount, native WSL checkout, remote cloud | Native WSL: fulfills the requested environment and isolates source/testing from Windows I/O. |
| Patch transfer | Flatten with git apply; preserve history with git am | git am in the native checkout preserves the supplied three changes as reviewable commits. |
| Bazel version | Installed matching 9.3; download 10 RC; build a new Bazel | Use installed 9.3 matching local source. Produces runtime evidence quickly; leave Bazel 10 runtime compatibility explicitly unclaimed. |
| Test breadth | Entire Bazel suite; representative real targets; only fake processes | Real C++ tests plus injected compiler/test/retry cases, and fake-process races. This covers the changed interfaces cheaply and deterministically. |
| Stream integrity | Only exit code; final markers; announced-event tracking | Track markers and announcements: dropping a middle event must remain visible. Cost is event-ID memory proportional to stream size. |
| Parsing | Brace counting; decode after every appended line; string-aware framing | Fast NDJSON parsing plus string-aware multiline framing avoids literal-brace loss and repeated parsing of an unfinished pretty record. |
| Stale BEP | Truncate the caller's file; always redirect to a private file; observe freshness | Compare inode/size/high-resolution timestamps before reading. Preserve caller ownership and live file semantics, without destroying the old file on a startup failure. |
| Replay | Record everything by default; infer from expanded BEP arguments; opt-in argv/cwd | Opt-in direct argv/cwd preserves argument boundaries, startup flags and program args without adding token/credential retention to every report. It is deliberately scoped, not a hermetic reproduction. |
| Fail-fast speed | Maintain a new incremental diagnostic index; cache all reconciliation; skip irrelevant events | Skip events unable to add a failure. It is the narrowest change with measured benefit; existing reconciliation remains authoritative. |
| Next feature | History/delta; remote CAS; durable full evidence; integrity and replay first | Implement integrity/replay now: both have concrete failure cases and no state migration or authentication dependency. Durable evidence is the next larger slice. |

## Next work, prioritized

1. **Durable invocation inspection.** Add an opt-in invocation directory with
   a manifest, original event IDs, full log/XML copies or content hashes,
   affected targets and attempts. Options: JSON manifest plus blobs, SQLite,
   or a content-addressed store. Prefer a versioned manifest plus blobs first:
   dependency-free and inspectable. Current sidecars store excerpts in /tmp;
   they do not implement this feature.
2. **Bounded memory and output.** Successful attempt state, event-ID sets, one
   huge JSON record, and full local log reads can still grow. Options: hard
   limits, spooling, or streaming extraction. Prefer explicit limits with
   omission metadata and optional spooling; never silently infer success from
   dropped records. Measure failure-heavy fan-out separately.
3. **Accurate final output accounting.** Existing stats describe an intermediate
   JSON payload before final formatting/enrichment and count characters in one
   path. Measure final UTF-8 bytes consistently, with documented inclusion of
   stats and sidecars. This record uses actual file sizes where quoted.
4. **Artifact availability and remote resolution.** Distinguish absent, missing
   local, unreadable, remote-only and empty evidence before adding CAS retrieval.
   Options: a pluggable resolver, external CAS CLI, embedded gRPC client. Prefer
   explicit availability states and an opt-in resolver interface first; no
   credential inference or automatic remote fetch is implemented.
5. **Invocation comparison.** Reuse the original plan's strong/weak fingerprint
   distinction and scope checks. Options: automatic workspace state or explicit
   `diff before after`. Prefer explicit stored-invocation diff first so partial
   builds cannot silently mark unseen targets fixed. Version fingerprints.
6. **Failure-heavy indexing and causal output.** Gate scans already fixes the
   healthy path; repeated failures can still be expensive. Benchmark before
   choosing memoized BEP graph resolution versus incremental indexes. Expose
   proven BEP causes separately from similar diagnostics.
7. **Broader compatibility.** Add Bazel 10 runtime, Java/JUnit, remote-only logs,
   custom runners, cancellation/process-group behavior, and command types that
   do not emit BEP. They are not covered by this C++ runtime matrix.

## Limits of this slice

Integrity checks detect announced missing events, not omitted events with no
surviving announcement, authenticated provenance, or arbitrary concatenated
invocations. Read chunks are bounded; total event/log/state memory is not.
File freshness assumes normal Bazel file writes and a local filesystem with
usable timestamps; concurrent writers to one BEP path remain unsupported.
No historical flaky model, delta store, CAS client, or full-evidence archive
has been implemented. Existing PLAN.md remains unchanged.
