"""test_provably_local.py — proof that a full MöbiAI session makes zero network egress.

This is v1 task 3. It installs a tripwire at the socket layer: any attempt to
open a real outbound connection raises immediately and fails the test. Then it
drives a complete offline session end to end and asserts the tripwire never
fired.

What "egress" means here: a connection to any address that is not loopback.
Loopback (127.0.0.1 / ::1) is allowed because the local cockpit API and the
local repo-memory server bind there; that traffic never leaves the machine.

Run:
    PYTHONPATH=src python -m pytest src/tests/test_provably_local.py -v
"""
from __future__ import annotations

import socket
import sys
import tempfile
import unittest
from pathlib import Path


# ── network tripwire ────────────────────────────────────────────────────────────

_LOOPBACK_PREFIXES = ("127.", "::1", "localhost")


class EgressDetected(AssertionError):
    """Raised the instant a non-loopback connection is attempted."""


class NetworkTripwire:
    """Patches socket connect paths so any non-loopback egress raises.

    Covers socket.socket.connect / connect_ex (the path urllib, requests,
    httpx, and raw sockets all funnel through) and socket.create_connection.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
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

    def __enter__(self) -> "NetworkTripwire":
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


# ── full-session exercise ────────────────────────────────────────────────────────

class TestProvablyLocal(unittest.TestCase):
    """Drive a complete offline session under the tripwire."""

    @classmethod
    def setUpClass(cls):
        # Ensure no cloud provider is configured — the test runs the offline path.
        import os
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(var, None)

    def _make_repo(self, root: Path) -> None:
        """A tiny but real repo: two python files with an import edge."""
        (root / "pkg").mkdir(parents=True, exist_ok=True)
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (root / "pkg" / "core.py").write_text(
            '"""Core module."""\n\n\ndef greet(name):\n    return f"hello {name}"\n',
            encoding="utf-8",
        )
        (root / "pkg" / "app.py").write_text(
            '"""App module."""\nfrom pkg.core import greet\n\n\ndef run():\n    return greet("world")\n',
            encoding="utf-8",
        )

    def test_full_session_makes_no_egress(self):
        from mobiai_repo_memory.indexer.build_index import build_index
        from mobiai_repo_memory.memory.query import answer_question
        from mobiai_repo_memory.graph.blast_radius import blast_radius
        from mobiai_repo_memory.analysis.risky_edits import top_risky
        from intent.parser import parse_intent
        from intent.providers import get_provider

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_repo(root)

            with NetworkTripwire() as tw:
                # 1. index the repo
                idx = build_index(str(root))
                self.assertGreaterEqual(len(idx["files"]), 3)

                # 2. ask / explain / risk / blast-radius (the read path)
                ans = answer_question(idx, "where is greet defined?")
                self.assertIn("matches", ans)
                _ = top_risky(idx["files"], 3)
                _ = blast_radius("pkg/core.py", idx["edges"])

                # 3. provider selection must pick an offline provider
                provider = get_provider()
                self.assertTrue(
                    provider.is_available,
                    "offline provider must be available with no API key",
                )
                pname = getattr(provider, "provider_name", type(provider).__name__).lower()
                self.assertNotIn("anthropic", pname)
                self.assertNotIn("openai", pname)

                # 4. parse an intent (offline NL -> typed operation) through the
                #    real provider, which needs the workspace graph
                from workspace.workspace import open_repo_workspace
                ws = open_repo_workspace(root)
                graph = getattr(ws, "graph", None)
                result = provider.parse("rename greet to welcome", graph)
                self.assertIsNotNone(result)

            # tripwire assertion: nothing left the machine
            self.assertEqual(
                tw.attempts, [],
                f"egress attempts during session: {tw.attempts}",
            )

    def test_cloud_provider_is_off_without_key(self):
        """The only egress path (cloud provider) must be inert with no key."""
        from intent.providers import get_provider
        provider = get_provider()
        # an offline provider is selected; it is one of the local ones
        name = getattr(provider, "provider_name", type(provider).__name__).lower()
        self.assertNotIn("anthropic", name)
        self.assertNotIn("openai", name)

    def test_tripwire_actually_trips(self):
        """Sanity: the tripwire must catch a real outbound attempt.

        Without this, a passing egress test could be a no-op. We confirm the
        tripwire raises on a non-loopback connect, and ignores loopback.
        """
        with NetworkTripwire() as tw:
            # loopback is allowed (no raise)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect_ex(("127.0.0.1", 9))  # discard port, refused is fine
            except EgressDetected:
                self.fail("tripwire wrongly flagged loopback as egress")
            finally:
                s.close()
            # non-loopback must raise
            s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            with self.assertRaises(EgressDetected):
                s2.connect(("93.184.216.34", 80))  # example.com, never actually reached
            s2.close()
        self.assertEqual(len(tw.attempts), 1)


if __name__ == "__main__":
    unittest.main()
