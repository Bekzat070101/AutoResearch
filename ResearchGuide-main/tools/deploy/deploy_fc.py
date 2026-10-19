"""Create or update Hangzhou FC 3.0 HTTP function. No secrets printed."""
from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
from pathlib import Path

from alibabacloud_fc20230330.client import Client
from alibabacloud_fc20230330 import models as fc
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

from aliyun_rpc import load_env, rpc
from fc_safety import DEFAULT_NAME, assert_health_persistent, is_function_missing, merge_env, production_release_checks, require_migration
from pack_fc import OUT, main as pack

REGION = "cn-hangzhou"
STATE = Path(__file__).resolve().parent / ".deploy.env"


def account_id(ak: str, sk: str) -> str:
    r = rpc("GetCallerIdentity", {}, version="2015-04-01",
            endpoint="https://sts.aliyuncs.com", ak=ak, sk=sk)
    if r.get("AccountId"):
        return str(r["AccountId"])
    raise SystemExit(f"STS {r.get('Code')} {str(r.get('Message', ''))[:200]}")


def write_state(rows: dict[str, str]) -> None:
    old = {}
    if STATE.exists():
        for line in STATE.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                old[k] = v
    old.update(rows)
    STATE.write_text("".join(f"{k}={v}\n" for k, v in old.items()), encoding="utf-8")


def llm_settings() -> dict[str, str] | None:
    """app → root 取第一个有 key 的源，整组取值；两处都没 key 返回 None。"""
    app_env = load_env(Path(__file__).resolve().parents[2] / ".env")
    root_env = load_env()
    for src in (app_env, root_env):
        key = src.get("LLM_API_KEY") or src.get("DEEPSEEK_API_KEY")
        if key:
            return {"key": key, "base_url": src.get("LLM_BASE_URL") or "https://api.deepseek.com/v1",
                    "model": src.get("LLM_MODEL") or "deepseek-flash",
                    "effort": src.get("LLM_REASONING_EFFORT") or ""}
    return None


def llm_key() -> str:
    settings = llm_settings()
    return settings["key"] if settings else ""


def client(ak: str, sk: str, aid: str) -> Client:
    return Client(Config(
        access_key_id=ak,
        access_key_secret=sk,
        endpoint=f"{aid}.{REGION}.fc.aliyuncs.com",
        region_id=REGION,
    ))


def existing_env(cli: Client, name: str) -> dict[str, str] | None:
    """读函数现有环境变量。只有 FunctionNotFound 当「没有」；其它异常原样抛出，禁止当空字典吞掉。"""
    try:
        got = cli.get_function(name, fc.GetFunctionRequest())
    except Exception as exc:
        if is_function_missing(exc):
            return None
        raise
    body = getattr(got, "body", None)
    env = getattr(body, "environment_variables", None) if body else None
    return dict(env or {})


def existing_function(cli: Client, name: str):
    """读函数现有配置（含 nas_config）。只有 FunctionNotFound 当「没有」。"""
    try:
        got = cli.get_function(name, fc.GetFunctionRequest())
    except Exception as exc:
        if is_function_missing(exc):
            return None
        raise
    return getattr(got, "body", None)


def _local_defaults(name: str) -> dict[str, str]:
    settings = llm_settings()
    env_vars = {"QIYAN_ENV": "test" if name != DEFAULT_NAME else "prod"}
    if settings is None:
        return env_vars
    env_vars["LLM_BASE_URL"] = settings["base_url"]
    env_vars["LLM_MODEL"] = settings["model"]
    key = settings["key"]
    if key:
        env_vars["LLM_API_KEY"] = key
        if "api.deepseek.com" in settings["base_url"]:  # 别家地址别配 DEEPSEEK_API_KEY：发版后模型会认证失败（Codex 复现）
            env_vars["DEEPSEEK_API_KEY"] = key
    if settings.get("effort"):  # 推理强度也是这一组的：本机写了才发，免得留着函数上原来的 max
        env_vars["LLM_REASONING_EFFORT"] = settings["effort"]
    return env_vars


