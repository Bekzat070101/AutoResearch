# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 调用。未配置密钥时返回 None，调用方回退规则版。

环境变量（也可写在项目根目录 .env）：
  LLM_API_KEY   必填才会真正请求
  LLM_BASE_URL  默认 https://api.deepseek.com/v1
  LLM_MODEL     默认 deepseek-flash。DeepSeek 的 /models 只剩 deepseek-flash、deepseek-v4-pro；
                旧名 deepseek-chat 已列入停用，目前仍被路由到 deepseek-flash 的非思考模式。
                deepseek-flash 默认开思考（贵、慢），这里对 DeepSeek 默认关掉，见 _deepseek_extras()。
  LLM_REASONING_EFFORT  可选 low/high/max，只在支持的模型上填写
"""
from __future__ import annotations

import json
from typing import Any
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import budget
import envfile
import logs
from limits import ReadLimitError, fetch, stream

_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    envfile.load()  # 实际在 envfile 被 import 时已经读过；留着这个名字给老调用方


_load_dotenv()


def config() -> dict:
    key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    base = (os.environ.get("LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/")
    model = (os.environ.get("LLM_MODEL") or os.environ.get("OPENAI_MODEL") or "deepseek-flash").strip()
    return {"enabled": bool(key), "base_url": base, "model": model, "has_key": bool(key)}


def resolve_cfg(uid: str | None = None, override: dict | None = None) -> dict:
    """取出这一次该用的地址/模型/密钥。有 override 或用户自己的 key 时，不改 os.environ。
    own=True 表示这把 key 是一次性传进来的或这个人自己上传的：花的是他自己的钱，不扣团队预算（budget.py）。"""
    if override:
        key = (override.get("api_key") or override.get("key") or "").strip()
        base = (override.get("base_url") or "https://api.deepseek.com/v1").strip().rstrip("/")
        model = (override.get("model") or "deepseek-flash").strip()
        return {"enabled": bool(key), "base_url": base, "model": model, "has_key": bool(key), "key": key, "own": True}
    if uid:
        import store
        secret = store.llm_secret(uid)
        if secret and secret.get("key"):
            return {
                "enabled": True,
                "base_url": (secret.get("base_url") or "https://api.deepseek.com/v1").rstrip("/"),
                "model": (secret.get("model") or "deepseek-flash").strip(),
                "has_key": True,
                "key": secret["key"],
                "own": True,
            }
    cfg = config()
    key = (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or ""
    ).strip()
    return {**cfg, "key": key, "own": False}


def _consent_ok(uid: str | None, override: dict | None = None, cfg: dict | None = None) -> bool:
    if override or not uid:
        return True
    cfg = cfg if cfg is not None else resolve_cfg(uid, override)
    if not cfg.get("own"):
        return True
    try:
        import budget
        return budget.consented(uid)
    except Exception:
        return False


def enabled(uid: str | None = None) -> bool:
    """配了密钥（服务器的，或这个人自己上传的），并且——用服务器密钥时——这个请求的人今天还有额度（budget.py）。
    自带 key 也要先同意当前隐私说明（16-F08：同意检查不再只挂在扣费路径上）。各功能在这里拿到 False 就走规则版。"""
    cfg = resolve_cfg(uid)
    if not _consent_ok(uid, cfg=cfg):
        return False
    return cfg["enabled"] and (cfg["own"] or budget.allowed())


def ready(uid: str | None = None) -> bool:
    """有没有能用的模型。兼容测试里把 enabled 桩成零参 lambda。"""
    fn = enabled
    try:
        return fn(uid) if uid is not None else fn()
    except TypeError:
        return bool(fn())


def apply_config(base_url: str, api_key: str, model: str, *, persist: bool = True) -> dict:
    """运行时写入环境变量，并可选落盘到 .env（已在 gitignore）。"""
    base = (base_url or "https://api.deepseek.com/v1").strip().rstrip("/")
    key = (api_key or "").strip()
    name = (model or "deepseek-flash").strip()
    if not key:
        raise ValueError("API key is required")
    if not base.startswith("https://"):
        raise ValueError("base_url must start with https://")
    os.environ["LLM_BASE_URL"] = base
    os.environ["LLM_API_KEY"] = key
    os.environ["LLM_MODEL"] = name
    if persist:
        _save_dotenv(base, key, name)
    return config()


def _save_dotenv(base: str, key: str, model: str) -> None:
    """只改这三行，其余配置和注释原样保留（原来整份重写，会把别的变量冲掉）。"""
    path = _ROOT / ".env"
    want = {"LLM_BASE_URL": base, "LLM_API_KEY": key, "LLM_MODEL": model}
    old = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["# 本地密钥，勿提交。由页面「连接模型」写入。"]
    out = []
    for line in old:
        name = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and name in want:
            out.append(f"{name}={want.pop(name)}")
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in want.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def probe(*, uid: str | None = None, override: dict | None = None) -> dict:
    """发一次极短请求，确认密钥和地址可用。"""
    cfg = resolve_cfg(uid, override)
    if not cfg["enabled"]:
        return {"ok": False, "error": "未配置 API key"}
    with budget.unmetered():  # 管理员测连通、学生试自己的密钥：都不算在任何人头上
        text = chat("只回复 ok 两个字母。", "ping", temperature=0, timeout=20, uid=uid, override=override)
    if not text:
        return {"ok": False, "error": "请求失败，请检查地址、密钥和网络", "model": cfg["model"]}
    return {"ok": True, "model": cfg["model"], "base_url": cfg["base_url"], "sample": text[:40]}


MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 一次回复远小于这个；卡住或乱发的服务不能无限占着评阅线程
# DeepSeek 排队时会不断发空行保持连接：timeout 管「多久没有数据」（发空行就不算卡住），
# 另有总时限封顶，卡住或一直吊着的服务最多占一个线程这么久
LLM_TOTAL_SECONDS = 150


def _post(url: str, key: str, payload: dict, timeout: int, total: int | None = None) -> tuple[dict | None, str]:
    """timeout 是「最多这么久没有数据」；整个请求另有 total 或 LLM_TOTAL_SECONDS 总时限。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        raw = fetch(req, timeout=max(timeout, total or LLM_TOTAL_SECONDS), idle=timeout, max_bytes=MAX_RESPONSE_BYTES)
        return json.loads(raw.decode("utf-8")), ""
    except urllib.error.HTTPError as exc:
        return None, f"http {exc.code}"
    except ReadLimitError as exc:
        return None, f"limit {exc}"  # 超时或超大：不重试，重试只会再等一遍
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"network {type(exc).__name__}"
    except json.JSONDecodeError:
        return None, "bad json"
    except UnicodeDecodeError:
        return None, "bad utf-8"  # 回包不是合法 UTF-8：原来这个异常从这里漏出去，整轮 500（Codex 复现）


