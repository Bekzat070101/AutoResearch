# -*- coding: utf-8 -*-
"""发版换空库后，已注册用户名和密码哈希仍能登录。"""
from __future__ import annotations

import gzip
import json
import sys
import urllib.request
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import account_store  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
KEY = "backup-key-16++++"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "live" / "qiyan.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE", raising=False)
    monkeypatch.delenv("BACKUP_OSS_BUCKET", raising=False)
    monkeypatch.delenv("BACKUP_KEY", raising=False)
    monkeypatch.delenv("QIYAN_SECRET", raising=False)
    store.init_db()
    auth._HITS.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def _reg(client, name="keep_me", password="secret-ok"):
    r = client.post("/api/auth/register", json={
        "username": name, "password": password, "nickname": "留着", "consent": True,
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_new_empty_db_without_store_makes_password_look_wrong(client, tmp_path, monkeypatch):
    """修前：换一份空库后，原密码登录 401，同名可以再注册。"""
    body = _reg(client)
    uid = body["uid"]
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "after-deploy" / "qiyan.db")
    store.init_db()
    again = client.post("/api/auth/signin", json={"username": "keep_me", "password": "secret-ok"})
    assert again.status_code == 401
    assert again.json()["detail"] == "用户名或密码不对"
    rereg = client.post("/api/auth/register", json={
        "username": "keep_me", "password": "secret-ok", "nickname": "新的", "consent": True,
    })
    assert rereg.status_code == 200
    assert rereg.json()["uid"] != uid


def test_account_store_keeps_password_across_empty_db(client, tmp_path, monkeypatch):
    """修后：副本不和库放一起时，发版换空库仍用原密码登录，uid 不变。"""
    durable = tmp_path / "durable" / "accounts.json.enc"
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(durable))
    monkeypatch.setenv("BACKUP_KEY", KEY)
    body = _reg(client)
    uid = body["uid"]
    raw = durable.read_bytes()
    assert b"secret-ok" not in raw
    payload = json.loads(gzip.decompress(account_store._aes(raw, KEY.encode(), False)))
    assert payload["v"] == 2
    assert isinstance(payload["sessions"], list)
    me = next(u for u in payload["users"] if u["username"] == "keep_me")
    assert "password" not in me
    assert me["password_hash"]

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "after-deploy" / "qiyan.db")
    store.init_db()
    assert store.user_by_username("keep_me")["id"] == uid
    again = client.post("/api/auth/signin", json={"username": "keep_me", "password": "secret-ok"})
    assert again.status_code == 200, again.text
    assert again.json()["uid"] == uid
    taken = client.post("/api/auth/register", json={
        "username": "keep_me", "password": "secret-ok", "nickname": "抢名", "consent": True,
    })
    assert taken.status_code == 409


def test_deleted_account_does_not_return_after_hydrate(client, tmp_path, monkeypatch):
    durable = tmp_path / "durable" / "accounts.json.enc"
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(durable))
    monkeypatch.setenv("BACKUP_KEY", KEY)
    body = _reg(client, "gone_user")
    token = body["token"]
    gone = client.delete("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert gone.status_code == 200, gone.text
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "after-deploy" / "qiyan.db")
    store.init_db()
    again = client.post("/api/auth/signin", json={"username": "gone_user", "password": "secret-ok"})
    assert again.status_code == 401


def test_hydrate_merges_into_db_that_already_has_users(client, tmp_path, monkeypatch):
    """v2：库里已有人也灌回（按 id 补缺），已有的不动；换库后有人抢注了同名就记 collision、不覆盖。"""
    durable = tmp_path / "durable" / "accounts.json.enc"
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(durable))
    monkeypatch.setenv("BACKUP_KEY", KEY)
    old_uid = _reg(client, "old_one")["uid"]
    _reg(client, "taken_name")
    account_store.flush()  # 两次注册在 2 秒合并窗内：把还没写的那次先写掉（进程退出前也是这么做）
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE")
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "kept" / "qiyan.db")
    store.init_db()
    second = client.post("/api/auth/register", json={
        "username": "new_one", "password": "secret-ok", "nickname": "新库", "consent": True,
    })
    assert second.status_code == 200
    squatter = _reg(client, "taken_name", "other-pw")
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(durable))
    store.init_db()
    assert store.user_by_username("new_one")["id"] == second.json()["uid"]
    assert store.user_by_username("old_one")["id"] == old_uid
    assert store.user_by_username("taken_name")["id"] == squatter["uid"]
    assert account_store.status()["collisions"] == 1
    again = client.post("/api/auth/signin", json={"username": "old_one", "password": "secret-ok"})
    assert again.status_code == 200


def test_oss_durable_prefix_not_daily(client, tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_KEY", KEY)
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_SECRET", "secret")
    uploaded = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return uploaded["blob"]

    def fake_urlopen(req, timeout=30):
        url = getattr(req, "full_url", None) or req.get_full_url()
        assert "/durable/accounts.json.enc" in url
        assert "/daily/" not in url
        if req.get_method() == "PUT":
            uploaded["blob"] = req.data
            return _Resp()
        if req.get_method() == "GET":
            if "blob" not in uploaded:
                raise HTTPError(url, 404, "NoSuchKey", {}, BytesIO())
            return _Resp()
        raise AssertionError(req.get_method())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    uid = _reg(client, "oss_user")["uid"]
    assert "blob" in uploaded
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "after-deploy" / "qiyan.db")
    store.init_db()
    # v2：init_db 不碰网络；OSS 的副本由 _lifespan 在线程池里拉（这里直接调它调的函数）
    before = client.post("/api/auth/signin", json={"username": "oss_user", "password": "secret-ok"})
    assert before.status_code == 401
    out = account_store.hydrate_oss()
    assert out.get("ok") and out["via"] == "oss", out
    again = client.post("/api/auth/signin", json={"username": "oss_user", "password": "secret-ok"})
    assert again.status_code == 200
    assert again.json()["uid"] == uid


def test_health_reports_account_store(client, tmp_path, monkeypatch):
    h = client.get("/api/health").json()
    assert "ephemeral" in h["db"]
    assert h["db"]["account_store"]["enabled"] is False
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(tmp_path / "acc.enc"))
    monkeypatch.setenv("BACKUP_KEY", KEY)
    h2 = client.get("/api/health").json()
    assert h2["db"]["account_store"]["enabled"] is True
    assert "file" in h2["db"]["account_store"]["via"]
