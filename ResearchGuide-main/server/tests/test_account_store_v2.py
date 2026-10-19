# -*- coding: utf-8 -*-
"""账号副本 v2：覆盖访客与会话、原子写、合并灌回、OSS 拉取在 lifespan、health/metrics 可观测。"""
from __future__ import annotations

import gzip
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import account_store  # noqa: E402
import auth  # noqa: E402
import main  # noqa: E402
import metrics  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
SECRET = "qiyan-secret-16+++++"


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "live" / "qiyan.db")
    monkeypatch.setenv("QIYAN_ACCOUNT_STORE", str(tmp_path / "durable" / "accounts.json.enc"))
    monkeypatch.setenv("QIYAN_SECRET", SECRET)
    monkeypatch.delenv("BACKUP_KEY", raising=False)
    monkeypatch.delenv("BACKUP_OSS_BUCKET", raising=False)
    store.init_db()
    auth._HITS.clear()
    yield
    account_store.reset()


@pytest.fixture
def client():
    return TestClient(main.app)


def _durable() -> Path:
    return account_store.file_path()


def _read(path: Path) -> dict:
    return json.loads(gzip.decompress(account_store._aes(path.read_bytes(), SECRET.encode(), False)))


def _reg(client, name, password="secret-ok"):
    r = client.post("/api/auth/register", json={
        "username": name, "password": password, "nickname": name, "consent": True,
    })
    assert r.status_code == 200, r.text
    return r.json()


def _guest(client, nickname="访客"):
    r = client.post("/api/auth/login", json={"nickname": nickname, "consent": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_snapshot_includes_guests_and_sessions_but_no_secrets(client):
    named = _reg(client, "named_one")
    guest = _guest(client)
    wx_uid = store.create_user("微信")["uid"]
    assert store.bind_wechat(wx_uid, "openid-xyz")
    wx_token = store.create_session(wx_uid, 3600)
    store.save_llm_secret(named["uid"], "sk-personal-key-should-not-leak", "https://example.invalid", "m")
    account_store.flush()
    raw = _durable().read_bytes()
    payload = _read(_durable())
    assert payload["v"] == 2
    uids = {u["id"] for u in payload["users"]}
    assert {named["uid"], guest["uid"], wx_uid} <= uids
    hashes = {s["token_hash"] for s in payload["sessions"]}
    assert store._token_hash(named["token"]) in hashes
    assert store._token_hash(guest["token"]) in hashes
    assert store._token_hash(wx_token) in hashes
    text = json.dumps(payload, ensure_ascii=False)
    assert "sk-personal-key-should-not-leak" not in text
    assert "user_secrets" not in text and "wrapped" not in text
    for tok in (named["token"], guest["token"], wx_token):
        assert tok not in text
        assert tok.encode() not in raw
    assert any(u.get("wechat_openid") == "openid-xyz" for u in payload["users"])


def test_hydrate_merges_into_non_empty_db_and_reports_collisions(client, tmp_path, monkeypatch):
    old_same = _reg(client, "same_name")
    old_other = _reg(client, "other_name")
    guest = _guest(client)
    account_store.flush()
    snapshot = _read(_durable())

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "after" / "qiyan.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE")
    store.init_db()
    newer = _reg(client, "same_name", "another-pass")
    assert newer["uid"] != old_same["uid"]

    out = account_store.hydrate(snapshot)
    assert out["collisions"] == 1
    assert account_store.status()["collisions"] == 1
    assert store.get_user(old_same["uid"]) is None
    assert store.user_by_username("same_name")["id"] == newer["uid"]
    assert store.user_by_username("other_name")["id"] == old_other["uid"]
    assert store.get_user(guest["uid"]) is not None
    for tok, uid in ((old_other["token"], old_other["uid"]), (guest["token"], guest["uid"])):
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200, r.text
        assert r.json()["uid"] == uid
    gone = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_same['token']}"})
    assert gone.status_code != 200


def test_dump_is_atomic_and_coalesced(client, monkeypatch):
    _reg(client, "atomic_one")
    account_store.flush()
    path = _durable()
    before = path.read_bytes()

    def boom(src, dst):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(account_store.os, "replace", boom)
        store.create_user("新访客")
        account_store.flush()
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp-*"))
    assert account_store.status()["last_error"] == "OSError"

    account_store.reset()
    writes = []
    real = account_store._write_atomic

    def counting(p, blob):
        writes.append(time.time())
        real(p, blob)

    monkeypatch.setattr(account_store, "_write_atomic", counting)
    start = time.time()
    for i in range(10):
        store.create_user(f"g{i}")
    assert len(writes) == 1
    time.sleep(0.5)
    assert len(writes) == 1
    assert time.time() - start < 2
    account_store.flush()
    assert len(writes) == 2
    assert len(_read(path)["users"]) == 12


