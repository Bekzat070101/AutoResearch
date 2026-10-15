# -*- coding: utf-8 -*-
"""有界线程池：队列满了立刻拒绝，不无限堆积。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class PoolFull(Exception):
    pass


class BoundedPool:
    def __init__(self, workers: int, queue: int, name: str) -> None:
        self._ex = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self.max_queued = max(1, workers + queue)
        self._sem = threading.Semaphore(self.max_queued)
        self._lock = threading.Lock()
        self._held = 0
        self._running = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            running = self._running
            held = self._held
        return {"queued": max(0, held - running), "running": running, "max_queued": self.max_queued}

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        if not self._sem.acquire(blocking=False):
            raise PoolFull()
        with self._lock:
            self._held += 1

        def wrap() -> Any:
            with self._lock:
                self._running += 1
            try:
                return fn(*args, **kwargs)
            finally:
                with self._lock:
                    self._running -= 1
                    self._held -= 1
                self._sem.release()

        return self._ex.submit(wrap)
