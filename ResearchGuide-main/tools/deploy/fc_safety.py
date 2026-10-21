"""deploy_fc 的纯函数：读现有环境变量失败不能当空字典；生产缺盘拒绝发布。"""
from __future__ import annotations

DEFAULT_NAME = "qiyan"
DEFAULT_PACKAGE_DB = "server/data/demo.db"
REQUIRED_PROD = ("QIYAN_SECRET", "WECHAT_APP_ID")


def is_function_missing(exc: BaseException) -> bool:
    msg = str(exc)
    name = type(exc).__name__
    return "FunctionNotFound" in name or "FunctionNotFound" in msg


def merge_env(remote: dict[str, str] | None, local: dict[str, str], *,
              db_flag: str | None, name: str) -> dict[str, str]:
    out = dict(local)
    if remote:
        out.update(remote)
        if "QIYAN_DB" in remote and not db_flag:
            out["QIYAN_DB"] = remote["QIYAN_DB"]
    if db_flag:
        out["QIYAN_DB"] = db_flag
    if "QIYAN_DB" not in out:
        out["QIYAN_DB"] = "/tmp/qiyan-test.db" if name != DEFAULT_NAME else "/tmp/qiyan.db"
    return out


def db_path_ephemeral(db: str) -> bool:
    path = (db or "").replace("\\", "/")
    if not path:
        return True
    return path.startswith("/tmp/") or path.startswith("/private/tmp/") or path.startswith("/var/tmp/")


def db_path_mounted_writable(db: str) -> bool:
    """生产库必须在已挂载且可写的卷上。/data 是函数计算本地盘，未挂载就拒绝。"""
    import os
    from pathlib import Path
    path = (db or "").replace("\\", "/")
    if db_path_ephemeral(path):
        return False
    if path == "/data" or path.startswith("/data/"):
        return os.path.ismount("/data") and os.access("/data", os.W_OK)
    parent = Path(db).parent
    if parent.exists() and not os.access(parent, os.W_OK):
        return False
    return True


def refuse_unsafe(env_vars: dict[str, str], name: str, allow_ephemeral: bool) -> None:
    if name != DEFAULT_NAME:
        return
    db = env_vars.get("QIYAN_DB") or ""
    missing = [k for k in REQUIRED_PROD if not env_vars.get(k)]
    ephemeral = db_path_ephemeral(db)
    if (missing or ephemeral) and not allow_ephemeral:
        why = []
        if missing:
            why.append("缺 " + ",".join(missing))
        if ephemeral:
            why.append(f"QIYAN_DB={db or '(空)'} 在临时目录")
        raise SystemExit("拒绝发布生产：" + "；".join(why) + "。没有持久盘不准发版。要试运行加 --allow-ephemeral。")


def apply_ephemeral_flag(env_vars: dict[str, str], name: str, allow_ephemeral: bool) -> None:
    """服务端守门（store.db_blocked）的放行开关：测试函数和 --allow-ephemeral 带 QIYAN_ALLOW_EPHEMERAL=1；
    正常发生产时去掉，函数上残留的也一并清掉。"""
    if name != DEFAULT_NAME or allow_ephemeral:
        env_vars["QIYAN_ALLOW_EPHEMERAL"] = "1"
    else:
        env_vars.pop("QIYAN_ALLOW_EPHEMERAL", None)


def assert_health_persistent(health: dict) -> None:
    db = health.get("db") if isinstance(health, dict) else None
    if not isinstance(db, dict) or db.get("ephemeral") is not False:
        raise SystemExit("发版后 health.db.ephemeral 不是 false，不准当作生产完成。")


def _get(obj, *keys):
    for k in keys:
        if isinstance(obj, dict):
            if k in obj:
                return obj[k]
        elif hasattr(obj, k):
            return getattr(obj, k)
    return None


def nas_mount_dirs(cfg) -> list[str]:
    """函数配置里 nas_config.mount_points[].mount_dir；没有就是 []。"""
    nas = _get(cfg, "nas_config", "nasConfig") if cfg is not None else None
    points = _get(nas, "mount_points", "mountPoints") if nas is not None else None
    out = []
    for mp in points or []:
        d = _get(mp, "mount_dir", "mountDir")
        if d:
            out.append(str(d))
    return out


def assert_db_under_nas(db_path: str, mounts: list[str]) -> str:
    import posixpath
    raw = db_path or ""
    if "\\" in raw or not raw.startswith("/"):
        raise SystemExit("拒绝发布生产：QIYAN_DB 不在已配置 NAS 挂载目录")
    db = posixpath.normpath(raw)
    for m in mounts or []:
        root = str(m)
        if "\\" in root or not root.startswith("/"):
            continue
        root = posixpath.normpath(root)
        if root != "/" and db.startswith(root.rstrip("/") + "/"):
            return db
    raise SystemExit("拒绝发布生产：QIYAN_DB 不在已配置 NAS 挂载目录")


def production_release_checks(env_vars: dict[str, str], name: str, allow_ephemeral: bool, cfg,
                              *, deploy, check_health, record):
    """发布门顺序：先定守门开关，生产再核函数 NAS 挂载，再 refuse_unsafe，发完必须有 URL 且 health 持久可写才 record。"""
    apply_ephemeral_flag(env_vars, name, allow_ephemeral)
    if name == DEFAULT_NAME and not allow_ephemeral:
        env_vars["QIYAN_DB"] = assert_db_under_nas(env_vars.get("QIYAN_DB") or "", nas_mount_dirs(cfg))
    refuse_unsafe(env_vars, name, allow_ephemeral)
    url = deploy()
    if name != DEFAULT_NAME:
        if url:
            record(url)
        return url
    if not url:
        raise SystemExit("发布未完成：未取得探活地址")
    health = check_health(url)
    if not allow_ephemeral:
        db = health.get("db") if isinstance(health, dict) else None
        if not isinstance(db, dict) or db.get("ephemeral") is not False or db.get("writable") is not True:
            raise SystemExit("发布未完成：health.db 不是 ephemeral=false 且 writable=true")
    record(url)
    return url


def require_migration(remote_db, new_db, remote_users, migrate_from) -> None:
    """远端已有用户且要换库路径时，必须带 --migrate-from-backup。"""
    if not new_db or not remote_db or remote_db == new_db:
        return
    if (remote_users or 0) > 0 and not migrate_from:
        raise SystemExit("换库必须先备份再恢复：加 --migrate-from-backup")