def test_oss_hydrate_runs_in_lifespan_with_timeout(monkeypatch):
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setattr(main, "ACCOUNT_OSS_TIMEOUT", 0.3)
    release = threading.Event()
    called = threading.Event()

    def hang():
        called.set()
        release.wait(11)
        return None

    monkeypatch.setattr(account_store, "_get_oss", hang)
    monkeypatch.setattr(account_store, "_put_oss", lambda blob: None)
    start = time.time()
    try:
        with TestClient(main.app) as c:
            assert time.time() - start < 5
            assert called.wait(5)
            h = None
            for _ in range(50):
                h = c.get("/api/health").json()
                if h["db"]["account_store"]["last_error"] == "timeout":
                    break
                time.sleep(0.1)
            assert h["db"]["account_store"]["last_error"] == "timeout"
            release.set()
    finally:
        release.set()


def test_invalid_snapshot_is_preserved_not_overwritten(client, monkeypatch):
    path = _durable()
    path.parent.mkdir(parents=True, exist_ok=True)
    garbage = b"not-a-snapshot-encrypted-with-an-old-key"
    path.write_bytes(garbage)
    store.init_db()
    assert account_store.status()["last_error"] == "invalid"
    _reg(client, "after_rotate")
    account_store.flush()
    kept = path.with_name(f"{path.name}.undecryptable-{datetime.now().strftime('%Y%m%d')}")
    assert kept.read_bytes() == garbage
    assert _read(path)["v"] == 2


def test_health_and_metrics_expose_snapshot_state(client):
    h = client.get("/api/health").json()["db"]["account_store"]
    for k in ("snapshot_users", "snapshot_sessions", "last_dump_at", "last_hydrate_at", "last_error", "collisions"):
        assert k in h
    text = metrics.render()
    assert "qiyan_account_snapshot_age_seconds -1" in text
    _reg(client, "seen_user")
    _guest(client)
    account_store.flush()
    h = client.get("/api/health").json()["db"]["account_store"]
    assert h["snapshot_users"] == 2
    assert h["snapshot_sessions"] == 2
    assert h["last_dump_at"]
    assert h["collisions"] == 0
    text = metrics.render()
    assert "qiyan_account_snapshot_users 2" in text
    age = [ln for ln in text.splitlines() if ln.startswith("qiyan_account_snapshot_age_seconds ")][0]
    assert 0 <= float(age.split()[1]) < 60


def test_deleted_user_is_not_revived_by_stale_snapshot(client, monkeypatch):
    acct = _reg(client, "gone_user")
    account_store.flush()
    assert any(u["id"] == acct["uid"] for u in _read(_durable())["users"])

    def boom():
        raise OSError("dump failed")

    with monkeypatch.context() as m:
        m.setattr(account_store, "_dump_now", boom)
        r = client.delete("/api/me", headers={"Authorization": f"Bearer {acct['token']}"})
        assert r.status_code == 200, r.text
    snapshot = _read(_durable())
    assert any(u["id"] == acct["uid"] for u in snapshot["users"])

    store.init_db()
    r = client.post("/api/auth/signin", json={"username": "gone_user", "password": "secret-ok"})
    assert r.status_code == 401
    assert store.user_by_username("gone_user") is None
    assert account_store.hydrate(snapshot)["tombstoned"] == 1


def test_entered_at_travels_with_snapshot(client, tmp_path, monkeypatch):
    acct = _reg(client, "entered_user")
    r = client.post("/api/onboard/enter", json={"uid": acct["uid"]},
                    headers={"Authorization": f"Bearer {acct['token']}"})
    assert r.status_code == 200, r.text
    account_store.flush()
    snapshot = _read(_durable())
    assert any(u["id"] == acct["uid"] and u["entered_at"] for u in snapshot["users"])

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fresh" / "qiyan.db")
    store.init_db()
    r = client.post("/api/auth/signin", json={"username": "entered_user", "password": "secret-ok"})
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is True

    v1 = {"v": 1, "users": [{k: v for k, v in u.items() if k != "entered_at"} for u in snapshot["users"]]}
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "fresh_v1" / "qiyan.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE")
    store.init_db()
    account_store.hydrate(v1)
    r = client.post("/api/auth/signin", json={"username": "entered_user", "password": "secret-ok"})
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is False


def _me(client, token):
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


def test_user_and_session_tombstones_win(client, tmp_path, monkeypatch):
    out = _reg(client, "out_user")
    stay = _reg(client, "stay_user")
    gone = _reg(client, "gone_user2")
    account_store.flush()
    old = _read(_durable())

    r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {out['token']}"})
    assert r.status_code == 200, r.text
    assert _me(client, out["token"]).status_code == 401
    r = client.delete("/api/me", headers={"Authorization": f"Bearer {gone['token']}"})
    assert r.status_code == 200, r.text
    account_store.hydrate(old)
    assert _me(client, out["token"]).status_code == 401
    assert _me(client, stay["token"]).status_code == 200

    late = _reg(client, "late_out")
    account_store.flush()
    r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {late['token']}"})
    assert r.status_code == 200, r.text
    account_store.reset()
    store.init_db()
    assert _me(client, late["token"]).status_code == 401
    assert _me(client, stay["token"]).status_code == 200

    account_store.dump_accounts(immediate=True)
    latest = _read(_durable())
    assert {store._token_hash(out["token"]), store._token_hash(late["token"])} <= {
        x["token_hash"] for x in latest["revoked"]}
    assert gone["uid"] in {x["id"] for x in latest["deleted"]}

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "empty" / "qiyan.db")
    store.init_db()
    account_store.hydrate(old)
    assert _me(client, out["token"]).status_code == 401
    assert _me(client, late["token"]).status_code == 401
    assert _me(client, gone["token"]).status_code == 401
    assert store.get_user(gone["uid"]) is None
    assert _me(client, stay["token"]).status_code == 200


