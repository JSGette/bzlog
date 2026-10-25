# Invocation bundles — 2026-10-25

`--bundle NEW_DIR` saves a local invocation for later inspection. It adds no
runtime dependencies and preserves the single-file bzlog distribution. The
existing compact stdout report and Bazel process exit behavior remain intact.

## Capture contract

The directory must be new; bzlog refuses an existing directory before launching
Bazel. Under WSL/POSIX the directory is created with mode 0700, and files written
by bzlog use 0600. The feature is opt-in because raw BEP, logs, and recorded
arguments can contain environment values or other sensitive data. There is no
automatic redaction, upload, or remote CAS retrieval.

Captured data:

- Raw JSON BEP, including malformed records if present. A wrapper-generated
  temporary BEP is copied before cleanup; untouched stale files are not copied
  after a startup failure. Post-hoc mode snapshots its input before parsing.
- Full action stdout/stderr references and every observed test result's
  `test.log` and `test.xml`, including successful and cached results.
- TestSummary passed/failed log references and BEP BuildToolLogs, including an
  announced profile. Tool logs are copied after event consumption finishes.
- The wrapper's full combined console output, separate from its bounded report
  excerpt. Post-hoc input cannot reconstruct a console capture it did not contain.
- Explicit binary/text BEP, profile, explain, and execution-log destinations
  when canonical command metadata confirms the effective option and the file
  changed since its pre-invocation observation. BEP-announced files can instead
  be associated directly. Unverified rc-supplied destinations are disclosed.
- A portable report with inline excerpts, an excerpt sidecar, invocation UUID,
  command/version/exit/stream metadata, and, in live mode when Git is available,
  the starting Git HEAD and tracked-file dirty status. Untracked source files,
  source contents, executable, rc files, and environment are not snapshotted.

The flag does not enable extra profiling or transport flags. For bviz-compatible
captures, supply `--build_event_binary_file=...` and `--profile=...` as usual;
bzlog preserves those requested output paths and archives their resulting bytes.
Compressed snapshots retain a `.gz` suffix based on their gzip signature.

## Layout and schema

```text
bundle/
  manifest.json         bzlog/bundle-v1
  report.json           bzlog/v2, inline evidence
  evidence.json         existing e1/e2 excerpt map
  blobs/<sha256>[.gz]   full captured bytes, deduplicated by content
```

All manifest artifact paths are relative to the bundle directory. Original
source URIs and replay cwd are provenance, not dependencies for inspecting saved
bytes. Raw BEP retains its original references; reprocessing that file alone
does not automatically substitute archived logs. Use `report.json`, the
manifest, and the listed blobs after moving the bundle.

The manifest starts in `state: collecting`. Finalization writes the report,
sidecar, and then an atomically replaced `state: finalized` manifest. An
abruptly terminated wrapper can leave a collecting manifest and unindexed blobs;
v1 does not implement journal recovery. Atomic replacement does not promise
power-loss durability or authenticate who produced the files.

Each artifact records an ID (`a1`, ...), kind, availability status, and, when
saved, its relative path, byte count, and SHA-256. References from BEP also
record the original event ID and file field, preserving target/configuration
and run/shard/attempt associations. The same bytes can serve several references.
No diagnostic count limit truncates the full-file archive.

Availability statuses include `saved`, `missing`, `remote_only`, `no_reference`,
`invalid_inline`, `invalid_uri`, `not_regular`, `unreadable`, `write_error`, `copy_error`,
`changed_during_copy`, `not_produced`, `unverified`, and `capture_error`.
Empty files are saved successfully with zero bytes. Remote URIs are recorded
without fetching. Regular-file checks avoid reading devices or blocking on
FIFOs. A copy reads at most the initially observed file size and checks metadata
again afterward; changed copies remain disclosed rather than called complete.

`capture_status` is complete only when all selected artifacts were saved and no
archive errors occurred. This does not assert that every possible Bazel output
was generated, that the BEP is complete, or that tests passed. Those states are
reported separately. Storage failures leave report diagnostics available and
preserve Bazel's exit status. Failure to create the requested bundle before
launching Bazel returns 2 without starting the build.

`--evidence` controls stdout rendering. The bundle retains full selected files
and inline report excerpts even with `--evidence none`. In default ref mode,
stdout points to the durable `evidence.json` instead of a temporary sidecar.
If that sidecar cannot be saved, stdout falls back to inline evidence.

## Inspection and verification

```sh
bzlog verify /path/to/bundle
bzlog show /path/to/bundle e1
bzlog rerun /path/to/bundle/report.json --format text
```

The rerun command still requires an audit and recorded command from the
original invocation. `show` reads the compact excerpt; full log/XML/capture
files are accessible at their manifest paths. A saved binary BEP blob can be
passed to `bviz --bep`, and a saved profile to `bviz --profile`.

