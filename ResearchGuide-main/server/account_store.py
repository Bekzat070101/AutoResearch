# -*- coding: utf-8 -*-
"""已注册账号的加密副本：用户名 + 密码哈希，不含明文密码。

线上 SQLite 若在 /tmp，发版或实例回收会换一份空库，登录就会变成「用户名或密码不对」，
只能再注册。把副本放到 QIYAN_ACCOUNT_STORE（持久盘上的独立文件）或 OSS durable/ 前缀，
新空库启动时灌回同一批 uid / 用户名 / 哈希，原来的密码仍能登录。

OSS durable/ 不要走 daily/：daily/ 有 7 天生命周期，到期会把账号副本一起删掉。
"""
from __future__ import annotations

import atexit
import gzip
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import logs
import store
from schemas import now_iso

OSS_PREFIX = "durable"
OSS_OBJECT = "accounts.json.enc"
_COLS = (
    "id", "nickname", "username", "password_salt", "password_hash",
    "created_at", "claimed_at", "consent_at", "consent_version",
    "wechat_openid", "onboard_state", "is_admin",
)
_SESSION_COLS = ("token_hash", "user_id", "created_at", "expires_at", "last_seen")
COALESCE_SECONDS = 2.0

_STATE_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()
_TIMER: threading.Timer | None = None
_LAST_WRITE = 0.0
_STATE: dict[str, Any] = {}


def _fresh_state() -> dict[str, Any]:
    return {"snapshot_users": None, "snapshot_sessions": None, "last_dump_at": None,
            "last_hydrate_at": None, "last_error": None, "collisions": [], "file_saved_at": None}


_STATE.update(_fresh_state())


def _key() -> bytes | None:
    raw = (os.environ.get("BACKUP_KEY") or os.environ.get("QIYAN_SECRET") or "").encode()
    if len(raw) < 16:
        return None
    return raw


def file_path() -> Path | None:
    raw = (os.environ.get("QIYAN_ACCOUNT_STORE") or "").strip()
    return Path(raw) if raw else None


def oss_enabled() -> bool:
    return bool((os.environ.get("BACKUP_OSS_BUCKET") or "").strip())


def enabled() -> bool:
    return bool(file_path() or oss_enabled()) and _key() is not None


def status() -> dict[str, Any]:
    via = []
    if file_path():
        via.append("file")
    if oss_enabled():
        via.append("oss")
    try:
        if store.db_blocked():
            raise OSError("db_blocked")
        registered = _registered_count()
        users = _user_count()
    except (sqlite3.Error, OSError):
        # 库打不开（路径只读、盘没挂上）时 health 也不能 500：探活本身就是用来发现这种事的
        registered = users = None
    with _STATE_LOCK:
        st = dict(_STATE)
    return {"enabled": enabled(), "via": via, "registered": registered, "users": users,
            "snapshot_users": st["snapshot_users"], "snapshot_sessions": st["snapshot_sessions"],
            "last_dump_at": st["last_dump_at"], "last_hydrate_at": st["last_hydrate_at"],
            "last_error": st["last_error"], "collisions": len(st["collisions"])}


def snapshot_age() -> float:
    with _STATE_LOCK:
        at = _STATE["last_dump_at"]
    return -1.0 if at is None else max(0.0, time.time() - at)


def snapshot_users() -> int:
    with _STATE_LOCK:
        return int(_STATE["snapshot_users"] or 0)


def note_error(kind: str) -> None:
    with _STATE_LOCK:
        _STATE["last_error"] = kind


def reset() -> None:
    """丢掉还没写的合并 dump、清空计时与可观测状态（换库 / 启动时用）。"""
    global _TIMER, _LAST_WRITE
    with _STATE_LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = None
        _LAST_WRITE = 0.0
        _STATE.clear()
        _STATE.update(_fresh_state())


def _aes(data: bytes, key: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import hashlib
    k = hashlib.sha256(key).digest()
    if encrypt:
        nonce = os.urandom(12)
        return nonce + AESGCM(k).encrypt(nonce, data, None)
    return AESGCM(k).decrypt(data[:12], data[12:], None)


def _encrypt(payload: dict[str, Any]) -> bytes:
    key = _key()
    if not key:
        raise RuntimeError("account_store_no_key")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _aes(gzip.compress(raw), key, True)


def _decrypt(blob: bytes) -> dict[str, Any]:
    key = _key()
    if not key:
        raise RuntimeError("account_store_no_key")
    data = json.loads(gzip.decompress(_aes(blob, key, False)))
    if not isinstance(data, dict) or data.get("v") not in (1, 2) or not isinstance(data.get("users"), list):
        raise ValueError("account_store_shape")
    if data.get("v") == 2 and not isinstance(data.get("sessions", []), list):
        raise ValueError("account_store_shape")
    return data


def _user_count() -> int:
    """users 表全部行（含访客）：换库前后核数用这个，不用只数用户名账号的 registered。"""
    with store._conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def _registered_count() -> int:
    with store._conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM users WHERE username IS NOT NULL AND username != ''"
        ).fetchone()[0])


