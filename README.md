# bzlog

Collapse a Bazel Build Event Protocol stream into an agent-sized JSON container.

Post-hoc:

```
bazel test //... --build_event_json_file=bep.json
python3 bzlog.py bep.json --pretty
```

Wrapping Bazel, consuming the stream live:

```
bzlog test //...
bzlog --progress --fail-fast 3 coverage //... --combined_report=lcov
```

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

## Container shape

- `invocation` — uuid, command, patterns, exit code, build metrics
- `summary` — counts only
- `diagnostics` — normalized failures, deduped by root cause
- `evidence` — content-addressed blobs, tail-truncated, referenced by id

Successful actions never appear. Evidence for dropped diagnostics is garbage
collected before serialization.

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