def _deepseek_extras(cfg: dict, effort: str) -> dict:
    """DeepSeek 的 deepseek-flash 默认开思考：没设推理强度就显式关掉，省钱也省时间。别的服务不加这个字段。"""
    if "api.deepseek.com" not in cfg["base_url"] or effort:
        return {}
    return {"thinking": {"type": "disabled"}}


def chat(system: str, user: str, *, temperature: float = 0.4, timeout: int = 25,
         json_mode: bool = False, tag: str = "chat", max_tokens: int = 2000,
         uid: str | None = None, override: dict | None = None,
         total: int | None = None, retry: bool = True) -> str | None:
    """返回助手文本；失败、未配置或没有额度返回 None。网络错误、429、5xx 重试一次（retry=False 则不重试）；total 是整次请求的总时限。
    用服务器密钥时每次请求上游都扣一笔：失败的那次上游也可能已经算了钱（比如回到一半断了），额度要和真实请求次数对得上。
    用户自己的 key 只当参数用，绝不写进 os.environ，也不扣团队预算。标题机翻在 budget.metered_as("*xlate") 里调，算在伪账号头上。"""
    cfg = resolve_cfg(uid, override)
    if not _consent_ok(uid, override, cfg) or not cfg["enabled"]:
        return None
    metered = not cfg["own"]
    if metered and not budget.charge():
        logs.emit({"llm": tag, "skipped": "budget"})
        return None
    key = cfg.get("key") or ""
    payload: dict = {
        "model": cfg["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,  # 封顶：出错的长输出不会无限计费
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip()
    if effort:
        payload["reasoning_effort"] = effort
    payload.update(_deepseek_extras(cfg, effort))
    url = cfg["base_url"] + "/chat/completions"
    t0 = time.time()
    data, err = None, ""
    for attempt in range(2 if retry else 1):
        if attempt and metered and not budget.charge():
            err += "; no budget to retry"
            break
        data, err = _post(url, key, payload, timeout, total) if total else _post(url, key, payload, timeout)
        can_retry = retry and (err.startswith("network") or err in ("http 429", "http 500", "http 502", "http 503", "http 504"))
        if data is not None or not can_retry or attempt == 1:
            break
        time.sleep(0.8)
    text = None
    if data is not None:
        # 回包形状逐层核对：原来顶层是数组、content 是列表这类回包在这里抛异常，漏出降级边界（Codex 复现）
        choices = data.get("choices") if isinstance(data, dict) else None
        message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(content, (str, type(None))):
            err = "bad payload"
        else:
            text = _clean_text((content or "").strip()) or None
    usage = data.get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}  # usage 是列表时原来读 token 数就抛异常（Codex 复现）
    logs.emit({
        "llm": tag, "model": cfg["model"], "ok": bool(text), "ms": int((time.time() - t0) * 1000),
        "tokens": usage.get("total_tokens"), "error": err or None,
        "own": bool(cfg.get("own")),
    })
    return text


def _clean_text(s: str) -> str:
    """去掉落单的代理字符（JSON 里的 \\ud800 这类）：它们写不进 SQLite，原来先存了事实、到存回复时才报错（Codex 复现）。
    先整段编码一次（C 速度），编不过才替换：逐字扫描 2 MB 要 85 毫秒（Codex 测过）。"""
    try:
        s.encode("utf-8")
        return s
    except UnicodeEncodeError:
        return s.encode("utf-8", "replace").decode("utf-8")


def _clean(obj: Any) -> Any:
    if isinstance(obj, str):
        return _clean_text(obj)
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {(_clean_text(k) if isinstance(k, str) else k): _clean(v) for k, v in obj.items()}
    return obj


def chat_json(system: str, user: str, *, timeout: int = 30, tag: str = "json",
              uid: str | None = None, override: dict | None = None,
              total: int | None = None, retry: bool = True) -> dict | None:
    text = chat(system + "\n只输出一个 JSON 对象，不要 Markdown 代码块。", user, temperature=0.2,
                timeout=timeout, json_mode=True, tag=tag, uid=uid, override=override,
                total=total, retry=retry)
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
        # 清理也可能在很深的嵌套上递归出错：和解析失败一样当这次调用失败，走回退（原来 600 层数组就 500，Codex 复现）
        return _clean(data) if isinstance(data, dict) else None
    except (json.JSONDecodeError, RecursionError):
        return None


def chat_stream(system: str, user: str, *, temperature: float = 0.6, timeout: int = 60,
                tag: str = "stream", max_tokens: int = 2000, status: dict | None = None,
                uid: str | None = None, override: dict | None = None):
    """逐块产出助手文本（OpenAI 兼容的 SSE）。

    只在调用方能接受纯文本时使用——结构化决策（TurnProposal）必须走 chat_json，
    因为它需要完整 JSON 才能校验。失败时产出零块，调用方要自己兜底。
    传了 status 时，流正常收尾（读到 [DONE] 或 finish_reason）会写 status["finished"] = True；没写就是中途断了。
    和 chat 一样的约束：DeepSeek 关思考、max_tokens 封顶、总时限 LLM_TOTAL_SECONDS、回复字节有上限——
    原来这里都没有：每个对话默认开着思考（贵、慢），一个一直吊着不结束的流能占住一个工作线程不放。
    """
    cfg = resolve_cfg(uid, override)
    if not _consent_ok(uid, override, cfg) or not cfg["enabled"]:
        return
    if not cfg["own"] and not budget.charge():  # 自己的 key 不扣团队预算
        logs.emit({"llm": tag, "skipped": "budget"})
        return
    key = cfg.get("key") or ""
    payload: dict = {
        "model": cfg["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip()
    if effort:
        payload["reasoning_effort"] = effort
    payload.update(_deepseek_extras(cfg, effort))
    req = urllib.request.Request(
        cfg["base_url"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    t0, pieces, err = time.time(), 0, ""
    try:
        for raw in stream(req, timeout=max(timeout, LLM_TOTAL_SECONDS), idle=timeout, max_bytes=MAX_RESPONSE_BYTES):
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                if status is not None and not status.get("cut"):
                    status["finished"] = True  # 原来断在半个 JSON / 半个汉字上，调用方也把半截当成功回复（Codex 复现）
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            # 块的形状逐层核对，不对就跳过：原来 choices 不是列表、delta 不是对象会抛异常，content 是列表会原样交出去（Codex 复现）
            choices = chunk.get("choices") if isinstance(chunk, dict) else None
            choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            delta = choice.get("delta")
            piece = delta.get("content") if isinstance(delta, dict) else None
            if piece and isinstance(piece, str):
                if status is not None and status.get("finished"):
                    status["finished"] = False  # 说了 stop 之后又来正文：以最后收到的为准，得再等一个 stop / [DONE]（Codex 复现）
                pieces += 1
                yield _clean_text(piece)
            reason = choice.get("finish_reason")  # 和最后一段正文同一块来的 stop 也算：先处理正文再看它
            if reason and status is not None:
                # 只有 stop 是说完了；length（到了字数上限）、content_filter 这些都是半截：原来一律当正常收尾，半截覆盖了完整回复（Codex 复现）
                status["finished"] = reason == "stop" and not status.get("cut")
                if reason != "stop":
                    status["cut"] = str(reason)[:40]
    except urllib.error.HTTPError as exc:
        err = f"http {exc.code}"
    except ReadLimitError as exc:
        err = f"limit {exc}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        err = f"network {type(exc).__name__}"
    if err and status is not None:
        status["finished"] = False  # 收到 stop 之后传输又出错：照样算没说完
    logs.emit({
        "llm": tag, "model": cfg["model"], "ok": pieces > 0,
        "ms": int((time.time() - t0) * 1000), "chunks": pieces, "error": err or None,
        "own": bool(cfg.get("own")),
    })
