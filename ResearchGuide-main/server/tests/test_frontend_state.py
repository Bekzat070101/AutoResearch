# -*- coding: utf-8 -*-
"""20-R02 / R03：用 node 执行 web/js/app.js 里的原函数（tools/verify/verify_frontend_state.js）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "verify" / "verify_frontend_state.js"


def _run(which: str) -> None:
    r = subprocess.run(["node", str(SCRIPT), which], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_draft_concurrent_sync():
    _run("drafts")


def test_prefs_reload_failure_blocks_replace():
    _run("prefs")


def test_boot_restores_directions():
    _run("boot")


def test_draft_revision_never_regresses():
    _run("rev")


def test_legacy_empty_draft_preserves_failed_clear():
    _run("legacy")


def test_conflict_action_survives_task_reopen():
    _run("reopen")


def test_draft_failure_shows_retry_notice():
    _run("retry")
def test_direction_writes_preserve_fresh_trail():
    _run("r4-dirs")


def test_boot_preserves_explicit_trail_state():
    _run("r4-boot")


def test_prefs_failure_has_visible_retry():
    _run("r4-retry")


def test_direction_write_aborts_after_portrait_switch():
    _run("direction-context")


def test_direction_rejection_aborts_after_lifecycle_change():
    _run("direction-context-errors")


def test_direction_save_completion_aborts_before_reading_current_prefs():
    _run("direction-context-save")


def test_direction_ui_continuation_aborts_after_portrait_switch():
    _run("direction-context-ui")
