# -*- coding: utf-8 -*-
"""管理接口：自己核 X-Admin-Token，列在 auth.PUBLIC。"""
from __future__ import annotations

import gzip
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth
import store
from schemas import now_iso

router = APIRouter()
SNAP = Path(os.environ.get("QIYAN_SNAP") or "/tmp/qiyan-snap.db")


def _admin(request: Request) -> None:
    token = os.environ.get("ADMIN_TOKEN") or ""
    if len(token) >= 32 and hmac.compare_digest(request.headers.get("X-Admin-Token", ""), token):
        return
    # 第二通道：登录着的管理员（注册时用 ADMIN_TOKEN 当邀请码的账号）凭自己的会话就能看，
    # 页面不用存管理员口令。放 503 之前：万一环境变量被误删，已入库的管理员账号还能用。
    bearer = auth._bearer(request)
    if bearer:
        uid, _ = store.session_lookup(bearer)
        if uid and store.get_user(uid).get("is_admin"):
            return
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


def _oss_signed(method: str, bucket: str, object_key: str, blob: bytes | None = None) -> "urllib.request.Request":
    import base64
    import hashlib
    import urllib.request
    from email.utils import formatdate
    key_id = (os.environ.get("BACKUP_OSS_ACCESS_KEY_ID") or os.environ.get("OSS_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("BACKUP_OSS_ACCESS_KEY_SECRET") or os.environ.get("OSS_ACCESS_KEY_SECRET") or "").strip()
    if not key_id or not secret:
        raise RuntimeError("OSS AccessKey 未配置")
    endpoint = (os.environ.get("BACKUP_OSS_ENDPOINT") or "https://oss-cn-hangzhou.aliyuncs.com").rstrip("/")
    date = formatdate(usegmt=True)
    content_type = "application/octet-stream"
    resource = f"/{bucket}/daily/{object_key}"
    to_sign = f"{method}\n\n{content_type}\n{date}\n{resource}"
    sig = base64.b64encode(hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha1).digest()).decode()
    headers = {
        "Content-Type": content_type,
        "Date": date,
        "Authorization": f"OSS {key_id}:{sig}",
    }
    url = f"{endpoint}{resource}"
    return urllib.request.Request(url, data=blob, method=method, headers=headers)


def _put_oss(name: str, blob: bytes) -> str:
    """有桶名才上传；配了桶而上传失败不能报成功。"""
    bucket = os.environ.get("BACKUP_OSS_BUCKET") or ""
    if not bucket:
        return "local"
    try:
        import urllib.request
        urllib.request.urlopen(_oss_signed("PUT", bucket, name, blob), timeout=30)
        return "ok"
    except HTTPException:
        raise
    except Exception as exc:
        print(json.dumps({"event": "backup", "ok": False, "error": type(exc).__name__}, ensure_ascii=False))
        raise HTTPException(502, f"OSS 上传失败：{type(exc).__name__}") from exc


def _get_oss(name: str) -> bytes | None:
    bucket = os.environ.get("BACKUP_OSS_BUCKET") or ""
    if not bucket:
        return None
    import urllib.request
    with urllib.request.urlopen(_oss_signed("GET", bucket, name), timeout=30) as resp:
        return resp.read()


@router.post("/api/admin/backup")
def backup(request: Request):
    _admin(request)
    try:
        return make_backup()
    except HTTPException as exc:
        if exc.status_code != 502:
            raise
        return JSONResponse(status_code=502, content={"ok": False, "detail": exc.detail})


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
        try:
            blob = _get_oss(name)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f"OSS 下载失败：{type(exc).__name__}") from exc
        if not blob:
            raise HTTPException(404, "本地没有这份备份；OSS 也没有")
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(blob)
    raw = gzip.decompress(_aes(src.read_bytes(), key, False))
    try:
        pending = install_restored_db(raw)
    except OSError as exc:
        raise HTTPException(500, f"换库失败：{exc}") from exc
    if pending:
        return {"ok": True, "object": name, "cleanup_pending": pending,
                "warning": "恢复已换入；WAL/SHM 清理待重试"}
    return {"ok": True, "object": name}


_BUSY = "数据库仍在使用，请稍后重试恢复"


def install_restored_db(raw: bytes) -> list[str]:
    """恢复包写到目标同目录唯一 stage，校验后在恢复闸内 os.replace。失败保留原库；返回清理失败的 sidecar。"""
    target = Path(store.DB_PATH)
    stage = target.with_name(f"{target.name}.incoming-{uuid4().hex}")
    try:
        _write_restore_bytes(stage, raw)
        _check_integrity_and_close(stage)
        try:
            deadline = time.monotonic() + 5
            if not store._LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
                raise HTTPException(503, _BUSY)
            try:
                with store.restore_gate(timeout=max(0, deadline - time.monotonic())):
                    with store._conn() as c:
                        left_ms = max(0, int(1000 * (deadline - time.monotonic())))
                        c.execute(f"PRAGMA busy_timeout={left_ms}")
                        busy = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                    if busy:
                        raise HTTPException(503, _BUSY)
                    os.replace(stage, target)
                    return _remove_sidecars_collect_errors(target)
            finally:
                store._LOCK.release()
        except store.RestoreBusy as exc:
            raise HTTPException(503, _BUSY) from exc
    finally:
        stage.unlink(missing_ok=True)


