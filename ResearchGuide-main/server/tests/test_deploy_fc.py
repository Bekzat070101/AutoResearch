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
