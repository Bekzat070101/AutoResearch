# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth
TOKEN = "a" * 32


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "a.db")
    store.init_db()
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("BACKUP_KEY", "backup-key-16++++")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("QIYAN_SNAP", str(tmp_path / "snap.db"))
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    admin.SNAP = tmp_path / "snap.db"


@pytest.fixture
def client():
    return TestClient(main.app)


def test_admin_requires_token(client):
    assert client.post("/api/admin/backup").status_code == 403
    assert client.post("/api/admin/backup", headers={"X-Admin-Token": "nope"}).status_code == 403
    r = client.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert "users" in body and "expired_sessions" in body
    assert "paper_dir_gaps" in body and "paper_dir_rows" in body


def test_backup_vacuum_and_restore(client, tmp_path):
    import gzip
    import sqlite3
    uid = store.create_user("备份人")["uid"]
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()
    assert out["ok"] and out["integrity"] == "ok" and out["oss"] == "local"
    src = tmp_path / "bk" / out["object"]
    raw = gzip.decompress(admin._aes(src.read_bytes(), b"backup-key-16++++", False))
    dest = tmp_path / "restored.db"
    dest.write_bytes(raw)
    with sqlite3.connect(dest) as c:
        assert c.execute("SELECT nickname FROM users WHERE id=?", (uid,)).fetchone()[0] == "备份人"
    store.create_user("恢复后应消失")
    live = client.post("/api/admin/restore", json={"object": out["object"]},
                       headers={"X-Admin-Token": TOKEN})
    assert live.status_code == 200, live.text
    assert store.get_user(uid)["nickname"] == "备份人"


