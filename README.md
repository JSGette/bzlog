# bzlog

Collapse a Bazel Build Event Protocol stream into an agent-sized JSON container.

Post-hoc:

```
bazel test //... --build_event_json_file=bep.json
python3 bzlog.py --pretty bep.json
```

Wrapping Bazel, consuming the stream live:

```
bzlog test //...
bzlog --progress --fail-fast 3 coverage //... --combined_report=lcov
```

For replay metadata, add `--record-command` before the Bazel command. It records
the original argv, a shell-quoted command, and working directory. This is opt-in
because arguments can contain credentials and lengthy configuration. The
environment, rc-file contents, executable version, and source revision still
need to match; diagnostic-level `repro` commands are only suggestions and do
not invent framework-specific test filters.

## Test execution audit and rerun previews

```sh
python3 bzlog.py --audit-tests --record-command test //... > report.json
python3 bzlog.py rerun report.json --format text
```

`--audit-tests` adds a full `test_audit` inventory, keyed by label and
configuration. It works in wrapper and post-hoc modes. Each row separates the
final test status from execution evidence: `executed`, `cached`, `mixed`,
`not_run`, or `unknown`. Attempt counts deduplicate run/shard/attempt IDs and
separate local and remote cache flags; a cache hit is counted once even when
both flags are set. A passing result does not imply fresh execution.

Execution is evidence about Bazel test attempts, not proof that every test case
inside a runner executed. Noncached passed/failed/timed-out attempts establish
execution; a remote infrastructure failure alone does not. Summary-only cache
totals can establish full cache reuse, but summary-only noncache totals do not
prove execution. Missing or contradictory counts are disclosed in `issues`.
Known configured tests without results remain unknown unless an explicit
build failure, skip, or pre-testing halt explains why they did not run.
Tests filtered out before BEP emission cannot be enumerated by this audit.

This inventory is opt-in and **not truncated by `--max-diagnostics`**: rerun
selection must include failures beyond the compact diagnostic limit. The
existing `summary.tests_*` counters retain their result/summary-based scope;
the audit can additionally include configured tests with missing results.
Its output and state therefore grow with the number of observed tests.

`bzlog rerun` emits a preview only, as JSON by default (`bzlog/rerun-v1`). It
selects final `FAILED`, `TIMEOUT`, `REMOTE_FAILURE`, and `FAILED_TO_BUILD`
labels from the full audit. Flaky/recovered tests and unfinished attempts are
excluded. A label with multiple configurations appears once in the command;
all of its selected failure configurations remain listed in `failed_tests`.
Other configurations of that label may also run. No testcase filter is invented.

The planner requires a complete stream, wrapper-mode `--record-command`, and
unambiguous Bazel original structured command metadata. It matches original
option forms against recorded argument boundaries, preserving startup flags,
configs, literal values, test filters, and cache policy. It rejects unknown or
redacted option boundaries, ambiguous command tokens, target-pattern files,
implicit/default targets, and commands other than `test`/`coverage`.

Explicit BEP, profile, explain, and execution-log destination options are
omitted from the preview so they do not directly reuse the original captures.
Their names are listed in `omitted_capture_options`; rc files can still supply
destinations. The preview does not pin environment, rc contents, executable,
or sources, and does not promise fresh execution when cache reuse is enabled.
It reports `nothing_to_rerun` when no confirmed final failures were observed;
this is not a claim that every requested test ran successfully.

See [the bviz comparison and implementation decisions](docs/AUDIT_RERUN_BVIZ.md)
for scope, alternatives, and real WSL validation.

## Command handling

When wrapping Bazel, bzlog prints the result and exits with Bazel's exit code.
If the Bazel process is terminated by a signal, bzlog exits with `128 + signal`.
Post-hoc analysis of a BEP file exits 0 after producing a report, even when the
recorded build failed.

bzlog's own flags come first; the first token it does not own starts Bazel's
command line and everything after it is passed through untouched. An explicit
`--` also ends bzlog's flags. Startup flags, `bazel run //x -- args`, and a
user-supplied `--build_event_json_file=` are all handled.
The separate-value form `--build_event_json_file PATH` also works. The last
Bazel-side occurrence wins; arguments after `bazel run ... --` belong to the
program. An existing BEP file is consumed only after the new process changes
it, so an old failure cannot trigger cancellation of a new invocation.

## Stream integrity

`invocation.stream` distinguishes `complete`, `incomplete`, `unavailable`, and
`degraded` input. A complete stream has a build-finished event, a terminal
`lastMessage`, and no unfulfilled announced child events. This describes
transport integrity, not build success. Empty input is unavailable; malformed
records, skipped handler events, or a trailing fragment make it degraded.
The report includes event/error counts and missing-event counts when relevant.
Text mode surfaces any non-complete state. Post-hoc reporting still exits zero:
automation should inspect both stream integrity and the recorded Bazel outcome.

