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


def test_changing_db_path_requires_migration(monkeypatch, tmp_path):
    import importlib
    import io
    import json
    import sqlite3
    import types
    from urllib.parse import urlparse

    class Obj:
        def __init__(self, *a, **k):
            self.__dict__.update(k)

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    models = _mod("alibabacloud_fc20230330.models")
    models.__getattr__ = lambda n: Obj
    calls: list = []
    remote_env = {"QIYAN_DB": "/tmp/a.db", "QIYAN_SECRET": "x", "WECHAT_APP_ID": "wx", "ADMIN_TOKEN": "tok",
                  "BACKUP_DIR": "/mnt/nas/backup"}
    users = {"n": 12}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_function(self, name, req):
            return Obj(body=Obj(environment_variables=dict(remote_env),
                                nas_config={"mount_points": [{"mount_dir": "/mnt/nas"}]}))

        def create_function(self, req):
            calls.append("deploy")
            return Obj(body=Obj(function_name="qiyan"))

        def update_function(self, name, req):
            calls.append("deploy")

        def create_trigger(self, *a, **k):
            pass

        def get_trigger(self, name, trig):
            return Obj(body=Obj(http_trigger=Obj(url_internet="https://fc.example")))

        def put_concurrency_config(self, *a, **k):
            pass

    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330", _mod("alibabacloud_fc20230330", models=models))
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.client", _mod("alibabacloud_fc20230330.client", Client=FakeClient))
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.models", models)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi", _mod("alibabacloud_tea_openapi"))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi.models", _mod("alibabacloud_tea_openapi.models", Config=Obj))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util", _mod("alibabacloud_tea_util"))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util.models", _mod("alibabacloud_tea_util.models", RuntimeOptions=Obj))
    monkeypatch.delitem(sys.modules, "deploy_fc", raising=False)
    deploy_fc = importlib.import_module("deploy_fc")
    zip_file = tmp_path / "x.zip"
    zip_file.write_bytes(b"zip")
    monkeypatch.setattr(deploy_fc, "OUT", zip_file)
    monkeypatch.setattr(deploy_fc, "pack", lambda: None)
    monkeypatch.setattr(deploy_fc, "account_id", lambda ak, sk: "1234567")
    monkeypatch.setattr(deploy_fc, "load_env", lambda *a, **k: {"AccessKey_ID": "a", "AccessKey_Secret": "b"})
    monkeypatch.setattr(deploy_fc, "write_state", lambda rows: calls.append("record"))
    monkeypatch.setattr(deploy_fc, "warm_after_deploy", lambda *a, **k: None)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        path = urlparse(req.full_url).path
        method = req.get_method()
        if method == "POST" and path == "/api/admin/backup":
            calls.append("backup")
            return Resp(json.dumps({"ok": True, "object": "qiyan-1.db.gz.enc", "oss": "local"}).encode())
        if method == "POST" and path == "/api/admin/restore":
            calls.append("restore")
            users["n"] = 12
            return Resp(json.dumps({"ok": True, "object": "qiyan-1.db.gz.enc"}).encode())
        if path == "/api/health":
            calls.append("health")
            body = {"qiyan_users": users["n"],
                    "db": {"ephemeral": False, "writable": True, "account_store": {"registered": users["n"]}}}
            return Resp(json.dumps(body).encode())
        raise AssertionError(req.full_url)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    real_connect = sqlite3.connect

    def spy_connect(*a, **k):
        calls.append("integrity")
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    with pytest.raises(SystemExit) as exc:
        deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db"])
    assert str(exc.value) == "换库必须先备份再恢复：加 --migrate-from-backup"
    assert "deploy" not in calls

    backup = tmp_path / "backup.db"
    con = real_connect(backup)
    con.execute("CREATE TABLE users(id INTEGER)")
    con.commit()
    con.close()
    calls.clear()
    users["n"] = 12

    def update_wipes(name, req):
        calls.append("deploy")
        users["n"] = 0

    monkeypatch.setattr(FakeClient, "update_function", lambda self, name, req: update_wipes(name, req))
    deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db", "--migrate-from-backup", str(backup)])
    core = [c for c in calls if c != "health"]
    assert core == ["backup", "integrity", "deploy", "restore", "record"]
    assert calls.index("restore") < calls.index("record")
    assert [c for c in calls[calls.index("restore"):] if c == "health"]
    assert users["n"] >= 12

    calls.clear()
    users["n"] = 12
    monkeypatch.setattr(FakeClient, "update_function",
                        lambda self, name, req: (calls.append("deploy"), users.__setitem__("n", 0)))
    monkeypatch.setattr(deploy_fc, "restore_remote", lambda *a, **k: calls.append("restore") or {"ok": True})
    with pytest.raises(SystemExit):
        deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db", "--migrate-from-backup", str(backup)])
    assert "record" not in calls

    calls.clear()
    users["n"] = 0
    monkeypatch.setattr(FakeClient, "update_function", lambda self, name, req: calls.append("deploy"))
    deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db"])
    assert "backup" not in calls and "deploy" in calls and "record" in calls

    # 备份只在老实例 /tmp 且没上 OSS：新实例拿不到，发版前就拒绝
    calls.clear()
    users["n"] = 12
    remote_env["BACKUP_DIR"] = "/tmp/qiyan-backup"
    with pytest.raises(SystemExit) as exc:
        deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db", "--migrate-from-backup", str(backup)])
    assert "BACKUP_DIR" in str(exc.value)
    assert "backup" in calls and "deploy" not in calls


