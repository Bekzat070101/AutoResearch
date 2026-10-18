# -*- coding: utf-8 -*-
"""公开接口扛得住乱发（Codex 复现过的两种）：
- 1.7 KB 的匿名 MCP 批量请求在事件循环上同步算，把整个服务卡住 3.7 秒；
- 连发 120 个不同的论文号，排上 120 个抓取任务；请求取消了，任务还留在队列里。
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import mcp_curriculum  # noqa: E402
import store  # noqa: E402
from singleflight import AsyncFlight, Overloaded  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "load.db")
    store.init_db()


def _asgi():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t")


def test_mcp_work_does_not_block_the_event_loop(monkeypatch):
    monkeypatch.setattr(mcp_curriculum, "http_handle", lambda payload: time.sleep(0.6) or {"jsonrpc": "2.0", "id": 1})

    async def main_():
        beats, done = [], asyncio.Event()

        async def heartbeat():
            while not done.is_set():
                beats.append(time.perf_counter())
                await asyncio.sleep(0.005)

        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.03)
        async with _asgi() as c:
            r = await c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        done.set()
        await hb
        return r, max(b - a for a, b in zip(beats, beats[1:]))

    r, gap = asyncio.run(main_())
    assert r.status_code == 200
    assert gap < 0.2  # 算的那 0.6 秒里，事件循环照常转（原来整段都卡住）


def test_mcp_rejects_oversized_bodies_and_batches():
    async def run():
        async with _asgi() as c:
            big = await c.post("/mcp", content=b"[" + b"1," * 40000 + b"1]", headers={"Content-Type": "application/json"})
            batch = await c.post("/mcp", json=[{"jsonrpc": "2.0", "id": i, "method": "tools/list"} for i in range(17)])
            ok = await c.post("/mcp", json=[{"jsonrpc": "2.0", "id": i, "method": "tools/list"} for i in range(3)])
        return big, batch, ok

    big, batch, ok = asyncio.run(run())
    assert big.status_code == 413 and batch.status_code == 413
    assert ok.status_code == 200 and len(ok.json()) == 3


def test_paper_text_needs_login():
    import auth
    assert "/api/papers/{arxiv_id}" not in auth.PUBLIC  # 匿名不能排抓取任务；test_auth 的全量扫描会验证它回 401


def test_flight_caps_pending_work_and_drops_abandoned_jobs():
    release = threading.Event()
    started = []

    def slow(i):
        started.append(i)
        release.wait(5)
        return i

    pool = ThreadPoolExecutor(max_workers=2)
    flight = AsyncFlight(pool, max_pending=5)

    async def run():
        tasks = [asyncio.create_task(flight.do(("k", i), slow, i)) for i in range(5)]
        await asyncio.sleep(0.05)
        with pytest.raises(Overloaded):
            await flight.do(("k", 99), slow, 99)  # 第六个不同的活：不收
        same = asyncio.create_task(flight.do(("k", 0), slow, 0))  # 已经在做的同一个活：照样能等
        await asyncio.sleep(0.02)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.05)
        abandoned_in_flight = flight.in_flight()
        release.set()
        return abandoned_in_flight, await same

    abandoned_in_flight, same_result = asyncio.run(run())
    pool.shutdown(wait=True)  # 等线程池把队列里剩下的都处理完
    assert abandoned_in_flight <= 2  # 等的人都走了的活不再占名额（原来 116 个全留着）
    assert same_result == 0  # 还有人在等的那个活照样做完
    assert sorted(started) == [0, 1]  # 排队的活被取消、从来没跑：只有两个工作线程先拿到的跑了


def test_many_distinct_papers_get_a_busy_answer_not_an_unbounded_queue(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main, "_paper_cached", lambda aid: None)
    monkeypatch.setattr(main, "_paper_payload", lambda aid: release.wait(5) and {"id": aid, "text": "t"})

    async def run():
        async with _asgi() as c:
            reqs = [asyncio.create_task(c.get(f"/api/papers/2601.{10000 + i}")) for i in range(60)]
            await asyncio.sleep(0.3)
            pending = main._FLIGHTS["arxiv"].in_flight()
            release.set()
            done = await asyncio.gather(*reqs)
        return pending, [r.status_code for r in done]

    pending, codes = asyncio.run(run())
    assert pending <= main._FLIGHTS["arxiv"].max_pending
    assert codes.count(503) >= 60 - main._FLIGHTS["arxiv"].max_pending
