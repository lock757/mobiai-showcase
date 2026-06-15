# Mobiai Safety Model — what is proven, what is not

The product's promise is **"never corrupts your code."** This document states exactly
what that guarantee covers today, backed by tests, and — honestly — where it does not yet
hold. No claim here is aspirational; each "proven" line maps to a test in
`src/tests/test_safety_invariant.py`.

## Architecture (the guarded edit path)
Every edit through `cli intent` (and the bridge's `mobiai_apply_intent`) runs:

1. `open_repo_workspace()` → on open, **recovers any incomplete rollback** from a prior crash.
2. `ws.snapshot("pre_plan")` → full text snapshot, **persisted** to `.mobius` (survives process exit).
3. `plan_executor.execute_plan()` → gated by `regime_guard.clear_to_execute()`.
4. `ValidationRunner.run()` → ladder: structure → **syntax (py_compile / node --check)** → typecheck → lint → targeted-tests.
5. On execution error **or** validation failure → `ws.rollback(snap.id)` via an **atomic, journaled** staging mechanism (`.mobius/.rollback_tmp`, `rollback_journal.json`, phases: staging → committing → complete).

## Proven today (tested)
| Guarantee | Test |
|---|---|
| A failed/rejected edit leaves the tree **byte-identical** to pre-edit | `test_syntax_breaking_edit_rolls_back_byte_identical` (def/class/return/import/for) |
| A **valid** edit actually persists (rollback isn't trivially "always revert") | `test_valid_edit_persists` |
| Multi-file failed edit reverts **all** files (no partial application) | `test_multifile_breaking_edit_rolls_back_all` |
| Circuit breaker blocks mutations after 3 consecutive validation failures | `test_regime_guard_blocks_after_consecutive_validation_failures` |
| Breaker trip **persists across separate processes** (fail-closed); `reset()` clears it | `test_regime_guard_trip_persists_across_processes` |
| A rollback interrupted mid-flight is **finished on next open** (journal replay) | `test_crash_recovery_finishes_incomplete_rollback` |
| A changed file with a **dangling first-party import** is caught dependency-free (zero false positives on third-party / star-export / `__all__` / `__getattr__` / unrelated danglers) | `test_import_resolution.py` (8 tests) |

Also structural, verified by reading the code: snapshots persist across CLI invocations;
rollback is journaled and atomic; recovery runs on every `open_repo_workspace()`.

## Honest limits (NOT yet guaranteed)
1. **Validation depth is shallow without external tools or repo tests.** Verified by reading
   `validation/runner.py`: SYNTAX (`py_compile`) reliably catches syntax breakage. But the
   TYPECHECK level (a) is a *degraded skip* when mypy/pyright isn't installed, and (b) runs
   mypy with `--ignore-missing-imports`, so a **broken import is not flagged even when mypy is
   present**. So a syntactically-valid-but-semantically-wrong edit (broken import, undefined
   name, wrong behavior) is caught only if the repo's own `targeted-tests` cover the changed
   code. For the wedge (regulated teams, often thin test coverage) this is the gap that matters.
   *Partially closed:* a dependency-free import-resolution check now runs at the SYNTAX rung and
   flags **dangling first-party imports in changed files** (e.g. a move/rename that rewrote a
   file's imports wrong), with zero false positives (third-party, star-exports, `__all__`,
   `__getattr__`, and unrelated pre-existing danglers are never flagged). *Remaining gap:* a
   missed importer in an **unchanged** file is not yet caught here (covered by rollback +
   targeted-tests + git); a graph-driven importer scan is the future enhancement. The deeper
   "semantically wrong but syntactically valid" behaviour break is inherent — only repo tests
   catch it. Floor today: any edit that IS caught reverts byte-identically (proven); anything
   missed is bounded by git.
2. **Concurrency unproven.** No test for two edit sessions on one repo simultaneously.
3. **Python only.** The mutation executors operate on Python AST. JS/TS has an indexer but no
   edit executors.

## Mitigation already in place
The repo is under git (baseline commit). Even where an automated guarantee has a gap above,
`git` provides a manual recovery floor. Closing limit 1 (validation depth) is the priority
before pilots.
