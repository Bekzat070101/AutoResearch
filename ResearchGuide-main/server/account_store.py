# -*- coding: utf-8 -*-
"""已注册账号的加密副本：用户名 + 密码哈希，不含明文密码。

线上 SQLite 若在 /tmp，发版或实例回收会换一份空库，登录就会变成「用户名或密码不对」，
只能再注册。把副本放到 QIYAN_ACCOUNT_STORE（持久盘上的独立文件）或 OSS durable/ 前缀，
新空库启动时灌回同一批 uid / 用户名 / 哈希，原来的密码仍能登录。

OSS durable/ 不要走 daily/：daily/ 有 7 天生命周期，到期会把账号副本一起删掉。
"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

import store
from schemas import now_iso

OSS_PREFIX = "durable"
OSS_OBJECT = "accounts.json.enc"
_COLS = (
    "id", "nickname", "username", "password_salt", "password_hash",
    "created_at", "claimed_at", "consent_at", "consent_version",
    "wechat_openid", "onboard_state",
)


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
        registered = _registered_count()
    except sqlite3.Error:
        registered = 0
    return {"enabled": enabled(), "via": via, "registered": registered}


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
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("users"), list):
        raise ValueError("account_store_shape")
    return data


def _registered_count() -> int:
    with store._conn() as c:
        return int(c.execute(
            "SELECT COUNT(*) FROM users WHERE username IS NOT NULL AND username != ''"
        ).fetchone()[0])


def export_registered() -> list[dict[str, Any]]:
    cols = ", ".join(_COLS)
    with store._conn() as c:
        rows = c.execute(
            f"SELECT {cols} FROM users WHERE username IS NOT NULL AND username != ''"
        ).fetchall()
    out = []
    for r in rows:
        item = {k: r[k] for k in _COLS}
        if not item.get("username") or not item.get("password_salt") or not item.get("password_hash"):
            continue
        out.append(item)
    return out


def dump_accounts() -> dict[str, Any]:
    """注册 / 升级 / 删号后调用。失败只打日志，不让注册接口 500。"""
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    try:
        users = export_registered()
        blob = _encrypt({"v": 1, "saved_at": now_iso(), "users": users})
        via: list[str] = []
        path = file_path()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
            via.append("file")
        if oss_enabled():
            _put_oss(blob)
            via.append("oss")
        print(json.dumps({"event": "account_store_dump", "n": len(users), "via": via},
                         ensure_ascii=False))
        return {"ok": True, "n": len(users), "via": via}
    except Exception as exc:
        print(json.dumps({"event": "account_store_dump_failed", "error": type(exc).__name__},
                         ensure_ascii=False))
        return {"ok": False, "reason": type(exc).__name__}


def hydrate_if_empty() -> dict[str, Any]:
    """空库（没有已注册用户）时从副本灌回。已有注册用户则以库为准，不覆盖。"""
    if not enabled():
        return {"skipped": "disabled"}
    if _registered_count() > 0:
        return {"skipped": "has_users"}
    loaded = load_snapshot()
    if not loaded:
        return {"skipped": "no_snapshot"}
    payload, via = loaded
    n = import_users(payload["users"])
    print(json.dumps({"event": "account_store_hydrated", "n": n, "via": via}, ensure_ascii=False))
    return {"ok": True, "n": n, "via": via}


def load_snapshot() -> tuple[dict[str, Any], str] | None:
    sources: list[tuple[str, bytes]] = []
    path = file_path()
    if path and path.exists():
        sources.append(("file", path.read_bytes()))
    if oss_enabled():
        blob = _get_oss()
        if blob:
            sources.append(("oss", blob))
    for via, blob in sources:
        try:
            return _decrypt(blob), via
        except Exception:
            print(json.dumps({"event": "account_store_invalid", "via": via}, ensure_ascii=False))
    return None


def import_users(rows: list[Any]) -> int:
    n = 0
    with store._LOCK, store._conn() as c:
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            if _insert_user(c, raw):
                n += 1
    return n


def _insert_user(c, row: dict[str, Any]) -> bool:
    uid = str(row.get("id") or "").strip()
    username = store.normalize_username(str(row.get("username") or ""))
    salt = str(row.get("password_salt") or "")
    hashed = str(row.get("password_hash") or "")
    if not uid or not store.valid_username(username) or not salt or not hashed:
        return False
    if c.execute("SELECT 1 FROM users WHERE id=? OR username=?", (uid, username)).fetchone():
        return False
    nick = (str(row.get("nickname") or username).strip() or username)[:24]
    now = now_iso()
    try:
        c.execute(
            "INSERT INTO users(id, nickname, token, onboard_state, created_at, claimed_at, "
            "username, password_salt, password_hash, consent_at, consent_version, wechat_openid) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid, nick, secrets.token_hex(16),
                row.get("onboard_state") or "{}",
                row.get("created_at") or now,
                row.get("claimed_at") or now,
                username, salt, hashed,
                row.get("consent_at"),
                row.get("consent_version") or "",
                row.get("wechat_openid") or None,
            ),
        )
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
    import urllib.request
    try:
        with urllib.request.urlopen(_oss_signed("GET"), timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        if "404" in str(exc) or "NoSuchKey" in str(exc):
            return None
        print(json.dumps({"event": "account_store_oss_get_failed", "error": type(exc).__name__},
                         ensure_ascii=False))
        return None
