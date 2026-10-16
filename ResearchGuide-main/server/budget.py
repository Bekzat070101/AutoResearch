# -*- coding: utf-8 -*-
"""全站伪账号额度。普通用户调用不经这里，避免改现有对话语义。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

import store

_WHO: ContextVar[str | None] = ContextVar("budget_who", default=None)
LLM_DAILY_XLATE = int(os.environ.get("LLM_DAILY_XLATE") or 300)


def total_cap() -> int:
    return int(os.environ.get("LLM_DAILY_TOTAL") or 5000)


def _caps(who: str) -> dict[str, int]:
    if who.startswith("*"):
        cap = LLM_DAILY_XLATE if who == "*xlate" else 0
        return {who: cap, "*": total_cap()}
    return {}


def consented(uid: str | None) -> bool:
    """是否接受了当前隐私说明。自带 key 也要先过这一关，不从扣费路径里顺带检查。"""
    if not uid:
        return True
    import auth as _auth
    u = store.get_user(uid) or {}
    return (u.get("consent_version") or "") == _auth.PRIVACY_VERSION


def allowed(who: str | None = None) -> bool:
    who = who if who is not None else _WHO.get()
    if not who:
        return True
    caps = _caps(who)
    if not caps:
        return True
    if store.llm_usage_today(who) >= caps.get(who, 0):
        return False
    if store.llm_usage_today("*") >= caps.get("*", total_cap()):
        return False
    return True


def charge(who: str | None = None, n: int = 1) -> None:
    who = who if who is not None else _WHO.get()
    if not who:
        return
    store.bump_llm_usage(who, n)
    if who.startswith("*") and who != "*":
        store.bump_llm_usage("*", n)


@contextmanager
def metered_as(who: str):
    token = _WHO.set(who)
    try:
        yield who
    finally:
        _WHO.reset(token)


def current() -> str | None:
    return _WHO.get()
