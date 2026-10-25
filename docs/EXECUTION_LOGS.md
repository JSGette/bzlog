# Execution-log inspection and comparison

This is the first implementation slice from the README's unfinished features.
It provides explicit, offline cache investigation through `bzlog exec-log`,
without changing wrapper execution or automatically fetching remote evidence.

## Inputs and output

Capture with `--execution_log_compact_file=PATH`, then inspect the path with
`bzlog exec-log PATH --converter /absolute/path/to/converter`. Add
`--baseline BEFORE` to compare. Both inputs default to compact; use
`--input-format` and, for a different baseline format, `--baseline-format`.
Accepted formats are compact, binary, and JSON. JSON input requires no decoder.

The external executable is Bazel's `//src/tools/execlog:converter`. It receives
separate argv elements `--input compact:PATH --output json:TEMPFILE`; no shell
parsing occurs. Keep its launcher, runfiles, and Java runtime together. Use a
version compatible with the producing Bazel, especially for new compact-log
entry types. A wrapper executable can supply converter-specific JVM options.

The Python module is separate from `bzlog.py` to keep the execution-log model
independent of BEP transport semantics. Existing commands continue to use only
the original module; the new subcommand imports `bzlog_execlog.py` on demand.

JSON reports use `schema: bzlog/exec-log-v1` and contain:

- `summary`: spawn counts, hit counts, eligibility flags, runner counts,
  per-mnemonic counters, and bounded examples without a cache hit.
- `comparison`, when requested: uniquely matched counts, changed fields,
  changed input paths/environment names/platform names, action-digest changes,
  unpaired records, and ambiguous matches.
- `notices`: scope and evidence limitations present in JSON and text output.

Every bounded section has `total`, `items`, and `omitted`. `--max-actions`
defaults to 25; per-action name/path details retain 10 items. Counts always
cover all parsed records. Argument/environment/platform values are represented
internally by hashes for comparisons; reports do not print these values or
their hashes. Labels and paths remain visible. This is not a general redactor:
the source logs and temporary converter output contain full original data.

## Interpretation

The `cacheHit` field is authoritative for recorded disk/remote hits. Runner names
describe where a spawn ran or which cache supplied it. `cacheable` and
`remoteCacheable` are separate eligibility flags; their counts can overlap.
An execution without a hit does not establish that a remote lookup was made.
Persistent local action-cache reuse omits the owning spawns from the log.

Matching uses `(targetLabel, mnemonic, first sorted listed output)`. Output paths
retain configuration directories. More than one record on either side makes
that key ambiguous, including repeated attempts. Outputless records are counted
separately. Unmatched entries are called only-in-baseline/current, never fixed,
removed, or newly failing. This is a matching heuristic, not invocation or action
identity proof. Ensure the two captures describe comparable build scopes.

Arguments retain order. Environment and platform values compare by name;
platform properties may repeat names, and comparison preserves their
multiplicity. Inputs compare path, digest, symlink target, and tool designation.
Declared outputs, timeout, and cache policy are also compared. Actual output
digests and timing are not input differences. Missing action digests give
`action_digest_changed: null` on changed rows. Inputs without digests are
counted there; some represent empty files. No recorded changes cannot establish
fully identical actions. Digest-only changes remain explicitly unexplained.

The strict reader handles NDJSON and Bazel's concatenated pretty JSON. It
rejects malformed/truncated records, duplicate fields, invalid field types,
oversized records, and changing files, and does not read FIFOs. Each JSON record
is limited to 16 MiB unless `--max-record-bytes` overrides it. Converter exit
errors and per-file timeouts return 2 with no partial stdout report. A parsed
execution log has no terminal completeness marker, so a valid prefix ending
on a message boundary cannot be distinguished from a complete log.

## Alternatives and decisions

