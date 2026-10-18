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