def test_implicit_remote_db_requires_migration(monkeypatch, tmp_path):
    import importlib
    import io
    import json
    import sqlite3
    import types
    from urllib.parse import urlparse

    class Obj:
        def __init__(self, *a, **k):
            self.__dict__.update(k)

    def _mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        return m

    models = _mod("alibabacloud_fc20230330.models")
    models.__getattr__ = lambda n: Obj
    calls: list = []
    remote_env = {"QIYAN_SECRET": "x", "WECHAT_APP_ID": "wx", "ADMIN_TOKEN": "tok",
                  "BACKUP_DIR": "/mnt/nas/backup"}
    users = {"n": 2}

    missing = {"on": False}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get_function(self, name, req):
            if missing["on"]:
                raise RuntimeError("FunctionNotFound")
            return Obj(body=Obj(environment_variables=dict(remote_env),
                                nas_config={"mount_points": [{"mount_dir": "/mnt/nas"}]}))

        def create_function(self, req):
            calls.append("deploy")
            return Obj(body=Obj(function_name="qiyan"))

        def update_function(self, name, req):
            calls.append("deploy")

        def create_trigger(self, *a, **k):
            pass

        def get_trigger(self, name, trig):
            return Obj(body=Obj(http_trigger=Obj(url_internet="https://fc.example")))

        def put_concurrency_config(self, *a, **k):
            pass

    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330", _mod("alibabacloud_fc20230330", models=models))
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.client", _mod("alibabacloud_fc20230330.client", Client=FakeClient))
    monkeypatch.setitem(sys.modules, "alibabacloud_fc20230330.models", models)
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi", _mod("alibabacloud_tea_openapi"))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_openapi.models", _mod("alibabacloud_tea_openapi.models", Config=Obj))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util", _mod("alibabacloud_tea_util"))
    monkeypatch.setitem(sys.modules, "alibabacloud_tea_util.models", _mod("alibabacloud_tea_util.models", RuntimeOptions=Obj))
    monkeypatch.delitem(sys.modules, "deploy_fc", raising=False)
    deploy_fc = importlib.import_module("deploy_fc")
    zip_file = tmp_path / "x.zip"
    zip_file.write_bytes(b"zip")
    monkeypatch.setattr(deploy_fc, "OUT", zip_file)
    monkeypatch.setattr(deploy_fc, "pack", lambda: None)
    monkeypatch.setattr(deploy_fc, "account_id", lambda ak, sk: "1234567")
    monkeypatch.setattr(deploy_fc, "load_env", lambda *a, **k: {"AccessKey_ID": "a", "AccessKey_Secret": "b"})
    monkeypatch.setattr(deploy_fc, "write_state", lambda rows: calls.append("record"))
    monkeypatch.setattr(deploy_fc, "warm_after_deploy", lambda *a, **k: None)

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        path = urlparse(req.full_url).path
        method = req.get_method()
        if method == "POST" and path == "/api/admin/backup":
            calls.append("backup")
            return Resp(json.dumps({"ok": True, "object": "qiyan-1.db.gz.enc", "oss": "local"}).encode())
        if method == "POST" and path == "/api/admin/restore":
            calls.append("restore")
            users["n"] = 2
            return Resp(json.dumps({"ok": True, "object": "qiyan-1.db.gz.enc"}).encode())
        if path == "/api/health":
            calls.append("health")
            body = {"qiyan_users": users["n"],
                    "db": {"ephemeral": False, "writable": True, "account_store": {"registered": users["n"]}}}
            return Resp(json.dumps(body).encode())
        raise AssertionError(req.full_url)

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    real_connect = sqlite3.connect

    def spy_connect(*a, **k):
        calls.append("integrity")
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)

    with pytest.raises(SystemExit) as exc:
        deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db"])
    assert str(exc.value) == "换库必须先备份再恢复：加 --migrate-from-backup"
    assert "deploy" not in calls and "record" not in calls

    backup = tmp_path / "backup.db"
    con = real_connect(backup)
    con.execute("CREATE TABLE users(id INTEGER)")
    con.commit()
    con.close()
    calls.clear()
    monkeypatch.setattr(FakeClient, "update_function",
                        lambda self, name, req: (calls.append("deploy"), users.__setitem__("n", 0)))
    deploy_fc.main(["qiyan", "--db", "/mnt/nas/qiyan.db", "--migrate-from-backup", str(backup)])
    assert [c for c in calls if c != "health"] == ["backup", "integrity", "deploy", "restore", "record"]
    assert users["n"] == 2

    calls.clear()
    missing["on"] = True
    deploy_fc.main(["qiyan-test", "--db", "/tmp/qiyan-test.db", "--allow-ephemeral"])
    assert "backup" not in calls and "deploy" in calls


