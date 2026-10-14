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
        self._sem = threading.Semaphore(max(1, workers + queue))

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any):
        if not self._sem.acquire(blocking=False):
            raise PoolFull()

        def wrap() -> Any:
            try:
                return fn(*args, **kwargs)
            finally:
                self._sem.release()

        return self._ex.submit(wrap)
