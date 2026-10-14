"""Create or update Hangzhou FC 3.0 HTTP function. No secrets printed."""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

from alibabacloud_fc20230330.client import Client
from alibabacloud_fc20230330 import models as fc
from alibabacloud_tea_openapi.models import Config
from alibabacloud_tea_util.models import RuntimeOptions

from aliyun_rpc import load_env, rpc
from fc_safety import DEFAULT_NAME, is_function_missing, merge_env, refuse_unsafe
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


def llm_key() -> str:
    app_env = load_env(Path(__file__).resolve().parents[2] / ".env")
    root_env = load_env()
    return (app_env.get("LLM_API_KEY") or root_env.get("DEEPSEEK_API_KEY")
            or root_env.get("LLM_API_KEY") or "")


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


def _local_defaults(name: str) -> dict[str, str]:
    env_vars = {
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
        "LLM_MODEL": "deepseek-flash",
        "QIYAN_ENV": "test" if name != DEFAULT_NAME else "prod",
    }
    key = llm_key()
    if key:
        env_vars["LLM_API_KEY"] = key
        env_vars["DEEPSEEK_API_KEY"] = key
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
    refuse_unsafe(env_vars, name, args.allow_ephemeral)
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
        instance_concurrency=32,
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
            instance_concurrency=32,
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
    if url:
        if name == DEFAULT_NAME:
            write_state({"REGION": REGION, "FC_FUNCTION": name, "PUBLIC_URL": url, "ACCOUNT_TAIL": aid[-4:]})
        else:
            write_state({"FC_TEST_FUNCTION": name, "TEST_URL": url})
        warm_after_deploy(url, env_vars)


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
