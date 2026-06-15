"""Phase 1 safety-core proof: the rollback invariant.

The product's central promise is "never corrupts code." These tests prove it by
driving the REAL `python -m cli intent ... --yes` entrypoint (not a reimplementation)
against edits that are designed to fail validation, and asserting that after any
failed edit the working tree is byte-identical to its pre-edit state.

A control test asserts that a VALID edit still persists — otherwise "always revert"
would trivially pass and prove nothing.

Provider is pinned to `nlp` so these run offline and fast (no MLX model load).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]   # the alpha17 repo root
SRC = REPO_ROOT / "src"

GREET_SRC = 'def greet(name):\n    return "hi " + name\n\ndef main():\n    print(greet("world"))\n'


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_shas(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*.py"))}


def _run_intent(repo: Path, intent: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "cli", "intent", intent, "--repo", str(repo),
         "--yes", "--provider", "nlp"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text(GREET_SRC, encoding="utf-8")
    return tmp_path


# A rename target that is a Python keyword makes the edit produce invalid syntax
# (e.g. `def def(name):`), which the SYNTAX validation level must catch -> rollback.
@pytest.mark.parametrize("keyword", ["def", "class", "return", "import", "for"])
def test_syntax_breaking_edit_rolls_back_byte_identical(repo: Path, keyword: str):
    before = _tree_shas(repo)
    proc = _run_intent(repo, f"rename greet to {keyword}")
    after = _tree_shas(repo)
    assert after == before, (
        f"INVARIANT VIOLATED: tree changed after a failed edit (rename->{keyword}).\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_valid_edit_persists(repo: Path):
    """Control: a valid rename must actually change the tree, proving the
    rollback above is triggered by validation failure, not by always reverting."""
    before = _tree_shas(repo)
    proc = _run_intent(repo, "rename greet to salute")
    after = _tree_shas(repo)
    assert after != before, f"valid edit did not persist.\nstdout:\n{proc.stdout}"
    content = (repo / "app.py").read_text()
    assert "def salute" in content and "def greet" not in content


def test_multifile_breaking_edit_rolls_back_all(tmp_path: Path):
    """A symbol used across two files, renamed to a keyword, must leave BOTH
    files byte-identical after rollback (no partial application)."""
    (tmp_path / "core.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    (tmp_path / "use.py").write_text(
        "from core import base\n\ndef run():\n    return base() + 1\n", encoding="utf-8")
    before = _tree_shas(tmp_path)
    proc = _run_intent(tmp_path, "rename base to class")
    after = _tree_shas(tmp_path)
    assert after == before, (
        f"INVARIANT VIOLATED: multi-file tree changed after failed edit.\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def test_regime_guard_blocks_after_consecutive_validation_failures():
    """The circuit breaker must stop authorizing mutations after a streak of
    validation failures. NOTE: this is in-process state (RegimeGuard is created
    fresh per Workspace); it protects an autonomous loop, not separate CLI calls."""
    from monitoring.regime_guard import RegimeGuard, OperationOutcome

    g = RegimeGuard()
    assert g.clear_to_execute() is True
    for _ in range(3):
        g.update(OperationOutcome(kind="rename_symbol", success=False,
                                  validation_passed=False, files_changed=0))
    assert g.clear_to_execute() is False, "breaker did not trip after 3 validation failures"


def test_regime_guard_trip_persists_across_processes(tmp_path: Path):
    """A tripped breaker must survive across separate processes (fail-closed),
    and `reset()` must clear the persisted trip. Two RegimeGuard instances sharing
    one state file faithfully simulate two CLI invocations."""
    from monitoring.regime_guard import RegimeGuard, OperationOutcome

    sp = tmp_path / ".mobius" / "regime_state.json"
    fail = lambda: OperationOutcome(kind="rename_symbol", success=False,
                                    validation_passed=False, files_changed=0)

    g1 = RegimeGuard(state_path=sp)
    for _ in range(3):
        g1.update(fail())
    assert g1.clear_to_execute() is False

    g2 = RegimeGuard(state_path=sp)          # "next process"
    assert g2.clear_to_execute() is True     # fresh defaults before restore
    g2.restore()
    assert g2.clear_to_execute() is False, "breaker trip did not persist across processes"

    g2.reset()                               # operator clears it
    g3 = RegimeGuard(state_path=sp)
    g3.restore()
    assert g3.clear_to_execute() is True, "reset did not clear the persisted trip"


def test_crash_recovery_finishes_incomplete_rollback(tmp_path: Path):
    """If a rollback crashed mid-flight (journal left in a non-complete phase),
    the next open_repo_workspace() must finish it: staged-good files land in place
    and the journal is cleared. Proves the 'never left half-edited' guarantee."""
    import json
    from workspace.workspace import open_repo_workspace

    good = "def ok():\n    return 1\n"
    corrupt = "def ok(:\n    broken syntax here\n"
    (tmp_path / "app.py").write_text(corrupt, encoding="utf-8")  # simulate half-applied edit

    mob = tmp_path / ".mobius"
    staging = mob / ".rollback_tmp" / "recover"
    staging.mkdir(parents=True)
    (staging / "app.py").write_text(good, encoding="utf-8")      # the staged-good content
    journal = {
        "snapshot_id": "test", "phase": "committing",
        "tmp_dir": str(staging), "trash_dir": str(mob / ".rollback_trash" / "none"),
        "files_to_restore": ["app.py"], "files_to_delete": [],
    }
    (mob / "rollback_journal.json").write_text(json.dumps(journal), encoding="utf-8")

    open_repo_workspace(tmp_path)  # recovery runs here

    assert (tmp_path / "app.py").read_text() == good, "crash recovery did not restore staged file"
    assert not (mob / "rollback_journal.json").exists(), "journal not cleared after recovery"
