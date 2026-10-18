# -*- coding: utf-8 -*-
"""uptime 工作流内嵌 Python：版本戳按 wN 比，持久化必须是 JSON false。"""
from __future__ import annotations

import builtins
import json
import re
import textwrap
from pathlib import Path

import pytest

YML = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "uptime.yml"


def _script() -> str:
    text = YML.read_text(encoding="utf-8")
    m = re.search(r"python - <<'PY'\n(.*?)\n\s*PY\n", text, re.S)
    return textwrap.dedent(m.group(1))


def _run(monkeypatch, health, html, require=""):
    files = {"/tmp/qiyan-health.json": json.dumps(health), "/tmp/qiyan-pages.html": html}
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if path in files:
            import io
            return io.StringIO(files[path])
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setenv("QIYAN_REQUIRE_PERSISTENT", require)
    try:
        exec(compile(_script(), "uptime", "exec"), {"__name__": "__main__"})
    except SystemExit as e:
        return 0 if e.code in (None, 0) else 1
    return 0


def _page(stamp):
    return f"<footer class='build-stamp'>界面版本 <b>{stamp}</b></footer>"


@pytest.mark.parametrize("build,stamp,require,db,code", [
    ("abc1234 w84", "w84", "", {"ephemeral": False}, 0),
    ("abc1234 w84", "def5678 w84", "", {"ephemeral": False}, 0),
    ("561362f w74", "w84", "", {"ephemeral": False}, 1),
    ("561362f", "w84", "", {"ephemeral": False}, 1),
    ("abc1234 w84", "", "", {"ephemeral": False}, 1),
    ("w84 w85", "w84", "", {"ephemeral": False}, 1),
    ("abc1234 w84", "w84 w85", "", {"ephemeral": False}, 1),
    ("abc1234 w84", "w84", "1", {"ephemeral": False}, 0),
    ("abc1234 w84", "w84", "1", {"ephemeral": True}, 1),
    ("abc1234 w84", "w84", "1", {}, 1),
    ("abc1234 w84", "w84", "1", {"ephemeral": "false"}, 1),
])
def test_real_build_and_persistence_contract(monkeypatch, build, stamp, require, db, code):
    health = {"ok": True, "db": db, "checks": {"build": build}}
    assert _run(monkeypatch, health, _page(stamp), require) == code