def test_late_oss_respects_latest_local_dump(client, monkeypatch):
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    acct = _reg(client, "late_oss_user")
    old = account_store.export_snapshot()

    def fail_put(blob):
        raise RuntimeError("oss down")

    monkeypatch.setattr(account_store, "_put_oss", fail_put)
    r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {acct['token']}"})
    assert r.status_code == 200, r.text
    account_store.flush()
    account_store.dump_accounts(immediate=True)
    assert account_store.status()["last_error"] == "RuntimeError"
    assert account_store._STATE["file_saved_at"] == _read(_durable())["saved_at"]

    monkeypatch.setattr(account_store, "_get_oss", lambda: account_store._encrypt(old))
    assert account_store.hydrate_oss() == {"skipped": "older_than_file"}
    assert account_store.apply_oss_payload(old) == {"skipped": "older_than_file"}
    assert _me(client, acct["token"]).status_code == 401


def test_oss_timeout_cannot_apply_late_result(client, tmp_path, monkeypatch):
    acct = _reg(client, "late_result")
    account_store.flush()
    blob = account_store._encrypt(_read(_durable()))

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "next" / "qiyan.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE")
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setattr(account_store, "_put_oss", lambda b: None)
    monkeypatch.setattr(main, "ACCOUNT_OSS_TIMEOUT", 0.05)
    store.init_db()
    release = threading.Event()
    returned = threading.Event()

    def slow():
        release.wait(11)
        returned.set()
        return blob

    monkeypatch.setattr(account_store, "_get_oss", slow)
    try:
        with TestClient(main.app) as c:
            h = None
            for _ in range(50):
                h = c.get("/api/health").json()
                if h["db"]["account_store"]["last_error"] == "timeout":
                    break
                time.sleep(0.05)
            assert h["db"]["account_store"]["last_error"] == "timeout"
            release.set()
            assert returned.wait(5)
            time.sleep(0.5)
            assert store.get_user(acct["uid"]) is None
            assert _me(c, acct["token"]).status_code == 401
            assert account_store.status()["last_hydrate_at"] is None
            assert account_store.status()["last_error"] == "timeout"
    finally:
        release.set()

    monkeypatch.setattr(account_store, "_get_oss", lambda: blob)
    assert account_store.hydrate_oss().get("ok")
    assert store.get_user(acct["uid"]) is not None


def test_oss_read_errors_are_observable(client, monkeypatch):
    import urllib.error
    import urllib.request
    from io import BytesIO

    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_SECRET", "secret")
    before = account_store._user_count()
    cases = [
        (urllib.error.HTTPError("u", 404, "NoSuchKey", {}, BytesIO()), {"skipped": "no_snapshot"}, None),
        (urllib.error.HTTPError("u", 403, "Forbidden", {}, BytesIO()), {"skipped": "HTTPError"}, "HTTPError"),
        (TimeoutError("timed out"), {"skipped": "TimeoutError"}, "TimeoutError"),
        (OSError("reset"), {"skipped": "OSError"}, "OSError"),
    ]
    for exc, out, err in cases:
        account_store.reset()

        def boom(req, timeout=30, exc=exc):
            raise exc

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert account_store.hydrate_oss() == out
        h = client.get("/api/health")
        assert h.status_code == 200
        assert h.json()["db"]["account_store"]["last_error"] == err
    assert account_store._user_count() == before


def test_entered_after_registration_snapshot_settled(client, tmp_path, monkeypatch):
    acct = _reg(client, "settled_user")
    account_store.flush()
    assert account_store._TIMER is None
    r = client.post("/api/onboard/enter", json={"uid": acct["uid"]},
                    headers={"Authorization": f"Bearer {acct['token']}"})
    assert r.status_code == 200, r.text
    account_store.flush()
    snapshot = _read(_durable())

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "settled" / "qiyan.db")
    store.init_db()
    r = client.post("/api/auth/signin", json={"username": "settled_user", "password": "secret-ok"})
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is True
    me = _me(client, acct["token"])
    assert me.status_code == 200, me.text
    assert me.json()["entered"] is True

    v1 = {"v": 1, "users": [{k: v for k, v in u.items() if k != "entered_at"} for u in snapshot["users"]]}
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "settled_v1" / "qiyan.db")
    monkeypatch.delenv("QIYAN_ACCOUNT_STORE")
    store.init_db()
    account_store.hydrate(v1)
    r = client.post("/api/auth/signin", json={"username": "settled_user", "password": "secret-ok"})
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is False
