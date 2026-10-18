# -*- coding: utf-8 -*-
"""Codex 第八轮（整个分支对 main）复现的问题。"""
from __future__ import annotations

import asyncio
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from singleflight import AsyncFlight  # noqa: E402


def test_a_running_shared_job_is_not_started_again_after_its_waiter_leaves():
    """等同一篇论文的人走光了，但抓取已经开始：后来的人接着等这一次，不再占一个新线程重跑。
    原来 key 照样删掉，重试一次就多占一个线程，四个线程能被同一篇论文占满（Codex 复现）。"""
    pool = ThreadPoolExecutor(max_workers=2)
    flight = AsyncFlight(pool, max_pending=8)
    started, release, calls = threading.Event(), threading.Event(), []

    def fetch():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return "paper"

    async def go():
        first = asyncio.create_task(flight.do(("paper", "2401.00001"), fetch))
        while not started.is_set():
            await asyncio.sleep(0.01)
        first.cancel()  # 第一个人断开：活已经在跑
        await asyncio.gather(first, return_exceptions=True)
        assert flight.in_flight() == 1  # 还记着
        second = asyncio.create_task(flight.do(("paper", "2401.00001"), fetch))
        await asyncio.sleep(0.05)
        release.set()
        return await second

    try:
        assert asyncio.run(go()) == "paper"
        assert calls == [1]
        assert flight.in_flight() == 0
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_a_queued_shared_job_is_still_dropped_when_nobody_waits():
    """还在排队（线程都忙）时等的人走光：照旧撤掉，不白跑。"""
    pool = ThreadPoolExecutor(max_workers=1)
    flight = AsyncFlight(pool, max_pending=8)
    hold, ran = threading.Event(), []
    blocker = pool.submit(hold.wait, 5)

    async def go():
        t = asyncio.create_task(flight.do(("paper", "x"), lambda: ran.append(1)))
        await asyncio.sleep(0.05)
        t.cancel()
        await asyncio.gather(t, return_exceptions=True)
        await asyncio.sleep(0.05)
        return flight.in_flight()

    try:
        assert asyncio.run(go()) == 0
    finally:
        hold.set()
        blocker.result(5)
        pool.shutdown(wait=True)
    assert ran == []


def test_a_cancelled_queued_review_lets_go_of_its_zip(monkeypatch):
    """评阅线程都在忙时，排着队的那份交了又断开：当场从队里拿掉、压缩包放掉，名额也还回来。
    原来计数先减了，活还留在 ThreadPoolExecutor 里握着 20 MB：反复交了又断开，上限 6 的队里堆了 32 份（Codex 复现）。"""
    import httpx

    import main
    import submission
    import workpool

    pool = workpool.BoundedPool(1, 1, "t")
    monkeypatch.setattr(main, "_REVIEW", pool)
    monkeypatch.setattr(main, "REVIEW_PENDING_MAX", 2)
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    started, release = threading.Event(), threading.Event()

    def slow_review(data, p):
        started.set()
        assert release.wait(5)
        return {"passed": 0, "total": 5}

    monkeypatch.setattr(submission, "review", slow_review)
    monkeypatch.setattr(main, "_review_and_record", lambda uid, p, data, portrait="": slow_review(data, p))

    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            first = asyncio.create_task(c.post("/api/projects/a/submit?uid=u", content=b"PK" + b"x" * 1000))
            while not started.is_set():
                await asyncio.sleep(0.01)
            second = asyncio.create_task(c.post("/api/projects/b/submit?uid=u", content=b"PK" + b"y" * 1000))
            for _ in range(200):
                if pool.stats()["queued"] == 1:
                    break
                await asyncio.sleep(0.01)
            queued_before = pool.stats()["queued"]
            second.cancel()  # 第二个人断开
            await asyncio.gather(second, return_exceptions=True)
            await asyncio.sleep(0.05)
            queued_after, held = pool.stats()["queued"], len(pool._queue)
            release.set()
            return queued_before, queued_after, held, (await first).status_code

    try:
        assert asyncio.run(go()) == (1, 0, 0, 200)
    finally:
        release.set()


def test_only_real_code_cell_outputs_count_as_results():
    """Markdown 单元挂个 outputs、或者代码单元只有报错输出：不算「跑出了结果」。原来 outputs 不空就算（Codex 复现）。"""
    import json

    import submission

    def nb(cells):
        return json.dumps({"cells": cells}).encode()

    assert submission._ipynb_text(nb([{"cell_type": "markdown", "source": "x", "outputs": [{"output_type": "stream"}]}]))[1] is False
    assert submission._ipynb_text(nb([{"cell_type": "code", "source": "x", "outputs": [{"output_type": "error"}]}]))[1] is False
    assert submission._ipynb_text(nb([{"cell_type": "code", "source": "x", "outputs": [{"meta": 1}]}]))[1] is False
    assert submission._ipynb_text(nb([{"cell_type": "code", "source": "x", "outputs": [{"output_type": "display_data"}]}]))[1] is True


def test_a_limitation_quote_also_found_earlier_is_matched_in_the_limitations_section(monkeypatch):
    """同一句先在引言里出现过、又在局限段里：按局限段算。原来只看第一次出现，判成「出自引言」（Codex 复现）。"""
    import arxiv
    import quotes

    sentence = "our evaluation covers only english benchmarks and may not transfer"
    text = ("1 Introduction\n" + "We note that " + sentence + ". " + "filler words here. " * 40
            + "\n5 Limitations\n" + "Finally, " + sentence + ".\n")
    paper = {"text": text, "source": "fulltext"}
    secs = arxiv.sections(text)
    assert [n for n, _ in secs][:2] == ["introduction", "limitations"] or "limitations" in [n for n, _ in secs]
    first = quotes.locate(sentence, paper)
    assert first["found"] and first["section"] != "limitations"  # 不指定时照旧报第一次出现
    best = quotes.locate(sentence, paper, prefer={"limitations"})
    assert best["found"] and best["section"] == "limitations"