def _check_integrity_and_close(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise HTTPException(500, "恢复包 integrity_check 失败")
    finally:
        conn.close()


def _remove_sidecars_collect_errors(target: Path) -> list[str]:
    pending: list[str] = []
    for suffix in ("-wal", "-shm"):
        try:
            Path(str(target) + suffix).unlink(missing_ok=True)
        except OSError:
            pending.append(suffix)
    return pending


def _write_restore_bytes(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)


@router.post("/api/admin/papers/collect")
def papers_collect(request: Request, node: str = "", quota: int = 20, fulltext: int = 8,
                   max_seconds: int = 240, source: str = ""):
    _admin(request)
    if quota < 1 or quota > 50:
        raise HTTPException(400, "quota 须在 1–50")
    if fulltext < 0 or fulltext > quota:
        raise HTTPException(400, "fulltext 须在 0–quota")
    if source not in ("", "arxiv", "osf", "biorxiv", "medrxiv"):
        raise HTTPException(400, "source 只认 arxiv / osf / biorxiv / medrxiv")
    import paper_collect
    if not store.try_collect_lock(max_seconds):
        raise HTTPException(409, "采集正在跑")
    try:
        return paper_collect.collect(node=node, quota=quota, fulltext=fulltext, xlate=True,
                                     max_seconds=max_seconds, source=source)
    except paper_collect.CollectError as exc:
        raise HTTPException(409, str(exc)) from exc
    finally:
        store.release_collect_lock()


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
            reading.warm_fetch(aid, hint=hint or None)
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


# ---------- 用户反馈（团队看全部） ----------

@router.get("/api/admin/feedback")
def admin_feedback_list(request: Request, status: str = "", limit: int = 200):
    """全部用户的反馈，新→旧，带提交人昵称。?status=open|reviewed|done 可过滤。"""
    _admin(request)
    if status and status not in ("open", "reviewed", "done"):
        raise HTTPException(400, "status 只能是 open / reviewed / done，或不传")
    return {"items": store.list_all_feedback(status, limit)}


class FeedbackStatusReq(BaseModel):
    id: str
    status: str


@router.post("/api/admin/feedback/status")
def admin_feedback_status(req: FeedbackStatusReq, request: Request):
    """处理进度：open（已收到）→ reviewed（已读）→ done（已处理）。"""
    _admin(request)
    if req.status not in ("open", "reviewed", "done"):
        raise HTTPException(400, "status 只能是 open / reviewed / done")
    if not store.set_feedback_status(req.id, req.status):
        raise HTTPException(404, "没有这条反馈")
    return {"ok": True, "id": req.id, "status": req.status}


class RewrapReq(BaseModel):
    old_secret: str = ""


@router.post("/api/admin/rewrap")
def rewrap(req: RewrapReq, request: Request):
    _admin(request)
    old = req.old_secret or ""
    if not old:
        raise HTTPException(400, "请求体带 old_secret")
    env_before = os.environ.get("QIYAN_SECRET")
    n = 0
    failed: list[str] = []
    changed: list[str] = []
    with store._LOCK, store._conn() as c:
        rows = list(c.execute("SELECT user_id, kind, wrapped FROM user_secrets"))
    for r in rows:
        try:
            plain = store.unwrap_secret(r["wrapped"], secret=old)
            new = store.wrap_secret(plain)
            with store._LOCK, store._conn() as c:
                hit = c.execute(
                    "UPDATE user_secrets SET wrapped=?, key_id='v2' WHERE user_id=? AND kind=? AND wrapped=?",
                    (new, r["user_id"], r["kind"], r["wrapped"]),
                ).rowcount
        except Exception:
            failed.append(r["user_id"])
            continue
        if hit:
            n += 1
        else:
            changed.append(r["user_id"])
    if os.environ.get("QIYAN_SECRET") != env_before:
        if env_before is None:
            os.environ.pop("QIYAN_SECRET", None)
        else:
            os.environ["QIYAN_SECRET"] = env_before
    if changed:
        raise HTTPException(409, {"ok": False, "code": "secret_changed", "changed": changed,
                                  "rewrapped": n, "failed": failed})
    if failed:
        raise HTTPException(400, {"ok": False, "rewrapped": n, "failed": failed})
    return {"ok": True, "rewrapped": n}