def export_snapshot() -> dict[str, Any]:
    """users 表全部行 + 未过期会话（只有令牌哈希）。不含 user_secrets、消息等。"""
    with store._conn() as c:
        users = [{k: r[k] for k in _COLS}
                 for r in c.execute(f"SELECT {', '.join(_COLS)} FROM users")]
        entered: dict[str, Any] = {}
        for r in c.execute("SELECT user_id, data FROM user_prefs"):
            try:
                data = json.loads(r["data"] or "{}")
            except json.JSONDecodeError:
                data = {}
            if isinstance(data, dict):
                entered[r["user_id"]] = data.get("entered_at")
        for u in users:
            u["entered_at"] = entered.get(u["id"])
        sessions = [{k: r[k] for k in _SESSION_COLS}
                    for r in c.execute(f"SELECT {', '.join(_SESSION_COLS)} FROM sessions WHERE expires_at>?",
                                       (time.time(),))]
        revoked = [{"token_hash": r["token_hash"], "revoked_at": r["revoked_at"]}
                   for r in c.execute("SELECT token_hash, revoked_at FROM revoked_sessions")]
        deleted = [{"id": r["id"], "deleted_at": r["deleted_at"]}
                   for r in c.execute("SELECT id, deleted_at FROM deleted_users")]
    return {"v": 2, "saved_at": now_iso(), "users": users, "sessions": sessions,
            "revoked": revoked, "deleted": deleted}


