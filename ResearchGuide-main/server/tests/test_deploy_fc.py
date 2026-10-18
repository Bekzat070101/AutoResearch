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
        fc_safety.assert_db_under_nas("/data/qiyan.db", [])


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


GOOD_ENV = {"QIYAN_DB": "/mnt/nas/qiyan.db", "QIYAN_SECRET": "x", "WECHAT_APP_ID": "wx"}
GOOD_HEALTH = {"db": {"ephemeral": False, "writable": True}}


def _cfg(*dirs):
    return {"nas_config": {"mount_points": [{"mount_dir": d} for d in dirs]}}


@pytest.mark.parametrize("cfg,db,url,health,ok", [
    (_cfg(), "/mnt/nas/qiyan.db", "https://x", GOOD_HEALTH, False),
    ({}, "/mnt/nas/qiyan.db", "https://x", GOOD_HEALTH, False),
    (_cfg("/mnt/nas2"), "/mnt/nas/qiyan.db", "https://x", GOOD_HEALTH, False),
    (_cfg("/mnt/nas"), "/mnt/nas/qiyan.db", "https://x", {"db": {"ephemeral": False, "writable": False}}, False),
    (_cfg("/mnt/nas"), "/mnt/nas/qiyan.db", None, GOOD_HEALTH, False),
    (_cfg("/mnt/nas"), "/mnt/nas/qiyan.db", "https://x", {}, False),
    (_cfg("/mnt/nas"), "/mnt/nas/qiyan.db", "https://x", GOOD_HEALTH, True),
])
def test_production_requires_remote_nas_and_postcheck(cfg, db, url, health, ok):
    written = []
    run = lambda: fc_safety.production_release_checks(  # noqa: E731
        {**GOOD_ENV, "QIYAN_DB": db}, "qiyan", False, cfg,
        deploy=lambda: url, check_health=lambda u: health, record=written.append)
    if ok:
        assert run() == url
        assert written == [url]
    else:
        with pytest.raises(SystemExit):
            run()
        assert written == []


def _llm_ns(app_env, root_env):
    import ast
    src = (DEPLOY / "deploy_fc.py").read_text(encoding="utf-8")
    ns = {
        "__file__": str(DEPLOY / "deploy_fc.py"),
        "DEFAULT_NAME": "qiyan",
        "Path": Path,
        "load_env": lambda *a, **k: dict(app_env if a else root_env),
    }
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in {"llm_settings", "llm_key", "_local_defaults"}:
            exec(compile(ast.Module([node], []), "deploy_fc.py", "exec"), ns)
    return ns


def test_llm_settings_selects_one_complete_source():
    ns = _llm_ns({"LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "gpt-x"},
                 {"DEEPSEEK_API_KEY": "sk-root"})
    got = ns["llm_settings"]()
    assert got["key"] == "sk-root"
    assert got["base_url"] == "https://api.deepseek.com/v1"
    assert got["model"] == "deepseek-flash"
    ns = _llm_ns({"DEEPSEEK_API_KEY": "sk-app"}, {})
    assert ns["llm_settings"]()["key"] == "sk-app"
    ns = _llm_ns({"LLM_API_KEY": "sk-app", "LLM_BASE_URL": "https://api.openai.com/v1", "LLM_MODEL": "gpt-x"},
                 {"LLM_API_KEY": "sk-root", "LLM_MODEL": "deepseek-flash"})
    got = ns["llm_settings"]()
    assert (got["key"], got["base_url"], got["model"]) == ("sk-app", "https://api.openai.com/v1", "gpt-x")
    ns = _llm_ns({"LLM_MODEL": "gpt-x"}, {"LLM_BASE_URL": "https://api.openai.com/v1"})
    assert ns["llm_settings"]() is None
    assert ns["llm_key"]() == ""
    assert not [k for k in ns["_local_defaults"]("qiyan") if k.startswith("LLM_") or k == "DEEPSEEK_API_KEY"]


@pytest.mark.parametrize("db", ["\\mnt\\nas\\qiyan.db", "/mnt/nas\\qiyan.db", "mnt/nas/qiyan.db", "C:\\mnt\\nas\\qiyan.db"])
def test_production_rejects_non_posix_db_before_sdk_update(db):
    calls = {"create": 0, "update": 0, "record": 0}

    def deploy():
        calls["create"] += 1
        calls["update"] += 1
        return "https://x"

    with pytest.raises(SystemExit, match="QIYAN_DB 不在已配置 NAS 挂载目录"):
        fc_safety.production_release_checks(
            {**GOOD_ENV, "QIYAN_DB": db}, "qiyan", False, _cfg("/mnt/nas"),
            deploy=deploy, check_health=lambda u: GOOD_HEALTH,
            record=lambda u: calls.__setitem__("record", calls["record"] + 1))
    assert calls == {"create": 0, "update": 0, "record": 0}


@pytest.mark.parametrize("db,ok", [
    ("/mnt/nas/qiyan.db/", True),
    ("/mnt/nas/missing/../qiyan.db", True),
    ("/mnt/nas/../nas2/qiyan.db", False),
])
def test_deploy_uses_checked_canonical_db_path(db, ok):
    events = []
    env_vars = {**GOOD_ENV, "QIYAN_DB": db}

    def deploy():
        events.append(("sdk", env_vars["QIYAN_DB"]))
        return "https://x"

    def check_health(u):
        events.append(("health", u))
        return GOOD_HEALTH

    run = lambda: fc_safety.production_release_checks(  # noqa: E731
        env_vars, "qiyan", False, _cfg("/mnt/nas"),
        deploy=deploy, check_health=check_health, record=lambda u: events.append(("record", u)))
    if ok:
        run()
        assert events == [("sdk", "/mnt/nas/qiyan.db"), ("health", "https://x"), ("record", "https://x")]
    else:
        with pytest.raises(SystemExit):
            run()
        assert events == []
