# -*- coding: utf-8 -*-
"""同一个人的改动一件一件来。

画像切换是把「现场」几张表整体存进快照、再把另一份快照装回来；还在跑的对话（等模型一两分钟）
回来时写进的是那一刻的现场——也就是新画像。Codex 复现了三种后果：旧画像的回复写进了新画像、
两次并发的项目评阅只留下一份、两张并发提交的阅读卡都是 v1。
这里给每个人一把锁：会改这个人数据的请求（对话、交任务、交卡、切画像……）拿到锁才执行，
别的请求最多等 WAIT 秒，等不到就回 409「上一件事还在处理」。

- 只在单个进程里有效：部署要求单实例（见 docs/DEPLOY.md），多实例本来就会分裂。
- 用 threading.Lock 而不是 RLock：流式对话的生成器在不同的工作线程里推进，拿锁和放锁可能不在同一个线程。
  也因此不能嵌套：只在接口这一层拿，库函数里不拿。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

WAIT = 15.0  # 秒：等前一件事做完的最长时间

_GUARD = threading.Lock()
_LOCKS: dict[str, list] = {}  # uid → [Lock, 在用/在等的人数]；没人用就删，字典不会越长越大


class Busy(Exception):
    """这个人还有一件事没做完。"""


def _ref(uid: str) -> threading.Lock:
    with _GUARD:
        slot = _LOCKS.setdefault(uid, [threading.Lock(), 0])
        slot[1] += 1
        return slot[0]


def _unref(uid: str) -> None:
    with _GUARD:
        slot = _LOCKS.get(uid)
        if slot is not None:
            slot[1] -= 1
            if slot[1] <= 0:
                del _LOCKS[uid]


@contextmanager
def hold(uid: str, wait: float | None = None) -> Iterator[None]:
    """阻塞地等这个人的锁（在工作线程里调，别在事件循环上调）。等不到抛 Busy。wait 默认取模块的 WAIT。"""
    wait = WAIT if wait is None else wait
    lock = _ref(uid)
    try:
        got = lock.acquire(timeout=wait) if wait > 0 else lock.acquire(blocking=False)
        if not got:
            raise Busy(uid)
        try:
            yield
        finally:
            lock.release()
    finally:
        _unref(uid)


MAX_WAITERS = 4  # 同一个人同时最多几个请求在等锁；再多直接 409（一个人连发几百个改动不该占满服务）
_POLL = 0.02


class Ticket:
    """异步拿到的锁。release 可以调多次，只放一次。"""

    def __init__(self, uid: str, lock: threading.Lock) -> None:
        self.uid, self._lock, self._done = uid, lock, False
        self._mu = threading.Lock()

    def release(self) -> None:
        with self._mu:
            if self._done:
                return
            self._done = True
        self._lock.release()
        _unref(self.uid)


async def acquire(uid: str, wait: float | None = None) -> Ticket:
    """在事件循环里等这个人的锁：轮询 + asyncio.sleep，不占任何工作线程。
    原来在工作线程里阻塞地等：同一个人的几十个改动请求就能把所有工作线程占住，
    拿着锁的那个请求反而拿不到线程去做完、去放锁（Codex 复现）。等不到或排队的人太多抛 Busy。"""
    import asyncio
    wait = WAIT if wait is None else wait
    with _GUARD:
        slot = _LOCKS.setdefault(uid, [threading.Lock(), 0])
        if slot[1] > MAX_WAITERS:  # 拿着锁的一个 + 在等的 MAX_WAITERS 个
            raise Busy(uid)
        slot[1] += 1
        lock = slot[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    try:
        while not lock.acquire(blocking=False):
            if loop.time() >= deadline:
                raise Busy(uid)
            await asyncio.sleep(_POLL)
    except BaseException:
        _unref(uid)
        raise
    return Ticket(uid, lock)


def busy(uid: str) -> bool:
    with _GUARD:
        slot = _LOCKS.get(uid)
    return bool(slot) and slot[0].locked()
