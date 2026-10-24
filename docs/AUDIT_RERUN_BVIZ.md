# Test execution audit, rerun previews, and bviz overlap

Implemented in WSL Ubuntu on 2026-10-24, following the supplied patch and the
earlier validation work. “Do that” was taken to mean the two proposed first
slices: an execution audit and safe rerun plans. This change adds both; the
larger feature ideas remain a roadmap. Nothing was pushed.

## Implemented behavior

- `--audit-tests`: opt-in, full configured-test inventory, separating final
  status from fresh attempt/cache/missing evidence. Includes explicitly known
  tests that have no results and keeps local/remote cache flags distinct.
- `bzlog rerun REPORT.json`: preview of final failing test labels, using the
  full inventory and original recorded argv/cwd. Preserves test filters,
  config flags, startup options, literal argument boundaries, and cache policy.
- Rerun plans reject incomplete streams and ambiguous original argument
  metadata. They omit explicitly supplied capture destinations, disclose that
  rc files may still supply them, and never execute the command.
- Focused synthetic tests plus reduced real Bazel streams and original
  command metadata. Existing default report output remains compact.

## Overlap with bviz

The named `yemu/bviz` repository returned 404. The matching public project is
[yume-ag/bviz](https://github.com/yume-ag/bviz), inspected at commit
`12d4d9b0fa26eef4a8d259c0bb1edec7b63d6526`. This is a source comparison, not a
claim that its GUI was installed or exercised in WSL.

| Capability | bviz evidence | bzlog direction |
|---|---|---|
| Build and test outcomes | Configured target identity, build failure causes, aborted/skipped targets, final test statuses | Substantial overlap; retain a compact machine-readable contract. |
| Cached and flaky tests | TestSummary action/cache counts, retry/run/shard counts; UI distinguishes cached passes, flaky, failed, incomplete and not-run outcomes | Do not claim novelty for these outcomes. Add per-attempt cache evidence, missing-result accounting and integrity gates. |
| Causal failures | Follows failed target children to actions and configured/unconfigured causes | Share the semantic discipline; keep proven causes distinct from message similarity. |
| Dependency exploration | Merged query/cquery/aquery graph and dependency explanations | Let bviz cover this graphical workflow. A future bzlog hint can link artifacts rather than reimplement graphs. |
| Performance analysis | Trace timeline, flamegraphs, critical path, spawn timing from execution logs | Avoid a second profiling UI. bzlog can retain concise anomaly evidence. |
| Failed-label rerun preview | No corresponding subcommand found in the inspected public CLI; its command enum contains Bazelrc | Keep the conservative headless planner implemented here. This is a bounded source finding, not a universal absence claim. |
| Correlating inputs | Groups profile/BEP/execution logs by invocation ID; query outputs lack that identity | A shared capture manifest is a useful future integration. Preserve uncertainty for query snapshots. |

Sources: [README and input formats](https://github.com/yume-ag/bviz/blob/12d4d9b0fa26eef4a8d259c0bb1edec7b63d6526/README.md),
[outcome ingestion](https://github.com/yume-ag/bviz/blob/12d4d9b0fa26eef4a8d259c0bb1edec7b63d6526/terminal/src/outcomes.rs),
[test outcome presentation](https://github.com/yume-ag/bviz/blob/12d4d9b0fa26eef4a8d259c0bb1edec7b63d6526/ui/graph/src/report/words.rs),
[CLI and invocation grouping](https://github.com/yume-ag/bviz/blob/12d4d9b0fa26eef4a8d259c0bb1edec7b63d6526/cli/src/lib.rs).

### Use both on one invocation

```sh
python3 bzlog.py --audit-tests --record-command test //... \
  --build_event_json_file=run.json \
  --build_event_binary_file=run.bep \
  --profile=run.profile.gz > run.bzlog.json
bviz --bep run.bep --profile run.profile.gz
```

bzlog reads JSON BEP; bviz's documented `--bep` input is binary BEP. The real
controlled-failure run captured both formats and a compressed profile in the
same invocation. No conversion layer or bviz dependency was added. Opening
these files in the bviz GUI was not tested.

## Decisions and alternatives

| Question | Options considered | Choice and reason |
|---|---|---|
| Audit scope | Infer requested tests from patterns; issue a separate query; use only observed BEP identities | Observed identities. A query can change state, expand expensive scope, and disagree with invocation configuration. Missing/filter coverage stays explicit. |
| Execution evidence | Treat PASSED as ran; use summary totals only; reconcile attempt/cache evidence with summary totals | Attempt/cache evidence with summary cross-checks. Fully cached summaries can establish reuse; incomplete/contradictory counts remain visible. |
| Configured tests without results | Call them skipped; infer from console; report unknown except explicit build/skip/halt evidence | Unknown by default. The real tag-filter run demonstrates why a completed target is not a test result. |
| Output size | Always include all tests; truncate to diagnostic limit; opt-in full audit | Opt-in full inventory. Truncating would silently lose failed labels in rerun plans. State/output remain proportional to observed tests. |
| Flag parsing | Maintain a Bazel flag catalog; split a shell command; reconstruct canonical options; match original structured metadata to exact argv | Match original option forms. It preserves repeated flags and separate-value arguments without duplicating Bazel's flag catalog or expanding rc settings. Unknown/redacted boundaries are refused. |
| Narrowing granularity | Guess testcase filters; rerun failed configurations only; rerun failed labels | Labels. Arbitrary runner filters and exact per-configuration CLI reconstruction cannot be inferred safely. All failed configurations are still listed in the plan. |
| Retry selection | Any unsuccessful attempt; only final failure statuses | Final failures. Recovered/flaky tests are excluded; missing final outcomes do not become failures by guesswork. |
| Replay side effects | Automatically run; preserve all capture paths; rewrite to guessed paths; preview with explicit capture flags omitted | Preview only; omit recognized direct capture destinations and disclose rc limitations. No automatic execution or overwrite. |
| Relationship to bviz | Rebuild graphs/profiling; depend on bviz internals; share Bazel capture inputs | Share capture inputs now. Both tools can consume one invocation while retaining their own interfaces. |

Rerun rejection is deliberately conservative: target-pattern files, implicit
default targets, missing original metadata, redacted/hidden options whose
boundaries cannot be matched, and ambiguous repeated command tokens are not
guessed. Original environment, rc files, executable, and source state remain
outside the recorded-argv guarantee. Keeping cache flags also means a planned
rerun is not necessarily fresh execution.

## WSL validation

Bazel 9.3.0 against Bazel source commit
`009e2761ff3e7a2a63167e821dc3bca996c46a4d`, Python 3.12.3. Separate output bases,
four jobs, and a 3 GiB JVM heap. Controlled source mutation occurred only in the
detached validation worktree and was restored in a `finally` block. Both Bazel
source checkouts were clean afterward.

| Case | Bazel exit | Audit / planner observation | Wrapper seconds |
|---|---:|---|---:|
| Fresh md5, numbers, strings | 0 | 3 executed targets; 4 attempts including strings' 2 shards | 4.240 |
| Cached repeat | 0 | 3 cached targets; 4 local cache results; no fresh attempts | 0.569 |
| Two runs per target | 0 | 3 executed targets; 8 unique attempts across runs/shards | 0.519 |
| Tag filter matches none | 4 | 3 configured targets with unknown execution and no test results; no invented pass | 0.420 |
| Injected gtest failure in md5, two peers passing | 3 | Exactly md5 selected; literal environment value and test flags retained; BEP/profile destinations omitted | 4.274 |
| Preview command validated after restoring source | 0 | Only md5 ran, fresh and passing; configuration checksum matched original failure | Not timed separately |

These timings are individual wall-clock observations, not a performance
benchmark. Cache and server state differ. The planner itself did not execute
anything: the separate validation harness deliberately ran the emitted argv
after restoring source and adding a new JSON capture destination.

The regression suite covers duplicate attempts, runs/shards, mixed caches,
summary-only evidence, contradictory counts, remote failures, configured tests
without results, skipped/interrupted targets, event order, multi-configuration
labels, full selection beyond the 25-diagnostic limit, literal shell-like
arguments, repeated output flags, delimiters, incomplete streams, and malformed
report input. Real captures exercise parsing boundaries as emitted by Bazel.
Final checks passed: **116 unittest cases and all 4 truncation checks**;
`git diff --check` was clean. This adds 25 tests to the previous 91-case suite.

Raw inputs, reports, exact argv, timings, binary BEP/profile, and the preview
are in `/home/jgett/REPOS/.work/bzlog-audit-20261024`. Reduced portable fixtures
are under `testdata/bazel-9.3/`; generated launcher environment is not included.
The full Bazel suite, Bazel 10 runtime, remote execution infrastructure, and
bviz GUI integration were not run. Remote-cache cases use synthetic regression
evidence, not a live remote service.

## Additional functionality worth pursuing

1. **Shared invocation bundle:** versioned manifest naming JSON/binary BEP,
   profile, execution log, source revision, capture cwd, and invocation UUID.
   This complements bviz directly and gives bzlog durable evidence ownership.
   Prefer manifest plus immutable blobs before SQLite or an embedded viewer.
2. **Evidence availability inspection:** distinguish empty, missing, unreadable,
   remote-only, and expired log references before attempting remote resolution.
   This explains why a diagnostic lacks detail without inventing a cause.
3. **Expected-test contract:** optionally accept an explicit label/configuration
   inventory and report unseen tests separately. Prefer a supplied snapshot
   over silently running a query or guessing pattern expansion.
4. **Scope-aware invocation diff:** compare failures and execution audits only
   when scopes/configurations are comparable. Unseen targets remain unknown;
   cache-only success should not be presented as fresh validation.
5. **Structured cause export:** expose BEP-proven parent/action causes for
   scripts and possible bviz cross-links, separate from diagnostic similarity.
6. **Runner-aware rerun adapters:** only after a runner declares its filter
   syntax; target-level previews remain the default for custom test runners.

Optimization work still worth measuring: successful-attempt memory, all-event
integrity sets, large log reads, failure-heavy reconciliation, and final UTF-8
output accounting. Do not claim the opt-in full audit solves bounded memory.

One source-review follow-up deserves a narrow fix: bviz distinguishes Bazel's
`ActionExecuted.exit_code` from the spawned process's
`failure_detail.spawn.spawn_exit_code`. bzlog currently exposes the former as
`exit_code`; add explicit scope/naming and a regression fixture before callers
mistake it for the child process code. This was identified, not silently folded
into the execution-audit change.
