# Real Bazel regression corpus

Captured in WSL Ubuntu with Bazel 9.3.0 against JSGette/bazel commit
`009e2761ff3e7a2a63167e821dc3bca996c46a4d` on 2026-10-24.

- `cpp-cached`: cached md5, numbers, and sharded strings tests.
- `compiler-failure`: an injected `#error` in md5_test.cc.
- `gtest-failure`: an injected failing `EXPECT_EQ(1, 2)` case.
- `flaky-standalone`: a run-under script failed once, then ran the real md5 test.
- `missing-target`: a nonexistent label in the real C++ test package.
- `audit-repeated-shards`: two fresh runs of md5, numbers, and sharded strings;
  eight attempts across three configured targets.
- `audit-filtered`: those three targets with a tag filter matching none. Bazel
  emits configured tests but no results; the audit must retain unknown execution.
- `audit-original-command.json`: direct original command metadata and argv from
  the controlled audit failure. Launcher environment and unrelated metadata
  are removed, paths normalized, and the literal test environment value retained.

These are reduced fixtures, not byte-for-byte originals. Event identities,
children, outcomes, and terminal markers are preserved. Command-line and
progress payloads are omitted; workspace paths are normalized. Log pointers
are replaced with inline excerpts captured in each live report. Test XML is
omitted because later test runs replace those files. No external filesystem,
network access, or Bazel installation is needed to replay the corpus.

The additional audit streams retain `configured` payloads. The original streams and contemporaneous reports remain under
`/home/jgett/REPOS/.work/bzlog-validation-20261024` on the validation machine.
Audit captures are under `/home/jgett/REPOS/.work/bzlog-audit-20261024`.