def test_restore_write_failure_keeps_original_db(client, tmp_path, monkeypatch):
    """16-F01：写恢复包中途失败时原库字节和账号都在。"""
    uid = store.create_user("不能丢")["uid"]
    orig = Path(store.DB_PATH).read_bytes()
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()

    def boom(path, data):
        path.write_bytes(data[:128])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(admin, "_write_restore_bytes", boom)
    r = client.post("/api/admin/restore", json={"object": out["object"]},
                    headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 500
    assert "换库失败" in r.text or "No space" in r.text or "28" in r.text
    assert Path(store.DB_PATH).read_bytes() == orig
    assert store.get_user(uid)["nickname"] == "不能丢"


def test_rewrap_wrong_secret_does_not_mutate_env(client, monkeypatch):
    """16-F02：错 old_secret 不改 QIYAN_SECRET，接口非 200。"""
    monkeypatch.setenv("QIYAN_SECRET", "current-wrap")
    uid = store.create_user("钥")["uid"]
    store.save_llm_secret(uid, "sk-user-key-zzzz", "https://api.deepseek.com/v1", "deepseek-flash")
    r = client.post("/api/admin/rewrap", json={"old_secret": "wrong"},
                    headers={"X-Admin-Token": TOKEN})
    assert r.status_code != 200
    assert os.environ["QIYAN_SECRET"] == "current-wrap"
    assert store.unwrap_secret(
        __import__("sqlite3").connect(str(store.DB_PATH)).execute(
            "SELECT wrapped FROM user_secrets WHERE user_id=?", (uid,)
        ).fetchone()[0]
    ) == "sk-user-key-zzzz"


def test_rewrap_old_secret_rewrites_with_current(client, monkeypatch):
    monkeypatch.setenv("QIYAN_SECRET", "old-wrap")
    uid = store.create_user("钥二")["uid"]
    store.save_llm_secret(uid, "sk-rewrapped-key", "https://api.deepseek.com/v1", "deepseek-flash")
    monkeypatch.setenv("QIYAN_SECRET", "new-wrap")
    r = client.post("/api/admin/rewrap", json={"old_secret": "old-wrap"},
                    headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["rewrapped"] >= 1
    assert os.environ["QIYAN_SECRET"] == "new-wrap"
    assert store.llm_secret(uid)["key"] == "sk-rewrapped-key"


def test_weekly_maintenance_clears_expired_sessions(client):
    uid = store.create_user("过期")["uid"]
    store.create_session(uid, -10)
    r = client.post("/api/admin/maintenance", headers={"X-Admin-Token": TOKEN}).json()
    assert r["ok"]
    assert r["sessions"] >= 1
    after = client.get("/api/admin/stats", headers={"X-Admin-Token": TOKEN}).json()
    assert after["expired_sessions"] == 0


def test_admin_user_delete(client):
    uid = store.create_user("人工删")["uid"]
    r = client.post("/api/admin/user/delete", json={"uid": uid}, headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200
    assert store.get_user(uid) is None


def test_oss_upload_403_is_502(client, monkeypatch):
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_SECRET", "secret")

    def boom(*a, **k):
        raise urllib.error.HTTPError("https://oss", 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 502


def test_oss_upload_ok_flag(client, monkeypatch):
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_SECRET", "secret")

    class Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b"ok"

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp())
    r = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200, r.text
    assert r.json()["oss"] == "ok"


def test_collect_slice_returns_next_node(monkeypatch):
    import paper_collect
    monkeypatch.setattr(store, "ephemeral", lambda: False)
    out = paper_collect.collect(max_seconds=0)
    nodes = list(__import__("interests").table()["nodes"])
    assert out["next_node"] == nodes[0]
    assert out["nodes"] == []


def test_collect_quota_out_of_range(client):
    r = client.post("/api/admin/papers/collect?quota=-1", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 400


def test_collect_lock_blocks_second(client, monkeypatch):
    import paper_collect
    monkeypatch.setattr(store, "ephemeral", lambda: False)
    assert store.try_collect_lock(30)
    r = client.post("/api/admin/papers/collect?node=520.20", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 409
    store.release_collect_lock()
    monkeypatch.setattr(paper_collect, "collect_node", lambda *a, **k: {"node": "520.20"})
    r2 = client.post("/api/admin/papers/collect?node=520.20&max_seconds=240",
                     headers={"X-Admin-Token": TOKEN})
    assert r2.status_code == 200, r2.text
    assert r2.json().get("next_node") == ""


def test_health_gains_checks_and_keeps_ok(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert "checks" in h and "secret_set" in h["checks"]
    assert "model_pool" in h
    assert "threads_max" in h["model_pool"]
    assert h["version"] == "0.2.0"


def test_concurrent_restore_partial_write_keeps_validated_database(client, tmp_path, monkeypatch):
    """20-R01：A 校验后暂停、B 写坏自己的 stage，A 继续换入的仍是完整库。"""
    import sqlite3
    import threading
    uid = store.create_user("并发恢复")["uid"]
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()
    validated, b_done = threading.Event(), threading.Event()
    real_write, real_check = admin._write_restore_bytes, admin._check_integrity_and_close
    calls = {"write": 0, "check": 0}

    def write(path, data):
        calls["write"] += 1
        if calls["write"] == 2:
            path.write_bytes(data[:128])
            raise OSError(28, "No space left on device")
        real_write(path, data)

    def check(path):
        calls["check"] += 1
        real_check(path)
        if calls["check"] == 1:
            validated.set()
            assert b_done.wait(10)

    monkeypatch.setattr(admin, "_write_restore_bytes", write)
    monkeypatch.setattr(admin, "_check_integrity_and_close", check)
    res = {}

    def run_a():
        res["a"] = client.post("/api/admin/restore", json={"object": out["object"]},
                               headers={"X-Admin-Token": TOKEN})

    t = threading.Thread(target=run_a)
    t.start()
    assert validated.wait(10)
    rb = client.post("/api/admin/restore", json={"object": out["object"]},
                     headers={"X-Admin-Token": TOKEN})
    b_done.set()
    t.join(15)
    assert rb.status_code == 500
    assert res["a"].status_code == 200, res["a"].text
    with sqlite3.connect(str(store.DB_PATH)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert store.get_user(uid)["nickname"] == "并发恢复"
    assert not list(Path(store.DB_PATH).parent.glob("*.incoming*"))


def test_restore_cleanup_error_reports_applied(client, tmp_path, monkeypatch):
    """20-R01：换入成功后 sidecar 清理失败报 200 + cleanup_pending。"""
    uid = store.create_user("已换入")["uid"]
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()
    store.create_user("恢复后应消失")
    real_unlink = Path.unlink

    def unlink(self, *a, **k):
        if str(self).endswith("-wal"):
            raise PermissionError(13, "Permission denied")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", unlink)
    r = client.post("/api/admin/restore", json={"object": out["object"]},
                    headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["object"] == out["object"]
    assert body["cleanup_pending"] == ["-wal"]
    assert body["warning"] == "恢复已换入；WAL/SHM 清理待重试"
    assert store.get_user(uid)["nickname"] == "已换入"


def test_restore_waits_for_open_connections(client, tmp_path):
    """20-R01：有连接没排空时 5 秒内 503，旧库不变。"""
    import time as _time
    uid = store.create_user("旧库")["uid"]
    out = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN}).json()
    store.create_user("恢复不应执行")
    orig = Path(store.DB_PATH).read_bytes()
    with store._conn() as c:
        c.execute("SELECT COUNT(*) FROM users").fetchone()
        t0 = _time.monotonic()
        r = client.post("/api/admin/restore", json={"object": out["object"]},
                        headers={"X-Admin-Token": TOKEN})
        elapsed = _time.monotonic() - t0
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "数据库仍在使用，请稍后重试恢复"
    assert elapsed < 6.5
    assert Path(store.DB_PATH).read_bytes() == orig
    assert store.get_user(uid)["nickname"] == "旧库"


def test_rewrap_concurrent_mine_save_preserves_latest_ciphertext(client, monkeypatch):
    """20-R04：解封旧密文后学生重存新 key，rewrap 不得覆盖。"""
    monkeypatch.setenv("QIYAN_SECRET", "old-wrap")
    uid = store.create_user("并发换钥")["uid"]
    store.save_llm_secret(uid, "sk-old-1111", "https://old.example/v1", "old-model")
    monkeypatch.setenv("QIYAN_SECRET", "new-wrap")
    real_unwrap = store.unwrap_secret
    hits = {"n": 0}

    def unwrap(hexed, secret=None):
        plain = real_unwrap(hexed, secret=secret)
        if secret == "old-wrap" and hits["n"] == 0:
            hits["n"] += 1
            store.save_llm_secret(uid, "sk-new-9999", "https://new.example/v1", "new-model")
        return plain

    monkeypatch.setattr(store, "unwrap_secret", unwrap)
    r = client.post("/api/admin/rewrap", json={"old_secret": "old-wrap"},
                    headers={"X-Admin-Token": TOKEN})
    monkeypatch.setattr(store, "unwrap_secret", real_unwrap)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["ok"] is False and detail["code"] == "secret_changed"
    assert detail["changed"] == [uid] and detail["rewrapped"] == 0 and detail["failed"] == []
    assert store.llm_secret(uid)["key"] == "sk-new-9999"
    meta = store.llm_secret_meta(uid)
    assert meta["tail"] == "9999" and meta["base_url"] == "https://new.example/v1"
    assert meta["model"] == "new-model"
    assert os.environ["QIYAN_SECRET"] == "new-wrap"


def test_oss_failure_has_false_ok_contract(client, monkeypatch):
    """D29：OSS 上传失败 502 回包精确为 ok:false + detail；成功仍 200/oss:ok。"""
    monkeypatch.setenv("BACKUP_OSS_BUCKET", "qiyan-bak")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_ID", "id")
    monkeypatch.setenv("BACKUP_OSS_ACCESS_KEY_SECRET", "secret")

    def boom(*a, **k):
        raise urllib.error.HTTPError("https://oss", 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN})
    assert r.status_code == 502
    assert r.json() == {"ok": False, "detail": "OSS 上传失败：HTTPError"}

    class Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b"ok"

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Resp())
    ok = client.post("/api/admin/backup", headers={"X-Admin-Token": TOKEN})
    assert ok.status_code == 200, ok.text
    assert ok.json()["oss"] == "ok"