Both readers accept NDJSON and pretty-printed JSON. Braces inside strings do
not frame events; live reads preserve partial UTF-8 bytes until a line is ready.
The reader tracks event identities to detect missing middle events, so memory
still scales with the number of distinct event IDs. It does not authenticate
input, reconstruct entirely unannounced events, or impose a per-record limit.
Original Bazel exit status remains authoritative in wrapper mode. A missing or
unlaunchable Bazel executable produces a report and exits 127 or 126.

BEP is written incrementally: `FileTransport` runs a writer thread that flushes
a `BufferedOutputStream` every 250ms (`EXPERIMENTAL_BEP_FILE_FLUSH_MILLIS`), and
`JsonFormatFileTransport` emits one whitespace-free JSON object per line. So the
file is tailable. Two consequences the reader must respect:

- a flush can land **mid-line** (the buffer is byte-oriented), so only parse on a
  trailing newline and carry the remainder
- bzlog's flags must come before the Bazel command

## Why not rtk/sqz

Line-oriented compressors treat build output as text. Bazel already emits a
structured event stream, so classification can be *read* instead of guessed:

| signal | source |
|---|---|
| action kind (`CppCompile`, `GoCompile`, `Javac`) | `ActionExecuted.type` |
| machine failure category (65 of them) | `FailureDetail` oneof |
| test status, attempt, shard, remote-cache hit | `TestResult` |
| per-test-case failures | `test.xml` referenced from `TestResult.testActionOutput` |
| stderr | `ActionExecuted.stderr` — a `File` pointer, not inline text |

## The actual win: root-cause collapsing

In a monorepo one broken header fails N targets with byte-identical stderr.
Line-based tools emit N copies. bzlog fingerprints
`(kind, action_type, file:line:message)` and emits one diagnostic with
`affected_count: N` plus a truncated `also_affects` list.

On the bundled fixture (47-way fan-out from a single bad header, one unrelated
test failure, 900 successful actions): 192,298 B -> 2,079 B.

## Framework-aware test log truncation

A blind tail keeps the last 40 lines of a `test.log`, which is often teardown
noise. bzlog first tries a registry of content-based detectors (the rule
mnemonic lies on custom test wrappers):

| framework | what is kept |
|---|---|
| gtest | `[ RUN ]`..`[ FAILED ]` block of each failing case + final summary |
| go test | `--- FAIL:` blocks; for panics the message + first ~5 frames |
| pytest | `FAILURES` section, each case cut at its assertion + short summary |
| python traceback | frames outside the workspace collapsed to a marker |
| JUnit | runs of framework stack frames (`org.junit`, `jdk.`, ...) collapsed |

Unrecognized logs fall back to the tail. Extracted evidence carries
`framework`, `kept_lines`, `total_lines`. Golden tests: `testdata/` +
`python3 test_truncate.py` (`--update` to regenerate).
Framework evidence is marked `distilled`; removing original lines also marks
it truncated, so `bzlog show` can disclose that it is an excerpt.

## Container shape

Reports use `schema: "bzlog/v2"`. Compared with v1, test diagnostics distinguish
final failures from flaky, recovered, and incomplete outcomes, and cache metrics
use `spawn_cache.hit_rate` instead of `remote_cache_hit_rate`. Consumers should
handle the new diagnostic kinds and metric field described below.

- `invocation` — uuid, command, patterns, exit code, build metrics
- `summary` — counts only
- `diagnostics` — normalized failures, deduped by root cause
- `evidence` — content-addressed blobs, tail-truncated, referenced by id

Successful actions never appear. Evidence for dropped diagnostics is garbage
collected before serialization.

Analysis and loading failures retain their messages, including failures attached
to target-pattern events. Empty analysis events are linked to descriptive root
causes through BEP child IDs, regardless of event arrival order. Failures with
unavailable logs are kept separate instead of being assumed identical; matching
compiler diagnostics are grouped within the same configuration.

When structured messages are missing, bzlog retains bounded BEP progress and
Bazel console context. These logs are marked as invocation-wide evidence, not
attributed to a particular target. Early errors and final output are both kept
so a long list of skipped-target warnings does not hide the initial failure.

## Inspecting evidence

The default `--evidence ref` writes a unique, private temporary sidecar for each
invocation and returns its path as `evidence_sidecar`. Text output includes a
command you can run to read the referenced excerpt:

```
bzlog show /tmp/bzlog-evidence-EXAMPLE.json e1
```