def put_single_instance(cli: Client, name: str) -> None:
    cli.put_concurrency_config(name, fc.PutConcurrencyConfigRequest(
        body=fc.PutConcurrencyInput(reserved_concurrency=1)))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("name", nargs="?", default=DEFAULT_NAME)
    p.add_argument("--db", default=None, help="显式改 QIYAN_DB；否则沿用函数上已有的")
    p.add_argument("--allow-ephemeral", action="store_true",
                   help="允许生产库仍在 /tmp 或缺少 QIYAN_SECRET/WECHAT_APP_ID。没有持久盘时不要用来发生产。")
    p.add_argument("--migrate-from-backup", default=None, metavar="PATH",
                   help="换库时：先远端备份、校验本地备份文件，发版后恢复并核对用户数。")
    args = p.parse_args(argv)
    name = args.name
    env = load_env()
    ak, sk = env["AccessKey_ID"], env["AccessKey_Secret"]
    aid = account_id(ak, sk)
    print("ACCOUNT", aid[-4:])
    print("FUNCTION", name)
    pack()
    zip_bytes = OUT.read_bytes()
    print("ZIP_MB", round(len(zip_bytes) / 1024 / 1024, 2))
    cli = client(ak, sk, aid)
    runtime = RuntimeOptions(read_timeout=120000, connect_timeout=30000)
    code = fc.InputCodeLocation(zip_file=base64.b64encode(zip_bytes).decode())
    remote = existing_env(cli, name)
    env_vars = merge_env(remote, _local_defaults(name), db_flag=args.db, name=name)
    cfg = existing_function(cli, name) if name == DEFAULT_NAME else None
    remote_db = (remote or {}).get("QIYAN_DB")
    check_health = check_deployed_health
    if args.db and remote_db and remote_db != args.db:
        trig = cli.get_trigger(name, "http")
        http = getattr(trig.body, "http_trigger", None) if trig.body else None
        old_url = (getattr(http, "url_internet", None) or getattr(http, "url_intranet", None)) if http else None
        if not old_url:
            raise SystemExit("换库前取不到远端地址，无法核对用户数")
        before = remote_user_count(old_url)
        print("USERS_BEFORE", before)
        require_migration(remote_db, args.db, before, args.migrate_from_backup)
        if before > 0:
            token = env_vars.get("ADMIN_TOKEN") or ""
            if not token:
                raise SystemExit("换库迁移需要函数上的 ADMIN_TOKEN")
            obj = backup_remote(old_url, token)
            con = sqlite3.connect(args.migrate_from_backup)
            try:
                ok = con.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                con.close()
            if ok != "ok":
                raise SystemExit(f"备份文件 integrity_check={ok}，不准换库")
            print("BACKUP_INTEGRITY ok")

            def check_health(u: str) -> dict:
                health = check_deployed_health(u)
                restore_remote(u, token, obj)
                after = remote_user_count(u)
                print("USERS_AFTER", after)
                if after < before:
                    raise SystemExit(f"恢复后用户数 {after} 少于发版前 {before}，换库未完成")
                return health
    url = production_release_checks(
        env_vars, name, args.allow_ephemeral, cfg,
        deploy=lambda: _deploy(cli, name, remote, env_vars, code),
        check_health=check_health,
        record=lambda u: write_state(
            {"REGION": REGION, "FC_FUNCTION": name, "PUBLIC_URL": u, "ACCOUNT_TAIL": aid[-4:]}
            if name == DEFAULT_NAME else {"FC_TEST_FUNCTION": name, "TEST_URL": u}),
    )
    if url:
        warm_after_deploy(url, env_vars)