def _write_atomic(path: Path, blob: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_bytes(blob)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _set_aside_undecryptable(path: Path) -> None:
    """旧副本用当前密钥解不开（密钥换了）：改名留着，不被新副本覆盖。"""
    if not path.exists():
        return
    try:
        _decrypt(path.read_bytes())
        return
    except Exception:
        pass
    note_error("invalid")
    aside = path.with_name(f"{path.name}.undecryptable-{datetime.now().strftime('%Y%m%d')}")
    if aside.exists():
        aside = path.with_name(f"{aside.name}-{os.getpid()}-{int(time.time())}")
    os.replace(path, aside)
    logs.emit({"event": "account_store_set_aside", "to": aside.name})


def _dump_now() -> dict[str, Any]:
    global _LAST_WRITE
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    with _WRITE_LOCK:
        with _STATE_LOCK:
            _LAST_WRITE = time.monotonic()
        try:
            payload = export_snapshot()
            blob = _encrypt(payload)
            via: list[str] = []
            path = file_path()
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                _set_aside_undecryptable(path)
                _write_atomic(path, blob)
                with _STATE_LOCK:
                    _STATE["file_saved_at"] = payload["saved_at"]
                via.append("file")
            if oss_enabled():
                _put_oss(blob)
                via.append("oss")
            with _STATE_LOCK:
                _STATE["snapshot_users"] = len(payload["users"])
                _STATE["snapshot_sessions"] = len(payload["sessions"])
                _STATE["last_dump_at"] = time.time()
            logs.emit({"event": "account_store_dump", "n": len(payload["users"]),
                       "sessions": len(payload["sessions"]), "via": via})
            return {"ok": True, "n": len(payload["users"]), "via": via}
        except Exception as exc:
            note_error(type(exc).__name__)
            logs.emit({"event": "account_store_dump_failed", "error": type(exc).__name__})
            return {"ok": False, "reason": type(exc).__name__}


def _fire() -> None:
    global _TIMER
    with _STATE_LOCK:
        _TIMER = None
    _dump_now()


def dump_accounts(immediate: bool = False) -> dict[str, Any]:
    """账号或会话变了就调。2 秒内的连续触发合并成一次全量重写；失败只打日志，不让接口 500。"""
    global _TIMER
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    with _STATE_LOCK:
        if immediate:
            if _TIMER is not None:
                _TIMER.cancel()
                _TIMER = None
            wait = 0.0
        else:
            if _TIMER is not None:
                return {"ok": True, "pending": True}
            wait = _LAST_WRITE + COALESCE_SECONDS - time.monotonic()
            if wait > 0:
                _TIMER = threading.Timer(wait, _fire)
                _TIMER.daemon = True
                _TIMER.start()
                return {"ok": True, "pending": True}
    return _dump_now()


def flush() -> dict[str, Any] | None:
    """有合并中还没写的 dump 就马上写（进程退出前也走这里）。"""
    global _TIMER
    with _STATE_LOCK:
        pending = _TIMER is not None
        if pending:
            _TIMER.cancel()
            _TIMER = None
    return _dump_now() if pending else None


atexit.register(flush)


def hydrate_file() -> dict[str, Any]:
    """启动时（init_db）从本地副本文件合并灌回。OSS 的拉取不在这里，见 hydrate_oss。"""
    reset()
    if not enabled():
        return {"skipped": "disabled"}
    path = file_path()
    if not path or not path.exists():
        return {"skipped": "no_snapshot"}
    try:
        payload = _decrypt(path.read_bytes())
    except Exception:
        note_error("invalid")
        logs.emit({"event": "account_store_invalid", "via": "file"})
        return {"skipped": "invalid"}
    with _STATE_LOCK:
        _STATE["file_saved_at"] = str(payload.get("saved_at") or "")
    return _hydrate_logged(payload, "file")


def fetch_oss_payload() -> dict[str, Any] | None:
    """只取 + 解密，不灌库：main._lifespan 在线程池里跑它，超时就丢掉结果。没有副本或解不开返回 None；取的异常上抛。"""
    blob = _get_oss()
    if not blob:
        return None
    try:
        return _decrypt(blob)
    except Exception:
        note_error("invalid")
        logs.emit({"event": "account_store_invalid", "via": "oss"})
        return None


def apply_oss_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """新鲜度比较与灌回在同一把 _WRITE_LOCK 里：比较之后、灌回之前落下的本地 dump 不会被旧 OSS 越过。"""
    with _WRITE_LOCK:
        with _STATE_LOCK:
            file_at = _STATE["file_saved_at"]
        if file_at and str(payload.get("saved_at") or "") <= file_at:
            # 本地文件副本更新（OSS 那次 put 失败过）：旧副本不往回灌，免得把已删的号复活
            logs.emit({"event": "account_store_oss_stale", "oss": payload.get("saved_at"), "file": file_at})
            return {"skipped": "older_than_file"}
        return _hydrate_logged(payload, "oss")


def hydrate_oss() -> dict[str, Any]:
    """同步入口：取到就灌（测试与手动重试用）；失败只记日志与 health。"""
    if not enabled() or not oss_enabled():
        return {"skipped": "disabled"}
    try:
        payload = fetch_oss_payload()
    except Exception as exc:
        note_error(type(exc).__name__)
        return {"skipped": type(exc).__name__}
    if payload is None:
        return {"skipped": "no_snapshot"}
    return apply_oss_payload(payload)


def _hydrate_logged(payload: dict[str, Any], via: str) -> dict[str, Any]:
    try:
        out = hydrate(payload)
    except Exception as exc:
        note_error(type(exc).__name__)
        logs.emit({"event": "account_store_hydrate_failed", "error": type(exc).__name__})
        return {"skipped": type(exc).__name__}
    logs.emit({"event": "account_store_hydrated", "via": via, **out})
    return {"ok": True, "via": via, **out}


def hydrate(payload: dict[str, Any]) -> dict[str, Any]:
    """合并灌回：users 按 id 不存在才插；用户名被别的 uid 占了记 collision 不插；
    sessions 按 token_hash 不存在、用户在库里、未过期才插。v1 快照没有 sessions。"""
    collisions: list[str] = []
    users = sessions = tombstoned = 0
    entered: list[tuple[str, Any]] = []
    with store._LOCK, store._conn() as c:
        for raw in payload.get("deleted") or []:
            if isinstance(raw, dict) and str(raw.get("id") or "").strip():
                c.execute("INSERT OR IGNORE INTO deleted_users(id, deleted_at) VALUES(?, ?)",
                          (str(raw["id"]).strip(), str(raw.get("deleted_at") or now_iso())))
        for raw in payload.get("revoked") or []:
            if not isinstance(raw, dict) or not str(raw.get("token_hash") or ""):
                continue
            try:
                at = float(raw.get("revoked_at"))
            except (TypeError, ValueError):
                at = time.time()
            c.execute("INSERT OR IGNORE INTO revoked_sessions(token_hash, revoked_at) VALUES(?, ?)",
                      (str(raw["token_hash"]), at))
        for raw in payload.get("users") or []:
            if not isinstance(raw, dict):
                continue
            if c.execute("SELECT 1 FROM deleted_users WHERE id=?", (str(raw.get("id") or "").strip(),)).fetchone():
                tombstoned += 1
                continue
            if _insert_user_if_missing(c, raw, collisions):
                users += 1
                if raw.get("entered_at"):
                    entered.append((str(raw.get("id") or "").strip(), raw.get("entered_at")))
        for raw in payload.get("sessions") or []:
            if isinstance(raw, dict) and _insert_session_if_missing(c, raw):
                sessions += 1
    for uid, v in entered:
        store.set_prefs(uid, extra={"entered_at": v})
    for uid in collisions:
        logs.emit({"event": "account_store_collision", "uid": uid})
    with _STATE_LOCK:
        _STATE["collisions"] = _STATE["collisions"] + collisions
        _STATE["snapshot_users"] = len(payload.get("users") or [])
        _STATE["snapshot_sessions"] = len(payload.get("sessions") or [])
        _STATE["last_hydrate_at"] = time.time()
    return {"users": users, "sessions": sessions, "collisions": len(collisions), "tombstoned": tombstoned}


def _insert_user_if_missing(c, row: dict[str, Any], collisions: list[str]) -> bool:
    uid = str(row.get("id") or "").strip()
    if not uid:
        return False
    if c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return False
    username = store.normalize_username(str(row.get("username") or "")) or None
    salt = str(row.get("password_salt") or "") or None
    hashed = str(row.get("password_hash") or "") or None
    if username is not None:
        if not store.valid_username(username) or not salt or not hashed:
            return False
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            collisions.append(uid)
            return False
    now = now_iso()
    nick = (str(row.get("nickname") or username or "").strip() or username or "访客")[:24]
    try:
        c.execute(
            "INSERT INTO users(id, nickname, token, onboard_state, created_at, claimed_at, "
            "username, password_salt, password_hash, consent_at, consent_version, wechat_openid, is_admin) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, nick, secrets.token_hex(16),
                row.get("onboard_state") or "{}",
                row.get("created_at") or now,
                row["claimed_at"] if "claimed_at" in row else now,  # 显式 NULL 是「还没认领的旧 uid」，不能替人认领
                username, salt, hashed,
                row.get("consent_at"),
                row.get("consent_version") or "",
                row.get("wechat_openid") or None,
                1 if row.get("is_admin") else 0,  # 旧快照没有这一列，当 0
            ),
        )
    except sqlite3.IntegrityError:
        collisions.append(uid)  # 多半是 wechat_openid 被别的 uid 占了（换库后那个人用微信重新进过）：也算冲突，不静默
        return False
    return True


