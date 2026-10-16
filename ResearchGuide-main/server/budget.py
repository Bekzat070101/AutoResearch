# -*- coding: utf-8 -*-
"""模型调用的预算：每调一次记一笔，按人、按全站每天有上限；超了就当模型没开，功能回退到规则版。

模型钱是团队的（DeepSeek 密钥在服务器上）。原来任何人开个访客号就能无限次触发模型调用：
六十次交任务、几次对话就是七十次真实调用，没人同意过隐私说明也照样调。现在：
- 谁在调：auth.guard 在每个请求开头 bind(uid)；公开接口 bind 成匿名，匿名一律不许调。
- 上限（每天，北京时间）：全站 LLM_DAILY_TOTAL、绑了微信或注册了用户名的人 LLM_DAILY_USER、访客 LLM_DAILY_GUEST，
  另外全体访客加起来 LLM_DAILY_GUESTS：访客号谁都能开（同一个出口地址一小时几百个，见 auth.PER_IP_HOUR），
  刷一堆访客号最多花掉这一份，花不到绑了微信的人头上。
- 没同意当前版本隐私说明的人不调：对话和成绩单会发给模型服务，这一条必须先同意。
- 不在 HTTP 请求里（脚本、测试直接调函数）不计不限：上下文变量是 None。
llm.enabled() 先问这里有没有额度（各功能本来就会在模型不可用时回退规则），llm.chat 每次真正请求上游前扣一笔（重试也扣）。
上传了自己 DeepSeek 密钥的人（store.user_secrets）用自己的钱，这里不管：llm.resolve_cfg 给的 own=True 就不问也不扣。
"""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

import store

ANON = ""  # 公开接口：不许调模型
_WHO: contextvars.ContextVar[str | None] = contextvars.ContextVar("llm_user", default=None)
CN = timezone(timedelta(hours=8))


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def total_cap() -> int:
    return _int("LLM_DAILY_TOTAL", 2000)


def user_cap() -> int:
    return _int("LLM_DAILY_USER", 150)


def guest_cap() -> int:
    return _int("LLM_DAILY_GUEST", 15)


def consented(uid: str | None) -> bool:
    """是否接受了当前隐私说明。自带 key 也要先过这一关，不从扣费路径里顺带检查。"""
    if not uid:
        return True
    import auth as _auth
    u = store.get_user(uid) or {}
    return (u.get("consent_version") or "") == _auth.PRIVACY_VERSION


def guests_cap() -> int:
    return _int("LLM_DAILY_GUESTS", 500)


GUESTS = "*guest"  # llm_usage 里全体访客合计的那一行
LLM_DAILY_XLATE = int(os.environ.get("LLM_DAILY_XLATE") or 300)  # 标题机翻（*xlate 伪账号）每天上限，计入全站


def bind(uid: str | None) -> None:
    """这个请求是谁的。None 只用于请求之外；请求里没有登录就传 ANON。"""
    _WHO.set(uid if uid is not None else ANON)


@contextmanager
def unmetered() -> Iterator[None]:
    """管理员测连通这类不该算在任何人头上的调用。"""
    token = _WHO.set(None)
    try:
        yield
    finally:
        _WHO.reset(token)


def _today() -> str:
    return store.llm_day()  # 北京时间的今天；store.llm_usage_today/bump_llm_usage 用同一口径


def _caps(uid: str) -> dict[str, int]:
    """这一笔要过的每道上限：这个人、全站，访客再加全体访客。没同意隐私说明、账号不存在，这个人的上限是 0。
    `*` 开头的是全站伪账号（标题机翻 *xlate）：只有自己的上限和全站上限，不查用户表。"""
    if uid.startswith("*"):
        return {uid: (LLM_DAILY_XLATE if uid == "*xlate" else 0), "*": total_cap()}
    import auth  # auth 也 import 本模块，放到函数里避免循环
    u = store.get_user(uid)
    if not u or u.get("consent_version") != auth.PRIVACY_VERSION:
        return {uid: 0, "*": total_cap()}
    if u.get("wechat_openid") or u.get("username"):  # 绑了微信、或注册了用户名密码：都是能找回的账号，不按访客算
        return {uid: user_cap(), "*": total_cap()}
    return {uid: guest_cap(), "*": total_cap(), GUESTS: guests_cap()}


def cap_for(uid: str) -> int:
    """这个人自己今天最多调几次。"""
    return _caps(uid)[uid]


def left(uid: str) -> int:
    day = _today()
    return max(0, min(cap - store.llm_calls(day, k) for k, cap in _caps(uid).items()))


def allowed(who: str | None = None) -> bool:
    """只查不扣：这个请求现在还能不能调模型。"""
    who = who if who is not None else _WHO.get()
    if who is None:
        return True
    return bool(who) and left(who) > 0


def charge(who: str | None = None) -> bool:
    """真正调用之前扣一笔；没额度返回 False，调用方当作模型不可用。"""
    who = who if who is not None else _WHO.get()
    if who is None:
        return True
    if not who:
        return False
    return store.llm_charge(_today(), _caps(who))


@contextmanager
def metered_as(who: str) -> Iterator[str]:
    """在这段里的模型调用算在伪账号头上（标题机翻 *xlate）：后台线程里也有上限，不记在任何学生头上。"""
    token = _WHO.set(who)
    try:
        yield who
    finally:
        _WHO.reset(token)


def current() -> str | None:
    return _WHO.get()