| Area | Options | Choice and reason |
|---|---|---|
| Compact decoding | Handwritten wire reader; vendored generated protobuf plus zstd dependency; Bazel converter | Use the official converter. It reconstructs input sets, directories, symlinks, and runfiles using Bazel's own semantics. This adds a tool/JDK dependency only to compact/binary inspection, while normal bzlog and JSON inspection remain standard-library-only. |
| Cache explanation | Infer misses from runners; inspect one log; compare recorded input changes | Provide both summary and explicit baseline comparison. Present evidence without claiming every non-hit is a cache lookup miss. |
| Matching | Label; action digest; outputs with target/mnemonic | Use outputs with target/mnemonic. Labels can own many actions, while digests change precisely when investigating input drift. Keep collisions ambiguous. |
| Sensitive values | Print old/new values; hash; redact with heuristics | Compare fingerprints and emit changed names only. Avoid a configurable secret detector and preserve exact change detection. |
| Scope | Automatic post-build analysis; explicit subcommand | Use an explicit subcommand. Decoding can be costly, and a separate historical log must not be attributed to a BEP invocation without evidence. |

Limits: decoding can expand compact data substantially on disk. Conversion
disk usage and total normalized action/input state remain unbounded; the
record and output limits do not cap the whole analysis. There is no native
protobuf/zstd decoder, remote cache client, automatic BEP association, or
proof of non-hermeticity. No performance improvement is claimed for builds.

## WSL validation — 2026-10-25

Python 3.12.3, Bazel 9.3.0, source
`009e2761ff3e7a2a63167e821dc3bca996c46a4d`. The converter was built in an
isolated output base using the source's workspace rc settings and a local JDK
25. Initial attempts without the required Java configuration and
`--deleted_packages=third_party/remoteapis` failed analysis; using the repository
configuration fixed the harness. These attempts are not counted as passes.

Real C++ builds used separate output bases, a dedicated disk cache, two build
jobs, and a 3 GiB server heap. Source modifications were restored afterward.

| Capture | Spawns | Cache hits | Expected comparison |
|---|---:|---:|---|
| First md5_test build | 21 | 0 | Baseline |
| Same build, fresh output base, populated disk cache | 21 | 21 | 21 matched, zero recorded input changes |
| Changed define, environment, and source comment | 21 | 0 | 21 matched changes; md5_test compilation identifies arguments, environment, and source path |
| Identical repeat using local action cache | 0 | 0 | 21 baseline-only records, no claim of disappearance or resolution |

All four compact logs were decoded and analyzed with the real converter.
Binary and direct JSON versions of the baseline also compared identically:
21 matched spawns with zero recorded changes. All **169 unittest cases** and
the **four original truncation checks** pass. The new slice adds 23 tests for
framing, malformed input, normalization, comparison uncertainty, output limits,
converter invocation/failures/timeouts, stability checks, and real fixtures.
Dedicated target and converter build servers were stopped after validation.
Comparisons used full captures; reduced JSON fixtures retain two spawns for
the normal regression suite. Raw logs and exact local commands are at
`/home/jgett/REPOS/.work/bzlog-execlog-20261025/runs.json` and are not committed.

## Remaining README items

- **CAS evidence resolution:** options are an embedded gRPC client with
  credentials, a user-supplied resolver executable, or a cache-specific HTTP
  path. Prefer an explicit resolver executable first: it can use the user's
  existing authentication and transport policy. Before accepting fetched bytes,
  validate resource names, expected size and digest, enforce a limit/timeout,
  and preserve remote-only evidence on failure. This is still unimplemented.
- **query/cquery enrichment:** options are automatic workspace-wide reverse
  dependencies, explicit scoped queries, or command previews only. Prefer
  explicit scope and current-workspace queries first, retaining configuration
  provenance when cquery is used. Broad queries can be costly; historical
  ownership cannot be inferred from today's checkout. This is still
  unimplemented.

Sources: [Bazel cache debugging](https://bazel.build/remote/cache-remote),
[converter documentation](https://github.com/bazelbuild/bazel/blob/master/src/tools/execlog/README.md),
[execution-log schema](https://github.com/bazelbuild/bazel/blob/master/src/main/protobuf/spawn.proto).
