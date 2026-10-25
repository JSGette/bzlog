# Changelog

## [0.0.1](https://github.com/JSGette/bzlog/releases/tag/v0.0.1) — 2026-10-25

First public release. bzlog turns Bazel's structured events and execution logs
into compact, evidence-backed reports for coding agents and command-line use.
Python 3.10+; standard library only for the core. Apache-2.0 licensed.

### Installation and agent support

- Offline `install.sh` / `install.py` for Linux, macOS, and WSL; configurable
  prefix, explicit project selection, multiple agent selections, and dry run.
- ChatGPT Codex `AGENTS.md` integration, including existing nonempty
  `AGENTS.override.md`; Claude Code `.claude/rules/bzlog.md`; Cursor
  `.cursor/rules/bzlog.mdc`. One shared Bazel workflow covers all three.
- Repeatable managed-block updates preserve unrelated instructions, line
  endings, and permissions. Preflight rejects conflicting unmanaged files,
  malformed blocks, changed Cursor frontmatter, and unsafe file types.
- Standalone installed command with both Python modules and license; source
  checkout need not remain in place. `bzlog --version` and `bzlog --help`.

### BEP analysis and compact diagnostics

- Live Bazel wrapper and post-hoc JSON Build Event Protocol analysis.
- Compact JSON, pretty JSON, and text output; configurable diagnostic count;
  inline, referenced, or omitted evidence. Main report schema `bzlog/v2`.
- Compile/action, analysis, loading, and test failure classification using BEP
  fields; failure details, labels, configurations, and action types retained.
- Root-cause grouping with affected-target counts and bounded related labels;
  compiler failures stay separated across configurations. Missing evidence is
  never sufficient to collapse failures together.
- Causal child-event links connect empty analysis failures to descriptive root
  causes regardless of event order. Target-pattern failures remain visible.
- Compiler/source diagnostics, source-context snippets, and suggested narrow
  repro commands. Invocation-wide BEP progress and console fallback preserve
  early errors and final output when structured failure details are absent.
- Framework-aware excerpts for gtest, Go tests/panics, pytest, Python tracebacks,
  and JUnit; JUnit XML failing-case extraction; tail fallback for unknown logs.
  Distilled/truncated evidence is labeled and bounded by lines and bytes.
- Unique private temporary evidence sidecars, inline fallback if saving fails,
  unused evidence removal, and `bzlog show SIDECAR ID` for on-demand retrieval.

### Test correctness, audit, and rerun planning

- Final test summaries reconcile retries, runs, shards, duplicate events, and
  target configurations. Final failures, flaky, recovered, and incomplete
  tests have distinct counters and diagnostic kinds; failed attempts retain
  bounded evidence and local/remote cache provenance.
- `--progress` emits provisional live diagnostics and final test summaries.
  `--fail-fast N` interrupts after the configured number of root causes and
  waits for conclusive test failure, allowing recoverable retries to finish.
- Fail-fast reconciliation runs only on events capable of changing failure
  state, avoiding repeated whole-inventory scans on healthy progress events.
- `--audit-tests` supplies a full, untruncated configured-test inventory with
  separate status and executed/cached/mixed/not-run/unknown evidence; missing
  or contradictory evidence is disclosed.
- Opt-in `--record-command` retains original argv, working directory, and a
  shell-quoted command without guessing framework-specific filters.
- `bzlog rerun REPORT` produces JSON (`bzlog/rerun-v1`) or text previews for
  final failing labels, retaining original options and their argument
  boundaries. It deduplicates labels across configurations and excludes
  recovered/flaky/incomplete tests.
- Rerun planning requires trustworthy complete captures and original command
  metadata; ambiguous/redacted layouts and target-pattern files are rejected.
  Explicit capture destinations are omitted to avoid reusing saved artifacts.
  Previews never execute commands automatically or promise hermetic replay.

### Durable invocation bundles

- `--bundle NEW_DIR` captures raw JSON BEP, wrapper console, full test logs/XML,
  and action stdout/stderr with event/field and per-attempt associations.
- BEP-announced tool logs/profiles and fresh requested binary/text BEP, profile,
  explain, and execution-log files are captured without enabling extra Bazel
  flags. Remote-only, missing, or failed captures have explicit statuses.
