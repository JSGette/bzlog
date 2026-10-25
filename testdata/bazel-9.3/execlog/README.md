# Reduced execution-log fixtures

Captured with Bazel 9.3.0 on 2026-10-25 from the Bazel source worktree at
`009e2761ff3e7a2a63167e821dc3bca996c46a4d`, building
`//src/test/cpp/util:md5_test`. Converted from real compact logs using
`//src/tools/execlog:converter` from that source tree.

- `baseline.json`: two selected spawns from the first build, without cache hits.
- `disk-hit.json`: corresponding spawns from a fresh output base sharing a
  populated disk cache, both cache hits.
- `changed.json`: a source comment, `--copt=-DBZLOG_EXAMPLE=2` instead of `=1`,
  and `--action_env=BZLOG_EXAMPLE=after` instead of `=before` changed.

Only md5_test's CppCompile and CppLink records are retained. Arguments are
reduced to the controlled define and source filename; environment is reduced
to `BZLOG_EXAMPLE`; inputs are reduced to `md5_test.cc`. Output labels/paths,
recorded cache flags, runner, statuses, timeout, and digests are unchanged.
The action digests describe the original complete actions and cannot be
recomputed from these reduced fixtures. Other environment/command data and
the raw compact logs are deliberately excluded.

The `}{` boundary between pretty objects is intentional: it matches Bazel's
converter output, which does not insert a newline between messages.