`verify` emits `bzlog/verify-v1` JSON and returns 0 if the listed saved files
match their hashes/sizes, otherwise 2. It rejects unfinished/unsupported
manifests, missing files, traversal outside the bundle (including resolved
symlinks), nonregular files, and modifications detected during verification.
It reports capture and stream states separately: a partial archive can have
valid saved bytes. The manifest itself is not authenticated, so verification
cannot detect coordinated changes to both the bytes and their recorded hashes.

## Alternatives and decisions

| Decision | Alternatives | Choice and reason |
|---|---|---|
| Storage format | ZIP, SQLite, directory plus manifest | Directory plus versioned JSON and blobs: inspectable, movable, dependency-free; no extraction or database migration needed. ZIP/export can be added separately. |
| Capture timing | Resolve every file at the end; snapshot on BEP arrival | Action/test snapshots at consumption time reduce overwritten-attempt loss. Tool logs and raw captures are finalized after writing completes. |
| File scope | Every named Bazel output; only report excerpts; selected full diagnostic files | Full log/XML/console/capture data. Arbitrary build outputs and test-output archives can be enormous and are outside v1. |
| Deduplication | Source path; one copy per reference; content hash | Content hash retains different versions of a reused path and shares identical bytes across result/summary references. |
| Capturing bviz inputs | Force extra flags; guess paths; preserve user captures | Preserve supplied flags. BEP references or canonical option plus freshness evidence associate files; ambiguous/unchanged files are not silently included. |
| Source reproducibility | Copy workspace; save Git metadata; infer source from later checkout | Live starting HEAD and tracked-dirty status only. A bundle is evidence storage, not a hermetic replay package. Post-hoc mode makes no historical source-state inference. |
| Archive failure | Replace Bazel exit status; discard report; disclose separately | Keep Bazel's exit and usable diagnostics. Partial/missing archive content is explicit in the report and manifest. |
| Crash recovery | Rewrite manifest per event; append journal; atomic final manifest | Atomic final manifest for this slice avoids quadratic manifest rewriting. Collecting state prevents false completion; journal recovery is deferred. |
| Verification | Assume copied files are intact; checksum snapshots | Stream SHA-256 and byte checks, independent of capture/BEP/test outcome. No signature/authentication claim. |

Copying uses 64 KiB chunks; file content is not loaded wholesale by the archive
writer. Existing diagnostic readers may still read full logs, and test/event
state plus manifest memory grow with input size. Total disk use has no quota in
v1. A file may already have been overwritten before its BEP event is consumed;
copy stability does not prove historical identity. Post-hoc evidence is explicitly
the content available when imported. Concurrent writers to one capture path
remain unsupported.

## Validation evidence

WSL Ubuntu, Python 3.12.3, Bazel 9.3.0, Bazel source
`009e2761ff3e7a2a63167e821dc3bca996c46a4d`. A detached validation worktree and
separate output base were used with four jobs and a 3 GiB JVM heap. The injected
gtest failure was restored in `finally`; the worktree was clean afterward.

| Run | Bazel exit | Result | Wrapper seconds |
|---|---:|---|---:|
| Controlled md5 failure plus sharded strings pass | 3 | Complete bundle, 18 artifact references including full log/XML, console, JSON/binary BEP and profile | 4.495 |
| Restored source, fresh tests | 0 | Two passing targets; complete bundle | 0.821 |
| Cached repeat | 0 | Two cached passing targets; complete bundle | 0.466 |
| Invalid startup option | 2 | Partial bundle with console and report; BEP explicitly not produced | 0.115 |

All original failure-bundle hashes were unchanged after the later successful
and cached invocations reused test-output paths. Moving the directory preserved
verification of all 18 artifact references, its original invocation UUID,
failure excerpts, and a rerun preview selecting only the failed md5 label.
These are individual wall times, not a performance benchmark.

Regression cases exercise full/empty/binary data, content deduplication,
availability states, relative and escaped paths, FIFO/device avoidance,
overwritten attempt paths, duplicate events, copy-time mutation, partial
storage failure, unfinished/tampered manifests, path/symlink escape, portable
reports, effective-option/freshness checks, temporary-BEP retention, complete
console retention, and startup failures. The bviz GUI, a remote CAS service,
power-loss recovery, and the full Bazel suite were not exercised.
Final checks passed: **140 unittest cases plus all 4 truncation checks**;
`git diff --check` was clean. The bundle slice adds 24 regression tests.

Raw bundles and exact runtime observations remain under
`/home/jgett/REPOS/.work/bzlog-bundles-20261025`. They are local validation data
and are not committed to the repository. The next useful layers are CI
expectations and comparable-run diffs over the stored reports; no such commands
are implied by this bundle implementation.