- Private directories, SHA-256 blob names and deduplication, versioned
  `bzlog/bundle-v1` manifests, and atomic manifest finalization. Optional capture
  failures preserve the original Bazel process outcome.
- Portable inline-evidence `report.json`, `evidence.json`, relative artifact
  paths, and Git HEAD/tracked-dirty observations for live captures. Saved bytes
  survive later Bazel invocations overwriting original file paths.
- `bzlog show BUNDLE ID` retrieves excerpts. `bzlog verify BUNDLE` checks sizes,
  hashes, path containment, regular files, and manifest structure while keeping
  integrity distinct from capture completeness or build/test success.
- Post-hoc bundles snapshot currently available referenced files and retain
  the original raw BEP unchanged; they do not claim historical file identity.

### Cache metrics and execution-log analysis

- Build metrics by exception or `--all-metrics`; spawn-cache hit rates use
  observed hit/execution outcomes, separating local action-cache reuse and
  aggregate action counters. Raw runners and local action-cache statistics
  remain available. Digest mismatches alone are not labeled non-hermeticity.
- `bzlog exec-log FILE` reads compact and binary execution logs through Bazel's
  official converter, or decoded SpawnExec JSON directly without dependencies.
- Cache hits, non-hit outcomes, cache eligibility, runners, and mnemonics,
  with bounded action examples. JSON (`bzlog/exec-log-v1`) and text output.
- `--baseline BEFORE` compares unique target/mnemonic/output identities and
  reports changes to command arguments, environment names, platform properties,
  input paths/digests/symlinks/tool flags, declared outputs, timeout, cache policy,
  and action digests. Argument/environment values are fingerprinted internally
  instead of copied into reports.
- Ambiguous/outputless actions remain unpaired; actions present only in one
  capture are listed separately. Local action-cache reuse may omit spawns, so
  absence is not a failure or remote-cache miss.
- Strict UTF-8/JSON framing for NDJSON and adjacent pretty JSON objects,
  schema/type/duplicate-field validation, regular-file and changing-input
  checks, a configurable record-size limit, and converter timeout. Invalid
  input returns 2 without a partial report; valid analysis returns 0 regardless
  of recorded action exit codes.

### Wrapper and stream reliability

- Original Bazel exit codes retained; signal exits use `128 + signal`, missing
  executables return 127, and unlaunchable executables return 126.
- bzlog flags precede Bazel arguments; startup options and `run ... -- args`
  pass through. Both forms of explicit JSON BEP output flags work, with the
  final Bazel-side occurrence winning. Old unchanged captures are not consumed.
- NDJSON and pretty JSON BEP parsing, partial UTF-8 live reads, and explicit
  complete/incomplete/degraded/unavailable stream states. Malformed records,
  skipped events, trailing fragments, and missing announced children are
  disclosed independently of process success.
- Correct protobuf JSON omitted-default handling and configured-test counts;
  relative file URIs resolve against the workspace, remote file hosts are not
  read locally, and malformed deferred artifact URIs do not crash bundling.

### Validation and documented limits

- Standard-library regression suite, golden framework-log fixtures, reduced
  real Bazel 9.3 BEP and execution-log fixtures, a fail-fast microbenchmark,
  WSL build/test/cache runs on Bazel's source, and documented design decisions.
- Real execution-log validation covers uncached builds, fresh output bases
  sharing a disk cache, controlled source/command/environment changes, local
  action-cache reuse, and equivalent compact/binary/decoded JSON analysis.
- [bviz overlap and scope](docs/AUDIT_RERUN_BVIZ.md),
  [bundle design](docs/BUNDLES.md), [execution-log choices](docs/EXECUTION_LOGS.md),
  [agent integration](docs/AGENT_INTEGRATION.md), and
  [lessons learned](docs/LESSONS_LEARNED.md) accompany the release.
- Still outstanding: remote `bytestream://` CAS fetching and `query`/`cquery`
  ownership/reverse-dependency enrichment. No graphical viewer, remote upload,
  general redaction, automatic converter installation, or hermetic replay.
- Execution logs lack a terminal completeness marker. Total comparison memory,
  converter disk usage, BEP record sizes, and audit inventories are not globally
  bounded. Bundles do not recover an interrupted collection journal. Raw
  captures and recorded commands may contain sensitive values; nothing is
  automatically uploaded or redacted.