Use the actual path and evidence ID from your report. Sidecars contain bounded
excerpts, not full original logs, and remain available until removed or cleaned
up by the system's temporary-file policy. Separate invocations do not overwrite
each other's evidence. If saving fails, evidence stays inline in the report.

`--evidence inline` includes excerpts in both JSON and text output;
`--evidence none` omits evidence blobs and references. Diagnostic messages and
brief invocation context remain visible in every mode.

Regression checks: `python3 -m unittest discover` and `python3 test_truncate.py`.

## Test outcomes and retries

Test counts are per target **and configuration**, using `TestSummary` for the
final outcome. If Bazel omits it, a test status from `TargetSummary` is used
instead, including `FAILED_TO_BUILD`; non-test target summaries are ignored.
`tests_passed`, `tests_flaky`, `tests_failed`, and
`tests_incomplete` are separate categories; their sum is `tests_total`, the
number of configured tests observed in results or summaries. Repeated events
do not inflate counts. When neither summary is available, a test remains
incomplete even when a passing or failing attempt has been observed.

Failed attempts retain their evidence after reconciliation:

- `test_failure`: a final failing summary, including timeouts and remote failures.
- `test_flaky`: the final summary is `FLAKY`.
- `test_recovered`: earlier unsuccessful attempts with a final `PASSED` summary.
- `test_attempt_failure`: unsuccessful attempts without a conclusive final status.
- `test_incomplete`: no conclusive final status and no unsuccessful attempt evidence.

Diagnostics carry `overall_status`; `final` indicates whether a summary was
received. Grouped failures retain a bounded `attempts` list with target, run,
shard, attempt, status, and cache provenance. `occurrences` counts unsuccessful
attempts, while `affected_count` counts distinct target labels. Additional
attempts are counted in `attempts_omitted`. Summary-only failures still produce a
diagnostic, using a referenced failed log when available.

Live test diagnostics are marked `provisional`. A later `test_summary` progress
event supplies the final status. `--fail-fast` waits for a conclusive failing
test summary so that retries can complete; flaky and recovered tests do not
trigger cancellation.

## Not done yet

- compact exec log (`--execution_log_compact_file`, zstd + `spawn.proto`) for
  cache-miss analysis
- `bytestream://` blobs need a CAS fetch to resolve
- `bazel query`/`cquery` enrichment: map a failing file back to the owning
  target and its reverse deps


## Output modes

| mode | bytes on the bundled fixture |
|---|---|
| raw BEP | 193,673 |
| naive "dump failing actions" (jq-style) | 39,827 |
| json, evidence inline | 2,267 |
| json, evidence in sidecar (default) | 1,587 |
| `--format text` | 628 |

`--format text` drops JSON punctuation, which is ~30% of the token cost and
carries no information the model needs. Keep JSON when a harness parses it.

Metrics are reported by exception: the block appears for a spawn cache hit rate
below 0.7 when cache hits or remote executions were observed, or wall time above
two minutes. A purely local build does not generate a cache anomaly.
`--all-metrics` includes all collected metrics.

`spawn_cache` reports `remote_hits`, `disk_hits`, `other_hits`, `executions`,
`remote_executions` (a subset of executions), `outcomes`, and `hit_rate`:

```
hits = remote_hits + disk_hits + other_hits
outcomes = hits + executions
hit_rate = hits / outcomes
```

These are observed spawn outcomes, not remote cache lookup requests. Aggregate
`total`, `internal`, and `action cache hit` runner counters count actions and are
excluded. Raw runner counts and separate local `action_cache` statistics remain
available with `--all-metrics`. The misleading `remote_cache_hit_rate` field has
been replaced by `spawn_cache.hit_rate`; consumers should update accordingly.
For example, 90 remote hits and 10 executions now yield 0.9 instead of 0.45.

Local action-cache `DIGEST_MISMATCH` counts remain available with `--all-metrics`.
They are not automatically classified as anomalies: normal source edits change
digests, so this count alone does not establish non-hermeticity.

Each diagnostic carries a `repro` command and, where the error has a file and
line, the surrounding source lines -- both exist to save the agent a tool call.

## Validation and development

Run `python3 -m unittest discover` and `python3 test_truncate.py`.
`testdata/bazel-9.3` contains reduced real streams from Bazel's C++ tests,
including compiler, gtest, loading, caching, and retry cases.

Fail-fast only reconciles diagnostics on events that can add a failure. Passing
tests and progress no longer rescan every accumulated test. The isolated loop
benchmark is `python3 benchmarks/fail_fast.py`; its result is not a whole-build
speedup. See [WSL validation and decisions](docs/WSL_VALIDATION_2026-10-24.md)
and [lessons learned](docs/LESSONS_LEARNED.md) for evidence and next work.
