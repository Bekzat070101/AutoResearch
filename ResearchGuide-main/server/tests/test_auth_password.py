# -*- coding: utf-8 -*-
"""注册 / 登录 / 升级：密码只存哈希，访客入口形状不变。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "auth.db")
    store.init_db()
    auth._HITS.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_old_guest_row_survives_new_columns(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db()
    guest = store.create_user("旧访客")
    store.init_db()
    u = store.get_user(guest["uid"])
    assert u["nickname"] == "旧访客"
    assert not u.get("username")
    with sqlite3.connect(db) as c:
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"user_secrets", "user_prefs", "drafts", "trails"} <= names
    assert not any(str(db.parent / f"users/{guest['uid']}.db") == p for p in [])


def test_register_login_same_uid(client):
    r = client.post("/api/auth/register", json={
        "username": "Demo_User", "password": "secret-ok", "nickname": "小北", "consent": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "demo_user"
    assert body["guest"] is False
    assert body["uid"]
    assert "password" not in str(body).lower()
    uid, token = body["uid"], body["token"]
    me = client.get("/api/auth/me", headers=_h(token)).json()
    assert me["uid"] == uid
    assert me["username"] == "demo_user"
    assert "api_key" not in me
    assert me["has_llm_key"] is False
    again = client.post("/api/auth/signin", json={"username": "demo_user", "password": "secret-ok"})
    assert again.status_code == 200
    assert again.json()["uid"] == uid
    raw = store.DB_PATH.read_bytes()
    assert b"secret-ok" not in raw


def test_duplicate_username_conflict(client):
    payload = {"username": "taken_1", "password": "secret-ok", "nickname": "甲", "consent": True}
    assert client.post("/api/auth/register", json=payload).status_code == 200
    r = client.post("/api/auth/register", json={**payload, "nickname": "乙"})
    assert r.status_code == 409


def test_wrong_password_is_401(client):
    client.post("/api/auth/register", json={
        "username": "alice", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    r = client.post("/api/auth/signin", json={"username": "alice", "password": "nope-nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "用户名或密码不对"


def test_register_requires_consent_and_guest_login_shape_unchanged(client):
    bad = client.post("/api/auth/register", json={
        "username": "need_ok", "password": "secret-ok", "nickname": "甲", "consent": False,
    })
    assert bad.status_code == 400
    guest = client.post("/api/auth/login", json={"nickname": "路人", "consent": True})
    assert guest.status_code == 200
    g = guest.json()
    assert g["guest"] is True
    assert g["token"]
    assert g["nickname"] == "路人"


def test_upgrade_guest_then_signin(client):
    g = client.post("/api/auth/login", json={"nickname": "过客", "consent": True}).json()
    uid, token = g["uid"], g["token"]
    up = client.post("/api/auth/upgrade", json={"username": "kept_me", "password": "secret-ok"},
                     headers=_h(token))
    assert up.status_code == 200, up.text
    assert up.json()["guest"] is False
    assert up.json()["uid"] == uid
    later = client.post("/api/auth/signin", json={"username": "kept_me", "password": "secret-ok"})
    assert later.status_code == 200
    assert later.json()["uid"] == uid
    twice = client.post("/api/auth/upgrade", json={"username": "other_nm", "password": "secret-ok"},
                        headers=_h(token))
    assert twice.status_code == 409


def test_signin_rate_limits_before_password(client, monkeypatch):
    monkeypatch.setattr(auth, "SIGNIN_USER_LIMIT", 2)
    client.post("/api/auth/register", json={
        "username": "rate_me", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    assert client.post("/api/auth/signin", json={"username": "rate_me", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/signin", json={"username": "rate_me", "password": "nope-nope"}).status_code == 401
    third = client.post("/api/auth/signin", json={"username": "rate_me", "password": "nope-nope"})
    assert third.status_code == 429
    ok_in_window = client.post("/api/auth/signin", json={"username": "rate_me", "password": "secret-ok"})
    assert ok_in_window.status_code == 429


def test_signin_bucket_cardinality_is_bounded(client, monkeypatch):
    clock = [auth.time.time() + 10]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    client.post("/api/auth/register", json={
        "username": "real_user", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    for bad in ("x", "a" * 25, "全角名字"):  # 形状不对的用户名：401，而且不建桶
        assert client.post("/api/auth/signin", json={"username": bad, "password": "nope-nope"}).status_code == 401
    assert not [k for k in auth._HITS if k.startswith("signin-user:")]
    for _ in range(auth.SIGNIN_USER_LIMIT):
        client.post("/api/auth/signin", json={"username": "locked_one", "password": "nope-nope"})
    assert client.post("/api/auth/signin", json={"username": "locked_one", "password": "nope-nope"}).status_code == 429
    hashed = []
    monkeypatch.setattr(store, "password_ok", lambda *a, **k: hashed.append(1) or False)
    codes = []
    for i in range(10020):
        r = client.post("/api/auth/signin", json={"username": f"ghost_{i}", "password": "nope-nope"})
        codes.append(r.status_code)
    assert len(auth._HITS) <= 10000
    assert codes[-1] == 429
    assert hashed == []
    assert client.post("/api/auth/signin", json={"username": "locked_one", "password": "nope-nope"}).status_code == 429
    db = store.DB_PATH
    monkeypatch.undo()
    monkeypatch.setattr(store, "DB_PATH", db)
    clock[0] += 600
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    ok = client.post("/api/auth/signin", json={"username": "real_user", "password": "secret-ok"})
    assert ok.status_code == 200, ok.text
    assert len(auth._HITS) < 10000


@pytest.mark.parametrize("bad", ["x", "a" * 25, "全角名字"])
def test_invalid_signin_public_ip_does_not_create_bucket(client, monkeypatch, bad):
    """R4-08：公网来源发形状不对的用户名，401 且不建任何桶、不查用户表。"""
    clock = [10000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    monkeypatch.setenv("TRUST_PROXY", "1")
    looked = []
    monkeypatch.setattr(store, "user_by_username", lambda *a, **k: looked.append(a) or None)
    before = {k: list(v) for k, v in auth._HITS.items()}
    r = client.post("/api/auth/signin", json={"username": bad, "password": "bad"},
                    headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 401
    assert r.json() == {"detail": "用户名或密码不对"}
    assert looked == []
    assert auth._HITS == before


def test_invalid_signin_full_capacity_still_401(client, monkeypatch):
    """R4-08：桶已满 10000 个未过期时，非法用户名仍是 401，不是 429。"""
    clock = [10000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    monkeypatch.setenv("TRUST_PROXY", "1")
    for i in range(auth._HITS_CAP):
        auth._HITS[f"fill:{i}"] = [clock[0]]
    looked = []
    monkeypatch.setattr(store, "user_by_username", lambda *a, **k: looked.append(a) or None)
    r = client.post("/api/auth/signin", json={"username": "x", "password": "bad"},
                    headers={"X-Forwarded-For": "8.8.8.8"})
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "用户名或密码不对"}
    assert looked == []
    assert len(auth._HITS) == auth._HITS_CAP


def test_successful_logins_do_not_consume_the_username_bucket(client):
    client.post("/api/auth/register", json={
        "username": "good_user", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    for _ in range(9):
        assert client.post("/api/auth/signin", json={"username": "good_user", "password": "secret-ok"}).status_code == 200
    assert client.post("/api/auth/signin", json={"username": "good_user", "password": "secret-ok"}).status_code == 200
    for _ in range(9):
        assert client.post("/api/auth/signin", json={"username": "good_user", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/signin", json={"username": "good_user", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/signin", json={"username": "good_user", "password": "nope-nope"}).status_code == 429


def test_signin_ip_budget_is_separate_from_guest_budget(client, monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "1")
    xff = {"X-Forwarded-For": "8.8.8.8"}
    client.post("/api/auth/register", json={
        "username": "class_user", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    for _ in range(auth.PER_IP_HOUR):
        assert client.post("/api/auth/login", json={"nickname": "访客"}, headers=xff).status_code == 200
    assert client.post("/api/auth/login", json={"nickname": "访客"}, headers=xff).status_code == 429
    assert client.post("/api/auth/signin", json={"username": "class_user", "password": "secret-ok"},
                       headers=xff).status_code == 200
    auth._HITS.clear()
    monkeypatch.setattr(auth, "SIGNIN_PER_IP_HOUR", 5, raising=False)
    for _ in range(5):
        assert client.post("/api/auth/signin", json={"username": "class_user", "password": "secret-ok"},
                           headers=xff).status_code == 200
    assert client.post("/api/auth/signin", json={"username": "class_user", "password": "secret-ok"},
                       headers=xff).status_code == 429
    assert client.post("/api/auth/login", json={"nickname": "访客"}, headers=xff).status_code == 200


def test_inflight_failure_slots_are_bounded(client, monkeypatch):
    import threading
    client.post("/api/auth/register", json={
        "username": "race_user", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    real_ok = store.password_ok
    entered = threading.Semaphore(0)
    gate = threading.Event()
    calls = []

    def latched(*a, **k):
        calls.append(1)
        entered.release()
        gate.wait(10)
        return False

    monkeypatch.setattr(store, "password_ok", latched)
    codes = []

    def attempt():
        r = client.post("/api/auth/signin", json={"username": "race_user", "password": "nope-nope"})
        codes.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for _ in range(10):
        assert entered.acquire(timeout=10)
    eleventh = client.post("/api/auth/signin", json={"username": "race_user", "password": "nope-nope"})
    assert eleventh.status_code == 429
    assert eleventh.json() == {"detail": "试得太频繁了，过一会儿再试"}
    gate.set()
    for t in threads:
        t.join(10)
    assert sorted(codes) == [401] * 10
    assert len(calls) == 10
    assert auth._INFLIGHT == {}
    assert len(auth._HITS["signin-user:race_user"]) == 10

    auth._HITS.clear()

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "password_ok", boom)
    loose = TestClient(main.app, raise_server_exceptions=False)
    assert loose.post("/api/auth/signin", json={"username": "race_user", "password": "nope-nope"}).status_code == 500
    assert auth._INFLIGHT == {}
    assert not auth._HITS.get("signin-user:race_user")
    monkeypatch.setattr(store, "password_ok", real_ok)
    assert client.post("/api/auth/signin", json={"username": "race_user", "password": "nope-nope"}).status_code == 401
    assert client.post("/api/auth/signin", json={"username": "race_user", "password": "secret-ok"}).status_code == 200
    assert auth._INFLIGHT == {}
    assert "signin-user:race_user" not in auth._HITS


def test_configured_failure_window_controls_sweep(client, monkeypatch):
    import os
    import subprocess
    clock = [10000.0]
    monkeypatch.setattr(auth.time, "time", lambda: clock[0])
    monkeypatch.setattr(auth, "SIGNIN_USER_WINDOW", 1200)
    client.post("/api/auth/register", json={
        "username": "slow_user", "password": "secret-ok", "nickname": "甲", "consent": True,
    })
    auth._HITS.clear()
    for _ in range(10):
        assert client.post("/api/auth/signin", json={"username": "slow_user", "password": "nope-nope"}).status_code == 401
    for t, code in ((10600, 429), (11199.999, 429), (11200, 200)):
        clock[0] = t
        assert client.post("/api/auth/signin", json={"username": "slow_user", "password": "secret-ok"}).status_code == code

    monkeypatch.setattr(auth, "SIGNIN_USER_WINDOW", 30)
    auth._HITS.clear()
    clock[0] = 20000.0
    for _ in range(10):
        assert client.post("/api/auth/signin", json={"username": "slow_user", "password": "nope-nope"}).status_code == 401
    for t, code in ((20029.999, 429), (20030, 200)):
        clock[0] = t
        assert client.post("/api/auth/signin", json={"username": "slow_user", "password": "secret-ok"}).status_code == code

    auth._HITS.clear()
    clock[0] = 30000.0
    for i in range(auth._HITS_CAP):
        auth._HITS[f"signin-user:ghost_{i}"] = [clock[0]]
    auth._NEXT_SWEEP[0] = 0.0
    clock[0] = 30030.0
    assert client.post("/api/auth/signin", json={"username": "slow_user", "password": "secret-ok"}).status_code == 200
    assert len(auth._HITS) < 10

    server_dir = Path(auth.__file__).resolve().parent
    for name in ("AUTH_SIGNIN_USER_LIMIT", "AUTH_SIGNIN_USER_WINDOW", "AUTH_SIGNIN_PER_IP_HOUR", "AUTH_PER_IP_HOUR"):
        for bad in ("0", "-5", "abc"):
            env = {**os.environ, name: bad, "LLM_API_KEY": ""}
            r = subprocess.run([sys.executable, "-c", "import auth"], cwd=server_dir, env=env,
                               capture_output=True, text=True, timeout=60)
            assert r.returncode != 0
            assert "AUTH_* 配置必须是正整数" in r.stderr