def _insert_session_if_missing(c, row: dict[str, Any]) -> bool:
    h = str(row.get("token_hash") or "")
    uid = str(row.get("user_id") or "")
    try:
        expires = float(row.get("expires_at"))
        last_seen = float(row.get("last_seen") or 0)
    except (TypeError, ValueError):
        return False
    if not h or not uid or expires <= time.time():
        return False
    if c.execute("SELECT 1 FROM sessions WHERE token_hash=?", (h,)).fetchone():
        return False
    if c.execute("SELECT 1 FROM revoked_sessions WHERE token_hash=?", (h,)).fetchone():
        return False
    if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
        return False
    try:
        c.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen) VALUES(?,?,?,?,?)",
                  (h, uid, row.get("created_at") or now_iso(), expires, last_seen))
    except sqlite3.IntegrityError:
        return False
    return True


def _oss_signed(method: str, blob: bytes | None = None):
    import base64
    import hashlib
    import urllib.request
    from email.utils import formatdate
    bucket = (os.environ.get("BACKUP_OSS_BUCKET") or "").strip()
    key_id = (os.environ.get("BACKUP_OSS_ACCESS_KEY_ID") or os.environ.get("OSS_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("BACKUP_OSS_ACCESS_KEY_SECRET") or os.environ.get("OSS_ACCESS_KEY_SECRET") or "").strip()
    if not bucket or not key_id or not secret:
        raise RuntimeError("OSS 未配置")
    endpoint = (os.environ.get("BACKUP_OSS_ENDPOINT") or "https://oss-cn-hangzhou.aliyuncs.com").rstrip("/")
    date = formatdate(usegmt=True)
    content_type = "application/octet-stream"
    resource = f"/{bucket}/{OSS_PREFIX}/{OSS_OBJECT}"
    to_sign = f"{method}\n\n{content_type}\n{date}\n{resource}"
    sig = base64.b64encode(hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()).decode()
    headers = {
        "Content-Type": content_type,
        "Date": date,
        "Authorization": f"OSS {key_id}:{sig}",
    }
    url = f"{endpoint}{resource}"
    return urllib.request.Request(url, data=blob, method=method, headers=headers)


def _put_oss(blob: bytes) -> None:
    import urllib.request
    urllib.request.urlopen(_oss_signed("PUT", blob), timeout=30)


def _get_oss() -> bytes | None:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(_oss_signed("GET"), timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            return None
        logs.emit({"event": "account_store_oss_get_failed", "error": type(exc).__name__})
        raise
