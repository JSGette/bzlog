# Lessons learned

Re-read and consolidated on 2026-10-24 from the earlier “Analyze bzlog Against
Bazel 10” review and the existing local PLAN.md. The original plan was copied
unchanged to the native WSL checkout and remains untracked.

1. **Correctness before compression.** Measure cause retention, false merges,
   evidence availability, and useful next actions alongside output size.
2. **Missing evidence establishes no equivalence.** Identical excerpts are not
   identical full logs. Keep BEP causal edges, diagnostic similarity, and unknown
   relationships distinct, especially across configurations.
3. **Final test summaries outrank attempts.** A failed attempt followed by a pass
   can be flaky. Do not cancel retries early or demote an actual regression based
   only on historical suspicion.
4. **Build outcome and stream integrity are independent.** A successful process
   may leave an incomplete stream. A failed process can produce a complete one.
   A final marker alone does not prove that every announced child was retained.
5. **Proto JSON omits default scalars.** In an existing ExitCode message, omitted
   code means zero. In TargetComplete, omitted success means false. Absence of a
   containing message does not establish its scalar defaults.
6. **A filename is not an invocation identity.** Reused BEP files can cancel new
   work if read before Bazel truncates them. Test logs/XML are also overwritten
   by later runs: preserve important evidence during the invocation.
7. **The output is an investigation entry point.** Excerpts must disclose
   truncation/distillation. Unique temporary evidence files prevent collisions,
   but they are not a durable full-evidence archive.
8. **Replay claims need a scope.** Original argv/cwd can be retained; reproducing
   configuration also needs rc files, environment, executable, and source state.
   Do not invent a gtest/Java-style filter for arbitrary test runners.
9. **Event frequency matters.** Rebuilding all test diagnostics on every progress
   event makes fail-fast quadratic even when everything passes. Gate expensive
   work on events that can change the relevant decision, then prove event-order
   and retry behavior is preserved.
10. **Bazel cache counters describe different populations.** Do not add total
    actions to spawn counts. Local action-cache reuse is separate from disk and
    remote spawn-cache hits. DIGEST_MISMATCH alone is not non-hermeticity.
11. **Invocation diffs require comparable scope.** Never report “fixed” because
    a later partial build omitted a target. Version strong and weak diagnostic
    fingerprints; line movement can be the same failure at a new location.
12. **Use actual producer output.** The WSL corpus exposed omitted-default bugs
    missed by handcrafted fixtures that explicitly wrote zero/false. Keep both
    narrow synthetic regressions and reduced, attributed real streams.

13. **Passed does not mean freshly executed.** Keep final status, observed
    attempts, cache provenance, and missing results separate. A configured
    target can survive tag filtering while producing no test result; absence
    alone does not establish the reason. Test-attempt evidence does not prove
    individual test cases inside a custom runner executed.
14. **A narrowed command needs argument boundaries.** An option value can look
    exactly like a target pattern. Match Bazel's original parsed option forms
    to recorded argv, reject ambiguity, and select failures from an untruncated
    inventory. Quote only for display; never parse a command back with shell
    splitting or execute a generated preview automatically.
15. **Inspect overlap in source, not just feature lists.** bviz already handles
    configured target outcomes, flaky tests, cache summaries, and causal edges.
    bzlog's useful focus is compact headless evidence and explicit uncertainty;
    graphical dependency and performance exploration can use bviz captures.

The validation record documents alternatives, limitations, and the next work
in order; it does not treat deferred features as implemented.
