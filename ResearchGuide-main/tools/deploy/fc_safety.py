"""deploy_fc 的纯函数：读现有环境变量失败不能当空字典；生产缺盘拒绝发布。"""
from __future__ import annotations

DEFAULT_NAME = "qiyan"
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
    unmounted = not db_path_mounted_writable(db)
    if (missing or ephemeral or unmounted) and not allow_ephemeral:
        why = []
        if missing:
            why.append("缺 " + ",".join(missing))
        if ephemeral:
            why.append(f"QIYAN_DB={db or '(空)'} 在临时目录")
        elif unmounted:
            why.append(f"QIYAN_DB={db} 不在已挂载可写卷")
        raise SystemExit("拒绝发布生产：" + "；".join(why) + "。没有持久盘不准发版。要试运行加 --allow-ephemeral。")


def assert_health_persistent(health: dict) -> None:
    db = health.get("db") if isinstance(health, dict) else None
    if not isinstance(db, dict) or db.get("ephemeral") is not False:
        raise SystemExit("发版后 health.db.ephemeral 不是 false，不准当作生产完成。")
