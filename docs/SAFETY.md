# Safety model: what's proven and what isn't

The core promise is that an agent can act and you won't be left with a corrupted or
half-finished result. This file says what that covers, and each claim points at a test in
[`../proof/`](../proof/) you can run.

## What's proven

A rejected or failed action leaves the world byte-for-byte where it started. In the code domain
that means the file tree is identical after a failed edit (`test_safety_invariant.py`, the
byte-identical keyword cases). A valid action still goes through, so the guarantee isn't a
trivial "always revert" (`test_valid_edit_persists`). A multi-file change that fails reverts
every file, not some of them (`test_multifile_breaking_edit_rolls_back_all`). If a rollback is
interrupted partway, the next session finishes it (`test_crash_recovery_finishes_incomplete_rollback`).

The circuit breaker stops authorizing actions after a streak of failures, and the trip holds
across process restarts rather than resetting on its own (the regime-guard tests).

A full session makes no network egress, enforced by a socket-level tripwire that is itself
tested so it can't quietly become a no-op (`test_provably_local.py`).

## What isn't, yet

Validation depth depends on the target's own tests. A change that is syntactically valid but
semantically wrong is only caught if the repo's tests cover it; anything missed is bounded by
the rollback and by git, not by magic. Concurrent sessions on one repo aren't covered. The edit
executors are Python-only today.

These limits are stated on purpose. The point of the project is a guarantee you can check, not
one you have to take on faith.