def test_ephemeral_flag_follows_function_name_and_switch():
    env = {"QIYAN_ALLOW_EPHEMERAL": "1"}  # 函数上残留的放行开关：正常发生产要清掉
    fc_safety.apply_ephemeral_flag(env, "qiyan", False)
    assert "QIYAN_ALLOW_EPHEMERAL" not in env
    fc_safety.apply_ephemeral_flag(env, "qiyan", True)
    assert env["QIYAN_ALLOW_EPHEMERAL"] == "1"
    env = {}
    fc_safety.apply_ephemeral_flag(env, "qiyan-test", False)
    assert env["QIYAN_ALLOW_EPHEMERAL"] == "1"


def test_release_checks_set_or_clear_the_flag_before_deploy():
    seen = {}
    env = {**GOOD_ENV, "QIYAN_DB": "/mnt/nas/qiyan.db", "QIYAN_ALLOW_EPHEMERAL": "1"}
    fc_safety.production_release_checks(env, "qiyan", False, _cfg("/mnt/nas"),
                                        deploy=lambda: seen.update(env) or "https://x",
                                        check_health=lambda u: GOOD_HEALTH, record=lambda u: None)
    assert "QIYAN_ALLOW_EPHEMERAL" not in seen
    env = {"QIYAN_DB": "/tmp/qiyan-test.db"}
    fc_safety.production_release_checks(env, "qiyan-test", False, None,
                                        deploy=lambda: seen.update(env) or "https://t",
                                        check_health=lambda u: {}, record=lambda u: None)
    assert seen["QIYAN_ALLOW_EPHEMERAL"] == "1"