def _deploy(cli: Client, name: str, remote, env_vars: dict[str, str], code) -> str | None:
    print("QIYAN_DB", env_vars.get("QIYAN_DB"))
    print("KEY", "set" if env_vars.get("LLM_API_KEY") else "missing")
    body = fc.CreateFunctionInput(
        function_name=name,
        runtime="custom.debian12",
        handler="index.handler",
        timeout=300,
        memory_size=2048,
        cpu=1,
        disk_size=512,
        instance_concurrency=200,  # 单实例最多 200：多开实例时各有一份库，登录在实例之间来回失效（docs/DEPLOY.md）
        internet_access=True,
        environment_variables=env_vars,
        code=code,
        description="qiyan research mentor" if name == DEFAULT_NAME else "qiyan test (not the public homepage)",
    )
    if remote is None:
        created = cli.create_function(fc.CreateFunctionRequest(body=body))
        print("CREATED", created.body.function_name if created.body else "ok")
    else:
        upd = fc.UpdateFunctionInput(
            runtime="custom.debian12",
            handler="index.handler",
            timeout=300,
            memory_size=2048,
            cpu=1,
            disk_size=512,
            instance_concurrency=200,  # 单实例最多 200：多开实例时各有一份库，登录在实例之间来回失效（docs/DEPLOY.md）
            internet_access=True,
            environment_variables=env_vars,
            code=code,
        )
        cli.update_function(name, fc.UpdateFunctionRequest(body=upd))
        print("UPDATED")
    trig_cfg = json.dumps({
        "authType": "anonymous",
        "methods": ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"],
        "disableURLInternet": False,
    })
    try:
        cli.create_trigger(name, fc.CreateTriggerRequest(body=fc.CreateTriggerInput(
            trigger_name="http",
            trigger_type="http",
            trigger_config=trig_cfg,
        )))
        print("TRIGGER created")
    except Exception as e:
        print("TRIGGER", type(e).__name__, str(e)[:240])
    url = None
    try:
        trig = cli.get_trigger(name, "http")
        http = getattr(trig.body, "http_trigger", None) if trig.body else None
        if http:
            url = getattr(http, "url_internet", None) or getattr(http, "url_intranet", None)
            print("TRIGGER_URL", url)
    except Exception as e:
        print("GET_TRIG", str(e)[:200])
    try:
        put_single_instance(cli, name)
        print("CONCURRENCY reserved=1")
    except Exception as e:
        raise SystemExit(f"单实例设置失败，未发布完成：{type(e).__name__} {str(e)[:240]}")
    return url


def check_deployed_health(url: str) -> dict:
    """生产发完必须看到 health.db.ephemeral=false。本函数只读探活，不发版。"""
    import urllib.request
    req = urllib.request.Request(f"{str(url).rstrip('/')}/api/health")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode() or "{}")
    assert_health_persistent(body)
    print("HEALTH persistent")
    return body


def remote_user_count(url: str) -> int:
    """读 health 的 qiyan_users，没有就读 db.account_store.registered。"""
    import urllib.request
    req = urllib.request.Request(f"{str(url).rstrip('/')}/api/health")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode() or "{}")
    n = body.get("qiyan_users") if isinstance(body, dict) else None
    if n is None:
        db = body.get("db") if isinstance(body, dict) else None
        store = db.get("account_store") if isinstance(db, dict) else None
        n = store.get("registered") if isinstance(store, dict) else None
    if n is None:
        raise SystemExit("health 里没有用户数，无法核对")
    return int(n)


def backup_remote(url: str, token: str) -> str:
    """POST /api/admin/backup，返回备份对象名。"""
    import urllib.request
    req = urllib.request.Request(f"{str(url).rstrip('/')}/api/admin/backup", method="POST",
                                 headers={"X-Admin-Token": token})
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode() or "{}")
    if not body.get("ok") or not body.get("object"):
        raise SystemExit("远端备份失败，不准换库")
    print("BACKUP", body["object"])
    return str(body["object"])


def restore_remote(url: str, token: str, obj: str) -> dict:
    """POST /api/admin/restore，把备份灌回新库。"""
    import urllib.request
    req = urllib.request.Request(f"{str(url).rstrip('/')}/api/admin/restore", method="POST",
                                 data=json.dumps({"object": obj}).encode(),
                                 headers={"X-Admin-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = json.loads(resp.read().decode() or "{}")
    if not body.get("ok"):
        raise SystemExit("恢复失败，换库未完成")
    print("RESTORED", obj)
    return body


def warm_after_deploy(url: str, env_vars: dict | None, post=None) -> dict:
    """发版后预热工具包正文。失败只打印，不阻断发版。"""
    token = (env_vars or {}).get("ADMIN_TOKEN") or ""
    if not token:
        print("请手动预热")
        return {"skipped": True}
    import urllib.request
    remaining = 1
    for _ in range(5):
        try:
            if post:
                body = post(url, token)
            else:
                req = urllib.request.Request(
                    f"{str(url).rstrip('/')}/api/admin/papers/warm?budget=240",
                    method="POST",
                    headers={"X-Admin-Token": token},
                )
                with urllib.request.urlopen(req, timeout=300) as resp:
                    body = json.loads(resp.read().decode() or "{}")
            print("WARM", body)
            remaining = int((body or {}).get("remaining", 1))
            if remaining == 0:
                break
        except Exception as e:
            print("WARM_FAIL", type(e).__name__, str(e)[:200])
            break
    return {"skipped": False, "remaining": remaining}


if __name__ == "__main__":
    main()
