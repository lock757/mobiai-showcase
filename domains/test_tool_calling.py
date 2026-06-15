"""Proof: the Predict / Contain / Prove safety model holds for a generic
side-effecting agent — no code, no files, just abstract tools mutating a world.

This is the domain-neutral demonstration. test_support_agent.py instantiates the
SAME harness for a concrete customer-support agent; nothing in the harness
changes between them. That invariance is the claim: safe autonomy is a property
of the control layer, not of the domain.

Structure mirrors ../proof/test_safety_invariant.py: every "must block" test is
paired with a control proving a legitimate action still goes through, so a
trivially over-cautious harness ("block everything") cannot pass and prove
nothing.
"""
from __future__ import annotations

import copy
import socket
import unittest

from agent_safety_harness import (
    SafeAgent, Action, CircuitBreaker, SessionTripwire, EgressDetected,
)


# A tiny mutable "world" standing in for any external system the agent acts on.
def make_world():
    return {"balance": 0, "events": []}


def adjust_balance(world, amount):
    """A reversible tool: change a balance, with an exact inverse."""
    def apply():
        world["balance"] += amount
        world["events"].append(("adjust", amount))
        return world["balance"]

    def undo():
        world["balance"] -= amount
        world["events"].pop()

    return Action(name="adjust_balance", args={"amount": amount},
                  apply=apply, undo=undo)


class TestContainment(unittest.TestCase):
    def test_dangerous_action_blocked_pre_execution(self):
        """Policy blocks an over-limit action BEFORE it runs; world untouched."""
        world = make_world()
        before = copy.deepcopy(world)
        agent = SafeAgent(policy=lambda a: None if a.args["amount"] <= 100
                          else "amount exceeds limit")

        out = agent.run(adjust_balance(world, 1000))

        self.assertFalse(out.allowed)
        self.assertFalse(out.executed)
        self.assertIn("exceeds limit", out.reason)
        self.assertEqual(world, before, "blocked action must leave the world byte-identical")

    def test_failed_validation_rolls_back_to_identical_state(self):
        """Action runs, fails a post-condition, and is rolled back so the world
        is exactly as it started."""
        world = make_world()
        adjust_balance(world, 40).apply()          # legit prior state: balance 40
        before = copy.deepcopy(world)
        # Validator rejects any state where balance > 50.
        agent = SafeAgent(validator=lambda a, r: world["balance"] <= 50)

        out = agent.run(adjust_balance(world, 25))  # would push balance to 65

        self.assertTrue(out.executed)
        self.assertFalse(out.committed)
        self.assertTrue(out.rolled_back)
        self.assertEqual(world, before, "rolled-back action must restore identical state")

    def test_valid_action_commits(self):
        """Control: a legitimate action actually changes the world and commits —
        otherwise 'always roll back' would pass the tests above for free."""
        world = make_world()
        before = copy.deepcopy(world)
        agent = SafeAgent(policy=lambda a: None if a.args["amount"] <= 100 else "too big",
                          validator=lambda a, r: world["balance"] <= 50)

        out = agent.run(adjust_balance(world, 30))

        self.assertTrue(out.committed)
        self.assertFalse(out.rolled_back)
        self.assertNotEqual(world, before, "a valid action must persist")
        self.assertEqual(world["balance"], 30)


def irreversible_action(world, amount):
    """Like adjust_balance, but flagged irreversible — undo cannot truly restore
    the world (stands in for 'sent an email', 'told a customer', etc.)."""
    act = adjust_balance(world, amount)
    return Action(name="irreversible", args={"amount": amount},
                  apply=act.apply, undo=act.undo, reversible=False)


class TestApprovalGate(unittest.TestCase):
    def test_irreversible_action_held_without_approval(self):
        world = make_world()
        agent = SafeAgent()                       # approval mode, no approver
        out = agent.run(irreversible_action(world, 10))
        self.assertTrue(out.held)
        self.assertFalse(out.executed)
        self.assertEqual(world["balance"], 0, "irreversible action must not auto-fire")

    def test_approved_irreversible_action_runs(self):
        world = make_world()
        agent = SafeAgent(approver=lambda a: True)
        out = agent.run(irreversible_action(world, 10))
        self.assertTrue(out.committed)
        self.assertEqual(world["balance"], 10)

    def test_held_action_is_not_a_breaker_failure(self):
        """Withholding for approval is a SAFE outcome — it must not count toward
        tripping the breaker, or normal caution would lock the agent out."""
        world = make_world()
        agent = SafeAgent(breaker=CircuitBreaker(threshold=3))
        for _ in range(5):
            agent.run(irreversible_action(world, 10))
        self.assertTrue(agent.breaker.clear_to_execute(), "holds must not trip the breaker")


