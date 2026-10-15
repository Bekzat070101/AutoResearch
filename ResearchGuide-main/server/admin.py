# -*- coding: utf-8 -*-
"""管理接口：自己核 X-Admin-Token，列在 auth.PUBLIC。"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import store
from schemas import now_iso

router = APIRouter()
SNAP = Path(os.environ.get("QIYAN_SNAP") or "/tmp/qiyan-snap.db")


def _admin(request: Request) -> None:
    token = os.environ.get("ADMIN_TOKEN") or ""
    if len(token) < 32:
        raise HTTPException(503, "ADMIN_TOKEN 未设或短于 32 字节")
    got = request.headers.get("X-Admin-Token", "")
    if hmac.compare_digest(got, token):
        return
    if not auth._ip_allows(request, "admin"):
        raise HTTPException(429, "太频繁")
    raise HTTPException(403, "管理员口令不对")


def _aes(data: bytes, key: bytes, encrypt: bool) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import hashlib
    k = hashlib.sha256(key).digest()
    if encrypt:
        nonce = os.urandom(12)
        return nonce + AESGCM(k).encrypt(nonce, data, None)
    return AESGCM(k).decrypt(data[:12], data[12:], None)


def make_backup() -> dict:
    key = (os.environ.get("BACKUP_KEY") or "").encode()
    if len(key) < 16:
        raise HTTPException(503, "BACKUP_KEY 未设或太短")
    SNAP.parent.mkdir(parents=True, exist_ok=True)
    if SNAP.exists():
        SNAP.unlink()
    dest = str(SNAP).replace("\\", "/")
    with store._LOCK, store._conn() as c:
        c.execute(f"VACUUM INTO '{dest}'")
        ok = c.execute("PRAGMA integrity_check").fetchone()[0]
    if ok != "ok":
        raise HTTPException(500, f"integrity_check={ok}")
    raw = SNAP.read_bytes()
    blob = _aes(gzip.compress(raw), key, True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out_dir = Path(os.environ.get("BACKUP_DIR") or "/tmp/qiyan-backup")
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"qiyan-{stamp}.db.gz.enc"
    dest_path = out_dir / name
    dest_path.write_bytes(blob)
    oss = _put_oss(name, blob)
    print(json.dumps({"event": "backup", "ok": True, "bytes": len(blob), "object": name, "oss": oss},
                     ensure_ascii=False))
    return {"ok": True, "bytes": len(blob), "object": name, "oss": oss, "integrity": "ok"}


def _put_oss(name: str, blob: bytes) -> str:
    """有 RAM 角色和桶名才上传；没有就只落本地，不假装已经进 OSS。"""
    bucket = os.environ.get("BACKUP_OSS_BUCKET") or ""
    if not bucket:
        return "local"
    try:
        import urllib.request
        endpoint = os.environ.get("BACKUP_OSS_ENDPOINT") or "https://oss-cn-hangzhou.aliyuncs.com"
        req = urllib.request.Request(
            f"{endpoint.rstrip('/')}/{bucket}/daily/{name}", data=blob, method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        urllib.request.urlopen(req, timeout=30)
        return "oss"
    except Exception as exc:
        print(json.dumps({"event": "backup", "ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        return "local"


@router.post("/api/admin/backup")
def backup(request: Request):
    _admin(request)
    return make_backup()


class RestoreReq(BaseModel):
    object: str = ""


@router.post("/api/admin/restore")
def restore(request: Request, object: str = "", req: RestoreReq | None = None):
    _admin(request)
    name = object or (req.object if req else "")
    if not name:
        raise HTTPException(400, "要 object= 备份文件名")
    key = (os.environ.get("BACKUP_KEY") or "").encode()
    if len(key) < 16:
        raise HTTPException(503, "BACKUP_KEY 未设或太短")
    src = Path(os.environ.get("BACKUP_DIR") or "/tmp/qiyan-backup") / name
    if not src.exists():
        raise HTTPException(404, "本地没有这份备份；OSS 下载尚未接通")
    raw = gzip.decompress(_aes(src.read_bytes(), key, False))
    tmp = Path(str(store.DB_PATH) + ".restoring")
    tmp.write_bytes(raw)
    conn = sqlite3.connect(str(tmp))
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise HTTPException(500, "恢复包 integrity_check 失败")
    finally:
        conn.close()
    target = Path(store.DB_PATH)
    with store._LOCK, store._conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for extra in (str(target) + "-wal", str(target) + "-shm"):
        Path(extra).unlink(missing_ok=True)
    last = None
    for _ in range(8):
        try:
            shutil.copyfile(tmp, target)
            last = None
            break
        except OSError as exc:
            last = exc
            time.sleep(0.05)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last:
        raise HTTPException(500, f"换库失败：{last}") from last
    return {"ok": True, "object": name}


@router.post("/api/admin/papers/collect")
def papers_collect(request: Request, node: str = "", quota: int = 20, fulltext: int = 8):
    _admin(request)
    import paper_collect
    try:
        return paper_collect.collect(node=node, quota=quota, fulltext=fulltext, xlate=True)
    except paper_collect.CollectError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/admin/papers/warm")
def papers_warm(request: Request, budget: int = 240):
    _admin(request)
    import reading
    t0 = time.time()
    done, failed = 0, 0
    targets = reading.warm_targets()
    for aid, pinned, hint in targets:
        if time.time() - t0 > budget:
            break
        try:
            reading.arxiv.fulltext(aid, hint=hint or None)
            if pinned:
                row = store.get_paper_text(aid)
                if row:
                    store.put_paper_text(aid, row, pinned=True)
            done += 1
        except Exception:
            failed += 1
    remaining = max(0, len(targets) - done - failed)
    return {"done": done, "failed": failed, "remaining": remaining}


@router.get("/api/admin/stats")
def stats(request: Request):
    _admin(request)
    db = Path(store.DB_PATH)
    wal = Path(str(db) + "-wal")
    papers = Path(__file__).resolve().parent / "data" / "papers"
    cache_files = list(papers.glob("*")) if papers.exists() else []
    try:
        with store._conn() as c:
            cache_n = c.execute("SELECT COUNT(*) FROM paper_text").fetchone()[0]
            cache_b = c.execute("SELECT COALESCE(SUM(bytes),0) FROM paper_text").fetchone()[0]
            user_n = c.execute("SELECT COUNT(*) FROM user_papers").fetchone()[0]
        cache_files_n, cache_bytes = cache_n, cache_b
    except Exception:
        cache_files_n, cache_bytes, user_n = len(cache_files), sum(f.stat().st_size for f in cache_files if f.is_file()), 0
    with store._conn() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        now = time.time()
        expired = c.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (now,)).fetchone()[0]
        secrets_n = c.execute("SELECT COUNT(*) FROM user_secrets").fetchone()[0]
        freelist = c.execute("PRAGMA freelist_count").fetchone()[0]
        usage = []
        try:
            usage = [dict(r) for r in c.execute(
                "SELECT user_id, SUM(calls) calls FROM llm_usage WHERE day=date('now','+8 hours') "
                "AND user_id NOT LIKE '*%' GROUP BY user_id ORDER BY calls DESC LIMIT 10")]
        except sqlite3.OperationalError:
            usage = []
        star, guest, xlate = 0, 0, 0
        try:
            star = c.execute(
                "SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE day=date('now','+8 hours') AND user_id='*'").fetchone()[0]
            guest = c.execute(
                "SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE day=date('now','+8 hours') AND user_id='*guest'").fetchone()[0]
            xlate = c.execute(
                "SELECT COALESCE(SUM(calls),0) FROM llm_usage WHERE day=date('now','+8 hours') AND user_id='*xlate'").fetchone()[0]
        except sqlite3.OperationalError:
            pass
    tmp = Path("/tmp")
    tmp_free = 0
    if tmp.exists():
        usage_tv = os.statvfs(tmp) if hasattr(os, "statvfs") else None
        if usage_tv:
            tmp_free = usage_tv.f_bavail * usage_tv.f_frsize
    build = ""
    for cand in (Path(__file__).resolve().parent.parent / "BUILD", Path(__file__).resolve().parent / "BUILD"):
        if cand.exists():
            build = cand.read_text(encoding="utf-8").strip()
            break
    return {
        "users": users, "sessions": sessions, "expired_sessions": expired,
        "user_secrets": secrets_n, "db_bytes": db.stat().st_size if db.exists() else 0,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0, "freelist_count": freelist,
        "arxiv_files": cache_files_n, "arxiv_bytes": cache_bytes, "user_papers": user_n,
        "tmp_free": tmp_free, "today_star": star, "today_guest": guest, "xlate_today": xlate, "top_uids": usage,
        "build": build, "ephemeral": store.ephemeral(),
        "pools": {"threads": int(os.environ.get("QIYAN_THREADS") or 128)},
        **_paper_dir_stats(),
    }


def _paper_dir_stats() -> dict:
    try:
        import interests
        counts = store.paper_dir_counts()
        thin = set(interests.table().get("thin") or [])
        gaps = []
        for nid in interests.table().get("nodes") or {}:
            need = 8 if nid in thin else 20
            n = int(counts.get(nid) or 0)
            if n < need:
                gaps.append({"node": nid, "dir_n": n, "need": need, "thin": nid in thin})
        return {
            "paper_dir_rows": sum(counts.values()),
            "paper_dir_nodes": len(counts),
            "paper_dir_gaps": gaps,
        }
    except Exception:
        return {"paper_dir_rows": 0, "paper_dir_nodes": 0, "paper_dir_gaps": []}


@router.post("/api/admin/maintenance")
def weekly(request: Request):
    _admin(request)
    now = time.time()
    with store._LOCK, store._conn() as c:
        try:
            c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass
        sessions = 0
        for _ in range(6):
            try:
                sessions = c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,)).rowcount
                break
            except sqlite3.OperationalError:
                time.sleep(0.05)
        tickets = 0
        try:
            tickets = c.execute(
                "DELETE FROM wechat_tickets WHERE expires_at < ?", (now - 3600,)).rowcount
        except sqlite3.OperationalError:
            pass
        usage = 0
        try:
            usage = c.execute(
                "DELETE FROM llm_usage WHERE day < date('now','+8 hours','-90 days')").rowcount
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass
    return {"ok": True, "sessions": sessions, "wechat_tickets": tickets, "llm_usage": usage}


class AdminUserReq(BaseModel):
    uid: str


@router.post("/api/admin/user/delete")
def admin_delete(req: AdminUserReq, request: Request):
    _admin(request)
    if not req.uid:
        raise HTTPException(400, "要 uid")
    return {"ok": True, "deleted": store.delete_user(req.uid)}


class RewrapReq(BaseModel):
    old_secret: str = ""


@router.post("/api/admin/rewrap")
def rewrap(req: RewrapReq, request: Request):
    _admin(request)
    old = (req.old_secret or "").encode()
    if not old:
        raise HTTPException(400, "请求体带 old_secret")
    n = 0
    with store._LOCK, store._conn() as c:
        rows = list(c.execute("SELECT user_id, kind, wrapped FROM user_secrets"))
    for r in rows:
        os.environ["_QIYAN_OLD"] = req.old_secret
        try:
            prev = os.environ.get("QIYAN_SECRET")
            os.environ["QIYAN_SECRET"] = req.old_secret
            plain = store.unwrap_secret(r["wrapped"])
            if prev is None:
                os.environ.pop("QIYAN_SECRET", None)
            else:
                os.environ["QIYAN_SECRET"] = prev
            new = store.wrap_secret(plain)
            with store._LOCK, store._conn() as c:
                c.execute("UPDATE user_secrets SET wrapped=?, key_id='v2' WHERE user_id=? AND kind=?",
                          (new, r["user_id"], r["kind"]))
            n += 1
        except Exception:
            if "QIYAN_SECRET" in os.environ and req.old_secret == os.environ.get("QIYAN_SECRET"):
                pass
    return {"ok": True, "rewrapped": n}
