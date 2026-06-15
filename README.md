# MobiAI — a safety layer for autonomous agents

**Let an AI agent act autonomously — and *prove* it can't do the irreversible thing you're
afraid of.**

The hard problem in agentic AI isn't getting an agent to act. It's being able to *let* it act
unsupervised without risking a catastrophe — the refund it shouldn't issue, the record it
shouldn't delete, the data it shouldn't leak, the code it shouldn't break. MobiAI is the
control layer that makes autonomy safe to deploy, built on three guarantees that are
**demonstrated by tests, not promised in a pitch deck**:

- **Predict** — a behavioral monitor (Harbinger / RegimeGuard) trips a circuit breaker when an
  agent starts failing, *before* it spirals. After a streak of bad outcomes it stops
  authorizing actions — and the trip survives across processes (fail-closed).
- **Contain** — a layered gate, because not every action can be taken back. Each action is
  policy-checked; if it's **irreversible or high-stakes** (you can't un-send an email or
  un-tell a customer something) it's **held for human approval before it runs**, never
  auto-fired; if it's reversible and a post-condition later fails, it's rolled back to exactly
  where it started. Autonomy is configurable — gate the risky actions, or let it run full-auto
  as an explicit operator choice. Reversibility is one layer, not the whole promise.
- **Prove** — a full session makes **zero network egress**. A socket-layer tripwire fails the
  build the instant anything non-loopback is dialed — and the tripwire itself is tested, so the
  guarantee can't silently become a no-op.

### The model is agent-agnostic — and that's proven, not claimed

The same Predict / Contain / Prove loop wraps any agent that takes side-effecting actions. This
repo demonstrates the **identical control layer** (`domains/agent_safety_harness.py`,
unchanged) across three different domains:

| Domain | What's contained | Proof |
|---|---|---|
| **Customer-support agent** | over-limit refunds, refund-draining loops, *irreversible emails held for approval*, data exfiltration | [`domains/test_support_agent.py`](domains/test_support_agent.py) |
| **Generic tool-calling agent** | any policy-violating or non-reversible action | [`domains/test_tool_calling.py`](domains/test_tool_calling.py) |
| **Live code editing** (the hardest) | edits that would corrupt the repo | [`proof/`](proof/) |

What changes per domain is only the *tools and the policy*. The harness is identical. That
invariance is the product: safe autonomy is a property of the control layer, not of any one
domain.

**Why prove it in code at all?** Because code is the lie-detector domain — correctness is
binary. After a bad edit the file tree is *byte-identical* to before, or it isn't; you can't
hand-wave that the way you can a "did the support agent handle it well?" demo. So MobiAI proves
the model in the hardest domain to fake, then shows the same harness containing a support agent.

> This repo is a **public showcase**: the architecture, the safety model, and the proof tests.
> The differentiated internals (the Harbinger predictive engine, the Prometheus
> planner→synthesizer→judge orchestrator) are kept in a private core.

---

## The proof (run it yourself)

**Generality, on the same harness — `domains/`.** The identical control layer contains a
generic tool-calling agent and a customer-support agent. Each "must block" test is paired with a
control proving a legitimate action still goes through, so an over-cautious "block everything"
harness can't pass and prove nothing.

```
$ python3 -m pytest domains/ -v

domains/test_support_agent.py::TestRefundContainment ...... (over-limit blocked / cap rolled    PASSED
                                                             back / legit refund commits)
domains/test_support_agent.py::TestCompromisedAgent ....... (refund-drain loop breaker-stopped) PASSED
domains/test_support_agent.py::TestIrreversibleActionGate . (email HELD for approval, not sent  PASSED
                                                             / approved -> sent / full-auto)
domains/test_support_agent.py::TestNoDataExfiltration ..... (approved email no egress /          PASSED
                                                             phone-home caught)
domains/test_tool_calling.py::TestContainment ............. (block / rollback / commit)          PASSED
domains/test_tool_calling.py::TestApprovalGate ............ (irreversible held / approved runs / PASSED
                                                             hold is not a breaker failure)
domains/test_tool_calling.py::TestPredict ................. (breaker trips / streak resets)      PASSED
domains/test_tool_calling.py::TestProve ................... (no egress / tripwire trips)         PASSED
domains/test_tool_calling.py::TestHardening ............... (apply-raise / rollback surfaced /   PASSED
                                                             operator reset / full audit)
================== 23 passed in 0.02s ==================
```

