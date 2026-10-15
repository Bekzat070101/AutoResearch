# -*- coding: utf-8 -*-
"""要等模型的活在自己的线程池里跑，排队有上限。

公用的工作线程池（Starlette 的，main.THREADS 个）给普通读写。原来对话、交任务、方向推荐这些同步接口
也在里面等模型：两百个人同时等模型，128 个线程全占住，连 /api/health、/api/auth/me 都排队五秒（Codex 第五轮压测复现）。
挪到这里以后公用池永远有空；这里满了，新来的在队里等，队也满了就 Overloaded（接口回 503）。

- submit 带上调用方的上下文变量：模型额度算在谁头上（budget.py）靠它。
- 还没开始的活可以 cancel：浏览器走了、排队的那一轮就不跑了（main.dialogue_stream）。取消的活当场从队里拿掉，
  连同它带着的请求内容一起放掉。不用 ThreadPoolExecutor：它取消了的活还留在内部队列里，要等线程空出来才扔，
  线程都在等模型时，一个人反复发起又断开就能在里面堆上任意多份（Codex 复现：上限 1，堆了 64 份）。
"""
from __future__ import annotations

import contextvars
import functools
import itertools
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from typing import Any, Callable

from singleflight import Overloaded

PoolFull = Overloaded  # 上游 w80 的调用方按这个名字接（pdf 池）


class BoundedPool:
    def __init__(self, workers: int, max_queued: int, name: str) -> None:
        self.workers, self.max_queued, self._name = workers, max_queued, name
        self._cv = threading.Condition()
        self._queue: OrderedDict[int, tuple] = OrderedDict()  # 排上了、还没开始的：序号 → (future, 上下文, 函数, 参数)
        self._seq = itertools.count()
        self._threads = 0
        self._idle = 0      # 在等活的线程
        self._closing = False
        self.running = 0

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
        """排上队返回 Future；排队的已经有 max_queued 个、或者服务在关，抛 Overloaded，什么都不做。"""
        if kwargs:
            fn = functools.partial(fn, **kwargs)
        fut: Future = Future()
        ctx = contextvars.copy_context()
        with self._cv:
            if self._closing or len(self._queue) >= self.max_queued:
                raise Overloaded()
            key = next(self._seq)
            self._queue[key] = (fut, ctx, fn, args)
            if len(self._queue) > self._idle and self._threads < self.workers:  # 闲着的不够分：再开一个线程
                self._threads += 1
                threading.Thread(target=self._work, name=f"{self._name}-{self._threads}", daemon=True).start()
            self._cv.notify()
        fut.add_done_callback(lambda f: f.cancelled() and self._drop(key))
        return fut

    def _drop(self, key: int) -> None:
        with self._cv:
            self._queue.pop(key, None)

    def _work(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._closing:
                    self._idle += 1
                    self._cv.wait()
                    self._idle -= 1
                if not self._queue:  # 在关、也没活了
                    self._threads -= 1
                    return
                _, job = self._queue.popitem(last=False)
                if job[0].set_running_or_notify_cancel():
                    self.running += 1
                else:
                    job = None  # 拿出来的那一刻刚被取消：也要放掉它带着的请求内容，别留到下一件活来
            if job is not None:
                self._run(*job)
            job = None

    def _run(self, fut: Future, ctx: contextvars.Context, fn: Callable[..., Any], args: tuple) -> None:
        try:
            fut.set_result(ctx.run(fn, *args))
        except BaseException as exc:  # noqa: BLE001 — 原样交给等结果的人
            fut.set_exception(exc)
        finally:
            with self._cv:
                self.running -= 1
                self._cv.notify_all()  # drain 在等

    def drain(self, wait: float = 60.0) -> None:
        """服务关闭时调（main._lifespan，另有 atexit 兜底）：排队没开始的撤掉，已经在跑的等它们做完（最多 wait 秒），
        期间新来的回 Overloaded；完了照常接活（测试里同一个进程会反复起停应用）。
        线程是守护线程：不等的话进程一退，浏览器断开后还在写的那一轮（回复、记忆）就写一半没了（Codex 复现）。"""
        with self._cv:
            self._closing = True
            # 排队的在锁里整个摘下来再取消：先放锁再逐个取消的话，刚做完的线程会趁空拿走一件开始跑（Codex 复现）
            queued = list(self._queue.values())
            self._queue.clear()
            self._cv.notify_all()
        for job in queued:
            job[0].cancel()
        del queued
        deadline = time.monotonic() + wait
        with self._cv:
            while self.running and time.monotonic() < deadline:
                self._cv.wait(timeout=max(0.0, deadline - time.monotonic()))
            self._closing = False

    def stats(self) -> dict[str, int]:
        with self._cv:
            return {"workers": self.workers, "threads": self._threads, "running": self.running,
                    "queued": len(self._queue), "max_queued": self.max_queued}