class TestPredict(unittest.TestCase):
    def test_breaker_trips_after_streak_and_refuses_further_actions(self):
        """After 3 bad outcomes the breaker opens and refuses even a legal
        action (fail-closed) — the spiral is stopped, not ridden out."""
        world = make_world()
        agent = SafeAgent(
            policy=lambda a: None if a.args["amount"] <= 100 else "too big",
            breaker=CircuitBreaker(threshold=3),
        )
        for _ in range(3):                      # three forbidden attempts
            agent.run(adjust_balance(world, 999))

        self.assertFalse(agent.breaker.clear_to_execute(), "breaker should be open")

        out = agent.run(adjust_balance(world, 10))  # now a perfectly legal action
        self.assertFalse(out.allowed)
        self.assertEqual(out.reason, "circuit breaker open")
        self.assertEqual(world["balance"], 0, "nothing should have landed")

    def test_good_outcome_resets_the_streak(self):
        """Control: a successful action resets the failure streak, so the
        breaker only trips on a *consecutive* run of bad outcomes."""
        world = make_world()
        agent = SafeAgent(
            policy=lambda a: None if a.args["amount"] <= 100 else "too big",
            breaker=CircuitBreaker(threshold=3),
        )
        agent.run(adjust_balance(world, 999))   # fail 1
        agent.run(adjust_balance(world, 999))   # fail 2
        agent.run(adjust_balance(world, 10))    # success -> reset
        agent.run(adjust_balance(world, 999))   # fail 1 again
        self.assertTrue(agent.breaker.clear_to_execute(), "streak should have reset")


class TestProve(unittest.TestCase):
    def test_session_makes_no_egress(self):
        """A full run of the harness attempts no outbound network connection."""
        world = make_world()
        agent = SafeAgent(policy=lambda a: None if a.args["amount"] <= 100 else "too big")
        with SessionTripwire() as tw:
            agent.run(adjust_balance(world, 30))
            agent.run(adjust_balance(world, 999))
        self.assertEqual(tw.attempts, [], f"unexpected egress: {tw.attempts}")

    def test_tripwire_actually_trips(self):
        """Sanity: the egress guarantee is not a no-op — a real outbound attempt
        is caught, loopback is allowed."""
        with SessionTripwire() as tw:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect_ex(("127.0.0.1", 9))      # loopback: allowed
            except EgressDetected:
                self.fail("tripwire wrongly flagged loopback")
            finally:
                s.close()
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(EgressDetected):
                s2.connect(("93.184.216.34", 80))   # non-loopback: must raise
            s2.close()
        self.assertEqual(len(tw.attempts), 1)


class TestHardening(unittest.TestCase):
    """Edge cases that separate a real safety layer from a demo."""

    def test_apply_exception_is_contained(self):
        """If a tool's apply() raises (before committing), the harness reports it
        as not-executed, counts it against the breaker, and the world is clean."""
        world = make_world()
        before = copy.deepcopy(world)

        def boom():
            raise RuntimeError("tool blew up")

        action = Action(name="boom", args={}, apply=boom, undo=lambda: None)
        agent = SafeAgent()
        out = agent.run(action)

        self.assertFalse(out.executed)
        self.assertFalse(out.committed)
        self.assertIn("apply raised", out.reason)
        self.assertEqual(world, before)
        self.assertEqual(agent.breaker.consecutive_failures, 1)

    def test_rollback_failure_is_surfaced_not_hidden(self):
        """The one thing a safety layer must never do is silently swallow a
        failed rollback. If undo() raises, the outcome says so explicitly."""
        world = make_world()

        def apply():
            world["balance"] += 10
            return world["balance"]

        def undo():
            raise RuntimeError("undo is broken")

        action = Action(name="leaky", args={}, apply=apply, undo=undo)
        agent = SafeAgent(validator=lambda a, r: False)  # force a rollback
        out = agent.run(action)

        self.assertTrue(out.executed)
        self.assertFalse(out.committed)
        self.assertFalse(out.rolled_back)
        self.assertIn("rollback raised", out.reason)

    def test_operator_reset_reopens_breaker(self):
        """A tripped breaker only clears on an explicit operator reset, never on
        its own."""
        agent = SafeAgent(breaker=CircuitBreaker(threshold=2))
        bad = Action(name="x", args={}, apply=lambda: (_ for _ in ()).throw(ValueError()),
                     undo=lambda: None)
        agent.run(bad)
        agent.run(bad)
        self.assertFalse(agent.breaker.clear_to_execute())
        agent.breaker.reset()
        self.assertTrue(agent.breaker.clear_to_execute())

    def test_every_decision_is_audited(self):
        """Every run() — allowed, blocked, or rolled back — lands in the audit
        trail. No silent actions."""
        world = make_world()
        agent = SafeAgent(policy=lambda a: None if a.args["amount"] <= 100 else "too big",
                          validator=lambda a, r: world["balance"] <= 50)
        agent.run(adjust_balance(world, 30))    # commit
        agent.run(adjust_balance(world, 999))   # blocked
        agent.run(adjust_balance(world, 40))    # rolled back (would hit 70)

        self.assertEqual(len(agent.audit), 3)
        self.assertEqual([o.committed for o in agent.audit], [True, False, False])
        self.assertEqual([o.allowed for o in agent.audit], [True, False, True])
        self.assertTrue(agent.audit[2].rolled_back)


if __name__ == "__main__":
    unittest.main()
