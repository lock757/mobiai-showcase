"""agent_safety_harness.py — a domain-agnostic Predict / Contain / Prove wrapper
for autonomous agents.

This is the *generalized* form of the safety model MobiAI proves in the code
domain (see ../proof/). The same three guarantees apply to ANY agent that takes
side-effecting actions — refunds, emails, infra changes, DB writes, code edits:

  Predict  — a circuit breaker stops authorizing actions after a streak of bad
             outcomes, *before* the agent spirals (fail-closed). In MobiAI this
             is the real RegimeGuard, which also persists the trip across
             processes; here it is a minimal in-memory equivalent.
  Contain  — a layered gate, because not every action can be taken back. Each
             action is (1) policy-checked, then (2) if it is irreversible or
             high-stakes, HELD for human approval before it runs — you cannot
             un-send an email, so it is never auto-fired; then (3) if it is
             reversible, executed transactionally and rolled back on a failed
             post-condition. Reversibility is one layer, not the whole claim.
             Autonomy is configurable: "approval" (gate the risky actions) or
             "full" (operator explicitly trades the gate for speed).
  Prove    — a session tripwire fails the instant a real outbound (non-loopback)
             network call is attempted, so "it stayed local" is verifiable, not
             asserted.

What changes per domain is ONLY the set of tools (their apply/undo) and the
policy. The harness below is identical whether the agent issues refunds, edits
code, or scales infrastructure. That is the whole point: safe autonomy is a
property of the control layer, not of any one domain.
"""
from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional


# ── Predict: fail-closed circuit breaker ─────────────────────────────────────

class CircuitBreaker:
    """Trips after `threshold` consecutive bad outcomes. A good outcome resets
    the streak. Once tripped it stays tripped until explicitly reset (an
    operator decision), so a spiraling agent cannot 'recover' itself past the
    guard. Mirrors MobiAI's RegimeGuard semantics."""

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = threshold
        self.consecutive_failures = 0
        self.tripped = False

    def clear_to_execute(self) -> bool:
        return not self.tripped

    def record(self, ok: bool) -> None:
        if ok:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            if self.consecutive_failures >= self.threshold:
                self.tripped = True

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.tripped = False


# ── Contain: a reversible action and the outcome record ──────────────────────

@dataclass
class Action:
    """A proposed side-effecting tool call.

    apply(): perform the effect and return a result.
    undo():  reverse whatever apply() did — only meaningful when `reversible`.
    reversible: can undo() actually restore the prior state? Editing a file or a
             ledger row can be reversed; *sending an email or telling a customer
             something cannot*. Irreversible actions are protected by the
             approval gate BEFORE they run, not by rollback after — because there
             is no "after" you can take back.
    """
    name: str
    args: dict
    apply: Callable[[], Any]
    undo: Callable[[], None]
    reversible: bool = True


@dataclass
class Outcome:
    action: str
    allowed: bool       # did it pass the pre-execution policy gate?
    executed: bool      # did apply() run?
    committed: bool     # did it pass validation and stay applied?
    rolled_back: bool   # was it undone after a failed post-condition?
    reason: str
    held: bool = False  # was it withheld pending human approval (not executed)?
    result: Any = None


# Policy: returns None to allow, or a string reason to block (pre-execution).
Policy = Callable[[Action], Optional[str]]
# Validator: inspects (action, result) post-execution; False -> roll back.
Validator = Callable[[Action, Any], bool]
# NeedsApproval: True if this action must be held for a human before it runs.
NeedsApproval = Callable[[Action], bool]
# Approver: the human-in-the-loop; True authorizes a held action to proceed.
Approver = Callable[[Action], bool]


# ── Prove: network egress tripwire ───────────────────────────────────────────

_LOOPBACK_PREFIXES = ("127.", "::1", "localhost")


class EgressDetected(AssertionError):
    """Raised the instant a non-loopback connection is attempted."""


class SessionTripwire:
    """Patches socket connect paths so any non-loopback egress raises. Same
    technique MobiAI uses in proof/test_provably_local.py."""

    def __init__(self) -> None:
        self.attempts: List[str] = []
        self._orig_connect = None
        self._orig_connect_ex = None
        self._orig_create_connection = None

    @staticmethod
    def _is_loopback(address) -> bool:
        try:
            host = address[0] if isinstance(address, (tuple, list)) else str(address)
        except Exception:
            return False
        host = str(host).lower()
        return any(host.startswith(p) or host == p for p in _LOOPBACK_PREFIXES)

    def _guard(self, address) -> None:
        if not self._is_loopback(address):
            self.attempts.append(str(address))
            raise EgressDetected(f"network egress attempted to {address!r}")

    def __enter__(self) -> "SessionTripwire":
        tw = self
        self._orig_connect = socket.socket.connect
        self._orig_connect_ex = socket.socket.connect_ex
        self._orig_create_connection = socket.create_connection

        def patched_connect(self_sock, address, *a, **k):
            tw._guard(address)
            return tw._orig_connect(self_sock, address, *a, **k)

        def patched_connect_ex(self_sock, address, *a, **k):
            tw._guard(address)
            return tw._orig_connect_ex(self_sock, address, *a, **k)

        def patched_create_connection(address, *a, **k):
            tw._guard(address)
            return tw._orig_create_connection(address, *a, **k)

        socket.socket.connect = patched_connect
        socket.socket.connect_ex = patched_connect_ex
        socket.create_connection = patched_create_connection
        return self

    def __exit__(self, *exc) -> None:
        socket.socket.connect = self._orig_connect
        socket.socket.connect_ex = self._orig_connect_ex
        socket.create_connection = self._orig_create_connection


