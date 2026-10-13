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


def refuse_unsafe(env_vars: dict[str, str], name: str, allow_ephemeral: bool) -> None:
    if name != DEFAULT_NAME:
        return
    db = env_vars.get("QIYAN_DB") or ""
    missing = [k for k in REQUIRED_PROD if not env_vars.get(k)]
    ephemeral = db.startswith("/tmp/") or not db
    if (missing or ephemeral) and not allow_ephemeral:
        why = []
        if missing:
            why.append("缺 " + ",".join(missing))
        if ephemeral:
            why.append(f"QIYAN_DB={db or '(空)'} 在 /tmp")
        raise SystemExit("拒绝发布生产：" + "；".join(why) + "。没有持久盘不准发版。要试运行加 --allow-ephemeral。")
