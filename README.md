# MobiAI: a safety layer for autonomous agents

Most of the work on AI agents goes into getting them to act. The harder problem is letting them
act without something going badly wrong: the refund they shouldn't issue, the record they
shouldn't delete, the data they shouldn't leak, the code they shouldn't break. MobiAI sits
between an agent and the world and makes that part safe.

It does three things, and this repo backs each one with tests you can run.

**It predicts trouble.** A behavioral monitor watches the agent's outcomes and trips a circuit
breaker once it starts failing, before one bad run turns into a worse one. The trip is
fail-closed and survives a process restart, so a spiraling agent can't retry its way back in.

**It contains actions.** Not everything can be undone, so containment is layered. A safe action
just runs. A reversible one runs inside a transaction and gets rolled back if it fails a check
afterward. An irreversible or high-stakes one, like a sent email or anything you tell a
customer, is held for a human to approve before it ever fires. You set the autonomy level: gate
the risky actions, or run full-auto and own that tradeoff on purpose. Rollback is one layer
here, not the whole story.

**It proves nothing left the machine.** A full session runs with zero network egress. There's a
tripwire at the socket layer that fails the build the moment anything reaches for a non-loopback
address, and a separate test for the tripwire itself so the guarantee can't quietly rot into a
no-op.

## The same layer works across domains

The control layer doesn't change when the problem does. `domains/agent_safety_harness.py` is the
same file whether it's wrapping a support agent, a payments agent, or a code-editing agent. What
changes per domain is the tools and the policy, nothing else. That's the real claim: safe
autonomy is a property of the layer, not of any one use case.

The repo runs it against three. A customer-support agent, where over-limit refunds get blocked,
refund-draining loops get stopped, irreversible emails are held for approval, and an attempt to
phone customer data out gets caught. A generic tool-calling agent. And live code editing, which
is the strict case.

I lead with code because code is the one place you can't fudge the result. After a bad edit the
files are byte-for-byte what they were, or they aren't. There's no "well, it handled that pretty
well" the way there is with a support transcript. So the most checkable domain gets the
strictest proof, and the same harness runs the rest.

## Run the proof yourself

The three domains, on the shared harness. Each "should block" test is paired with a control that
lets a legitimate action through, so a harness that just blocks everything can't pass and look
good doing it.

```
$ python3 -m pytest domains/
.......................
23 passed in 0.02s
```

The code case drives the real CLI against edits built to fail (renaming a function to a Python
keyword, which produces invalid syntax), then checks the file tree is untouched. A control test
confirms a valid edit still lands, so "always revert" can't pass for free.

```
$ PYTHONPATH=src python3 -m pytest proof/ -v

test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[def]    PASSED
test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[class]  PASSED
test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[return] PASSED
test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[import] PASSED
test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[for]    PASSED
test_safety_invariant.py::test_valid_edit_persists                                    PASSED
test_safety_invariant.py::test_multifile_breaking_edit_rolls_back_all                 PASSED
test_safety_invariant.py::test_regime_guard_blocks_after_consecutive_validation_failures PASSED
test_safety_invariant.py::test_regime_guard_trip_persists_across_processes            PASSED
test_safety_invariant.py::test_crash_recovery_finishes_incomplete_rollback            PASSED
test_provably_local.py::test_cloud_provider_is_off_without_key                        PASSED
test_provably_local.py::test_full_session_makes_no_egress                             PASSED
test_provably_local.py::test_tripwire_actually_trips                                  PASSED

13 passed in 118.10s
```

The two files under [`proof/`](proof/) are short and worth reading. They're the honest core of
the claim.

## What's here and what isn't

This is a public showcase: the architecture, the safety model, and the tests. The parts that
took the longest, the predictive engine and the planning core, aren't in here.

If you're shipping an agent that can do something you can't take back, that's the problem I work
on. block61625@gmail.com, GitHub [@lock757](https://github.com/lock757).