**The hardest domain — live code, `proof/`.** The central promise *"an agent can edit your code
and it will never be left corrupted"* is proven by driving the **real** CLI against edits
engineered to fail, then asserting the tree is byte-identical afterward. A control test proves a
*valid* edit still persists.

```
$ PYTHONPATH=src python3 -m pytest proof/ -v

proof/test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[def]    PASSED
proof/test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[class]  PASSED
proof/test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[return] PASSED
proof/test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[import] PASSED
proof/test_safety_invariant.py::test_syntax_breaking_edit_rolls_back_byte_identical[for]    PASSED
proof/test_safety_invariant.py::test_valid_edit_persists                                    PASSED
proof/test_safety_invariant.py::test_multifile_breaking_edit_rolls_back_all                 PASSED
proof/test_safety_invariant.py::test_regime_guard_blocks_after_consecutive_validation_failures PASSED
proof/test_safety_invariant.py::test_regime_guard_trip_persists_across_processes            PASSED
proof/test_safety_invariant.py::test_crash_recovery_finishes_incomplete_rollback            PASSED
proof/test_provably_local.py::test_cloud_provider_is_off_without_key                        PASSED
proof/test_provably_local.py::test_full_session_makes_no_egress                             PASSED
proof/test_provably_local.py::test_tripwire_actually_trips                                  PASSED

================== 13 passed in 118.10s ==================
```

Read the two test files in [`proof/`](proof/) — they're short, and they're the honest core of
the claim:
- [`test_safety_invariant.py`](proof/test_safety_invariant.py) — byte-identical rollback,
  multi-file atomicity, circuit-breaker persistence, crash recovery.
- [`test_provably_local.py`](proof/test_provably_local.py) — the zero-egress tripwire (and the
  meta-test that the tripwire really trips).

---

## How it works

```
┌──────────────────────────────────────────────────────────────────┐
│  Natural-language intent  ("rename greet to welcome")             │
├──────────────────────────────────────────────────────────────────┤
│  1. Regime Gate (Harbinger)   — is it safe to continue?           │
│  2. Intent Resolution         — NL → typed, validated operation   │
│  3. Operation Execution       — semantic, AST-aware transform      │
│  4. Validation Ladder         — structure → syntax → tests         │
│  5. Rollback (on any failure) — restore byte-identical tree        │
│  6. Feedback to Harbinger     — update regime / breaker state      │
└──────────────────────────────────────────────────────────────────┘
   Everything runs on 127.0.0.1. No network egress. Read-only by default.
```

- **Local-first / zero-egress** — FastAPI backend bound to loopback; offline NL provider so it
  runs with no API key and nothing leaves the machine. Buyer fit: teams who can't send code or
  data to a cloud (compliance, regulated, NDA work).
- **Semantic, not text-diff** — operations are AST-aware (rename, move, extract, delete-symbol,
  add-docstring), disambiguated against a repo dependency graph; ambiguous edits *abstain*
  rather than guess.
- **Human-in-the-loop by default** — dry-run preview → approve → guarded apply → one-click undo
  → exportable audit log.

See [`docs/SAFETY.md`](docs/SAFETY.md) for the safety model in full.

---

## Why it matters

Autonomy is gated on trust. The reason teams don't let agents act unsupervised isn't capability
— it's that one irreversible mistake (a bad refund, a dropped table, leaked data, corrupted
code) is unacceptable, and "we tuned the prompt" is not a control. MobiAI is the missing control
layer: predict the failure, contain the action, prove nothing leaked — demonstrated, runnable,
across domains, on a control layer that doesn't change when the domain does.

📫 **block61625@gmail.com** · GitHub [@lock757](https://github.com/lock757)
