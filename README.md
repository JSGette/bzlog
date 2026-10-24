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

When wrapping Bazel, bzlog prints the result and exits with Bazel's exit code.
If the Bazel process is terminated by a signal, bzlog exits with `128 + signal`.
Post-hoc analysis of a BEP file exits 0 after producing a report, even when the
recorded build failed.

bzlog's own flags come first; the first token it does not own starts Bazel's
command line and everything after it is passed through untouched. An explicit
`--` also ends bzlog's flags. Startup flags, `bazel run //x -- args`, and a
user-supplied `--build_event_json_file=` are all handled.

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

## Container shape

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

Metrics are reported by exception: a healthy cache hit rate is not news, so the
block only appears when something is off (hit rate below 0.7, DIGEST_MISMATCH at
scale, wall time above two minutes). `--all-metrics` overrides.

Each diagnostic carries a `repro` command and, where the error has a file and
line, the surrounding source lines -- both exist to save the agent a tool call.
