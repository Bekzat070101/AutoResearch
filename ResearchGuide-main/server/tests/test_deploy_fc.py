# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "tools" / "deploy"
sys.path.insert(0, str(DEPLOY))

import fc_safety  # noqa: E402


def test_get_function_other_errors_are_not_missing():
    class Timeout(Exception):
        pass
    assert fc_safety.is_function_missing(Exception("FunctionNotFound: no"))
    assert not fc_safety.is_function_missing(Timeout("read timeout"))
    assert not fc_safety.is_function_missing(RuntimeError("InternalError"))


def test_merge_keeps_remote_db_unless_flag():
    remote = {"QIYAN_DB": "/mnt/nas/qiyan.db", "WECHAT_APP_ID": "wx", "QIYAN_SECRET": "s"}
    local = {"QIYAN_DB": "/tmp/qiyan.db", "LLM_MODEL": "deepseek-flash"}
    got = fc_safety.merge_env(remote, local, db_flag=None, name="qiyan")
    assert got["QIYAN_DB"] == "/mnt/nas/qiyan.db"
    assert got["LLM_MODEL"] == "deepseek-flash"
    flagged = fc_safety.merge_env(remote, local, db_flag="/mnt/nas/other.db", name="qiyan")
    assert flagged["QIYAN_DB"] == "/mnt/nas/other.db"
    created = fc_safety.merge_env(None, local, db_flag=None, name="qiyan")
    assert created["QIYAN_DB"] == "/tmp/qiyan.db"


def test_refuse_ephemeral_production():
    with pytest.raises(SystemExit, match="拒绝发布"):
        fc_safety.refuse_unsafe({"QIYAN_DB": "/tmp/qiyan.db"}, "qiyan", False)
    with pytest.raises(SystemExit, match="QIYAN_SECRET"):
        fc_safety.refuse_unsafe({"QIYAN_DB": "/mnt/nas/qiyan.db"}, "qiyan", False)
    fc_safety.refuse_unsafe(
        {"QIYAN_DB": "/mnt/nas/qiyan.db", "QIYAN_SECRET": "x", "WECHAT_APP_ID": "wx"},
        "qiyan", False,
    )
    fc_safety.refuse_unsafe({"QIYAN_DB": "/tmp/qiyan.db"}, "qiyan", True)
    fc_safety.refuse_unsafe({"QIYAN_DB": "/tmp/qiyan-test.db"}, "qiyan-test", False)


def test_refuse_private_tmp_and_unmounted_data():
    creds = {"QIYAN_SECRET": "x", "WECHAT_APP_ID": "wx"}
    with pytest.raises(SystemExit, match="临时目录"):
        fc_safety.refuse_unsafe({**creds, "QIYAN_DB": "/private/tmp/qiyan.db"}, "qiyan", False)
    with pytest.raises(SystemExit, match="挂载"):
        fc_safety.refuse_unsafe({**creds, "QIYAN_DB": "/data/qiyan.db"}, "qiyan", False)


def test_assert_health_persistent():
    fc_safety.assert_health_persistent({"db": {"ephemeral": False}})
    with pytest.raises(SystemExit, match="ephemeral"):
        fc_safety.assert_health_persistent({"db": {"ephemeral": True}})
    with pytest.raises(SystemExit, match="ephemeral"):
        fc_safety.assert_health_persistent({})


def test_local_defaults_returns_three_keys(monkeypatch):
    import ast
    src = (DEPLOY / "deploy_fc.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ns = {
        "__file__": str(DEPLOY / "deploy_fc.py"),
        "DEFAULT_NAME": "qiyan",
        "Path": Path,
        "load_env": lambda *a, **k: {
            "LLM_API_KEY": "sk-from-env",
            "LLM_BASE_URL": "https://api.deepseek.com/v1",
            "LLM_MODEL": "deepseek-flash",
        },
    }
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"llm_settings", "llm_key", "_local_defaults"}:
            exec(compile(ast.Module([node], []), "deploy_fc.py", "exec"), ns)
    got = ns["_local_defaults"]("qiyan")
    assert got["LLM_API_KEY"] == "sk-from-env"
    assert got["LLM_BASE_URL"]
    assert got["LLM_MODEL"]
    assert "key" in ns["llm_settings"]() and "base_url" in ns["llm_settings"]() and "model" in ns["llm_settings"]()