# ── The harness ──────────────────────────────────────────────────────────────

class SafeAgent:
    """Wraps an agent's action stream with Predict / Contain / Prove.

    Usage: build Actions (each with apply + undo), hand them to .run(). The
    harness decides whether to authorize, executes transactionally, validates,
    and rolls back on failure — recording every decision in an audit trail.
    """

    def __init__(self, policy: Optional[Policy] = None,
                 validator: Optional[Validator] = None,
                 breaker: Optional[CircuitBreaker] = None,
                 autonomy: str = "approval",                       # "approval" | "full"
                 needs_approval: Optional[NeedsApproval] = None,
                 approver: Optional[Approver] = None) -> None:
        self.policy: Policy = policy or (lambda a: None)
        self.validator: Validator = validator or (lambda a, r: True)
        self.breaker = breaker or CircuitBreaker()
        self.autonomy = autonomy
        # By default, anything that cannot be reversed must be approved by a human
        # before it runs. In "full" autonomy this gate is off (the operator's
        # explicit choice to trade safety for speed).
        self.needs_approval: NeedsApproval = needs_approval or (lambda a: not a.reversible)
        self.approver: Optional[Approver] = approver
        self.audit: List[Outcome] = []

    def _log(self, outcome: Outcome) -> Outcome:
        self.audit.append(outcome)
        return outcome

    def run(self, action: Action) -> Outcome:
        # Predict: refuse everything once the breaker is open (fail-closed).
        if not self.breaker.clear_to_execute():
            return self._log(Outcome(action.name, allowed=False, executed=False,
                                     committed=False, rolled_back=False,
                                     reason="circuit breaker open"))

        # Contain (pre): policy gate. A blocked action never runs at all.
        block = self.policy(action)
        if block is not None:
            self.breaker.record(ok=False)  # repeated forbidden attempts == spiraling
            return self._log(Outcome(action.name, allowed=False, executed=False,
                                     committed=False, rolled_back=False,
                                     reason=f"blocked by policy: {block}"))

        # Contain (pre): approval gate. The protection for IRREVERSIBLE actions —
        # you cannot un-send an email, so it is held until a human authorizes it,
        # never auto-fired. Withholding is a *safe* outcome, not a breaker failure.
        if self.autonomy != "full" and self.needs_approval(action):
            approved = bool(self.approver(action)) if self.approver else False
            if not approved:
                return self._log(Outcome(action.name, allowed=True, executed=False,
                                         committed=False, rolled_back=False, held=True,
                                         reason="held for human approval "
                                                "(irreversible / high-stakes)"))
            # approved -> fall through and execute

        # Execute.
        try:
            result = action.apply()
        except Exception as e:  # apply itself failed; assume no partial effect
            self.breaker.record(ok=False)
            return self._log(Outcome(action.name, allowed=True, executed=False,
                                     committed=False, rolled_back=False,
                                     reason=f"apply raised: {e}"))

        # Contain (post): validate the resulting state; roll back if bad.
        if not self.validator(action, result):
            if not action.reversible:
                # No rollback is possible. This is *why* irreversible actions are
                # approval-gated above; surface it honestly rather than pretend.
                self.breaker.record(ok=False)
                return self._log(Outcome(action.name, allowed=True, executed=True,
                                         committed=False, rolled_back=False,
                                         reason="irreversible action failed validation; "
                                                "cannot roll back (approval gate is the safeguard)",
                                         result=result))
            try:
                action.undo()
                rolled = True
                reason = "validation failed -> rolled back"
            except Exception as e:  # rollback failure is the one thing we cannot hide
                self.breaker.record(ok=False)
                return self._log(Outcome(action.name, allowed=True, executed=True,
                                         committed=False, rolled_back=False,
                                         reason=f"validation failed AND rollback raised: {e}",
                                         result=result))
            self.breaker.record(ok=False)
            return self._log(Outcome(action.name, allowed=True, executed=True,
                                     committed=False, rolled_back=rolled,
                                     reason=reason, result=result))

        # Success: the action committed.
        self.breaker.record(ok=True)
        return self._log(Outcome(action.name, allowed=True, executed=True,
                                 committed=True, rolled_back=False,
                                 reason="ok", result=result))
