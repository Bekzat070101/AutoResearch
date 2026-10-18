# -*- coding: utf-8 -*-
"""Codex 第五轮压测（两百人同时用）复现的四件事：等模型的接口占满公用线程池、校园出口地址开不了号、
浏览器走了排队的对话还拿着锁、重试不扣额度。都不调真的模型。"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auth  # noqa: E402
import budget  # noqa: E402
import dialogue  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import store  # noqa: E402
import userlock  # noqa: E402
import workpool  # noqa: E402


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "r5.db")
    store.init_db()
    for k in ("LLM_DAILY_TOTAL", "LLM_DAILY_USER", "LLM_DAILY_GUEST", "LLM_DAILY_GUESTS", "TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)


def _uids(n):
    return [store.create_user(f"u{i}")["uid"] for i in range(n)]


def _blocking_turn(monkeypatch):
    """dialogue.turn 换成一直等模型的假版本。返回（进去了几个的信号量, 放行的事件, 谁进去过）。"""
    entered, release, seen = threading.Semaphore(0), threading.Event(), []

    def slow_turn(uid, message, conversation_id=None):
        seen.append(uid)
        entered.release()
        assert release.wait(10)
        return {"reply": "ok"}

    monkeypatch.setattr(dialogue, "turn", slow_turn)
    return entered, release, seen


def test_waiting_on_the_model_does_not_hold_up_ordinary_requests(monkeypatch):
    """公用线程池只有 4 个：8 个人同时在等模型，登录状态、读任务、健康检查照样马上回。原来要等模型那边放出线程。"""
    monkeypatch.setattr(main, "THREADS", 4)
    entered, release, _ = _blocking_turn(monkeypatch)
    uids = _uids(8)
    with TestClient(main.app) as c, ThreadPoolExecutor(8) as ex:
        try:
            turns = [ex.submit(c.post, "/api/dialogue/turn", json={"uid": u, "message": "在吗"}) for u in uids]
            for _ in uids:
                assert entered.acquire(timeout=5)  # 8 个都进了模型那一步：比公用池的线程多
            t0 = time.monotonic()
            assert c.get("/api/health").status_code == 200
            assert c.get(f"/api/tasks?uid={uids[0]}").status_code == 200
            assert c.post("/api/directions/choose", json={"uid": "nobody", "code": "ai"}).status_code == 404
            assert time.monotonic() - t0 < 2
            assert c.get("/api/health").json()["model_pool"]["running"] == 8
        finally:
            release.set()
        assert all(t.result(10).status_code == 200 for t in turns)


def test_the_model_queue_has_a_ceiling(monkeypatch):
    """模型池一个线程、排队一个：第三个人回 503，流式对话也是，而且没把这个人的锁留着。"""
    monkeypatch.setattr(main, "_MODEL", workpool.BoundedPool(1, 1, "t"))
    monkeypatch.setattr(main, "MODEL_QUEUE", 1)
    entered, release, seen = _blocking_turn(monkeypatch)
    a, b, c3, d = _uids(4)
    with TestClient(main.app) as c, ThreadPoolExecutor(2) as ex:
        try:
            first = ex.submit(c.post, "/api/dialogue/turn", json={"uid": a, "message": "一"})
            assert entered.acquire(timeout=5)
            second = ex.submit(c.post, "/api/dialogue/turn", json={"uid": b, "message": "二"})
            for _ in range(250):
                if main._MODEL.stats()["queued"] == 1:
                    break
                time.sleep(0.02)
            r = c.post("/api/dialogue/turn", json={"uid": c3, "message": "三"})
            assert r.status_code == 503 and r.json()["detail"] == main.CROWDED_MSG
            r = c.post("/api/dialogue/stream", json={"uid": d, "message": "四"})
            assert r.status_code == 503
            assert not userlock.busy(c3) and not userlock.busy(d)
        finally:
            release.set()
        assert first.result(10).status_code == 200 and second.result(10).status_code == 200
    assert seen == [a, b]
    assert main._MODEL.stats()["queued"] == 0


def _drive_stream(uid, disconnect_when):
    """直接驱动 ASGI 的流式对话（Uvicorn 报 ASGI 2.3，断开时 receive 回 http.disconnect）。
    disconnect_when：一个 asyncio.Event 的工厂，set 了就断开。返回 app 的协程。"""
    body = json.dumps({"uid": uid, "message": "说一句"}).encode()
    sent_request = [False]

    async def receive():
        if not sent_request[0]:
            sent_request[0] = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect_when.wait()
        return {"type": "http.disconnect"}

    started = asyncio.Event()

    async def send(msg):
        if msg["type"] == "http.response.start":
            started.set()

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": "/api/dialogue/stream", "raw_path": b"/api/dialogue/stream",
             "query_string": b"", "root_path": "", "server": ("t", 80), "client": ("127.0.0.1", 1),
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]}
    return main.app(scope, receive, send), started


def test_a_queued_turn_is_withdrawn_when_its_browser_leaves(monkeypatch):
    """模型池被占满时这一轮在排队；浏览器走了：这一轮撤掉、锁马上放，之后也不会再去调模型。原来锁一直拿到它排上、跑完。"""
    pool = workpool.BoundedPool(1, 10, "t")
    monkeypatch.setattr(main, "_MODEL", pool)
    ran = []

    def steps(uid_, message, conversation_id=None, stream_reply=False):
        ran.append(uid_)
        yield "result", {"reply": "ok"}

    monkeypatch.setattr(dialogue, "turn_steps", steps)
    uid = _uids(1)[0]
    hold, started = threading.Event(), threading.Event()
    blocker = pool.submit(lambda: started.set() or hold.wait(10))  # 别人的一轮占着唯一的线程
    assert started.wait(5)

    async def run():
        leave = asyncio.Event()
        app, started = _drive_stream(uid, leave)
        task = asyncio.create_task(app)
        await asyncio.wait_for(started.wait(), 5)
        queued_and_locked = pool.stats()["queued"] == 1 and userlock.busy(uid)
        leave.set()
        await asyncio.wait_for(task, 5)
        return queued_and_locked

    try:
        assert asyncio.run(run())
        assert not userlock.busy(uid) and pool.stats()["queued"] == 0  # 断开的那一刻就放了，不等前面那轮
    finally:
        hold.set()
    blocker.result(5)
    time.sleep(0.1)
    assert ran == []  # 撤掉的那一轮没再跑


def test_a_waiting_stream_sends_heartbeats(monkeypatch):
    """排队、等模型的时候隔一会儿发一行 SSE 注释：代理不掐空闲连接，前端跳过它照常收后面的事件。"""
    monkeypatch.setattr(main, "HEARTBEAT", 0.05)
    release = threading.Event()

    def steps(uid_, message, conversation_id=None, stream_reply=False):
        assert release.wait(5)
        yield "result", {"reply": "ok"}

    monkeypatch.setattr(dialogue, "turn_steps", steps)
    uid = _uids(1)[0]
    threading.Timer(0.3, release.set).start()
    r = TestClient(main.app).post("/api/dialogue/stream", json={"uid": uid, "message": "说一句"})
    assert r.status_code == 200
    assert r.text.startswith(": wait\n\n") and r.text.endswith('event: result\ndata: {"reply": "ok"}\n\n')
    assert not userlock.busy(uid)


@pytest.mark.real_auth
def test_two_hundred_students_behind_one_campus_address_can_all_start(monkeypatch):
    """同一个公网出口（机房、宿舍）两百人开访客号、开微信登录：都放行。原来一小时 30 个，170 个 429。"""
    monkeypatch.setenv("TRUST_PROXY", "1")
    monkeypatch.setenv("AUTH_DEV_CODES", "1")
    monkeypatch.setattr(auth, "_HITS", {})
    c = TestClient(main.app)
    nat = {"X-Forwarded-For": "203.0.113.50"}
    assert all(c.post("/api/auth/login", json={"nickname": f"同学{i}"}, headers=nat).status_code == 200
               for i in range(200))
    assert all(c.post("/api/auth/wechat/start", json={}, headers=nat).status_code == 200 for _ in range(200))


def _consented_guest(name="访客"):
    uid = store.create_user(name)["uid"]
    store.record_consent(uid, auth.PRIVACY_VERSION)
    return uid


def _wechat_user():
    uid = store.create_wechat_user("微信用户", f"openid-{time.time_ns()}")
    store.record_consent(uid, auth.PRIVACY_VERSION)
    return uid


def test_all_guests_together_share_one_daily_pool(monkeypatch):
    """访客号谁都能开：全体访客一天合计有上限，刷访客号花不到绑了微信的人的额度。"""
    monkeypatch.setenv("LLM_DAILY_GUESTS", "4")
    try:
        got = []
        for _ in range(3):
            budget.bind(_consented_guest())
            got += [budget.charge(), budget.charge()]
        assert got == [True, True, True, True, False, False]
        assert budget.left(_consented_guest()) == 0
        w = _wechat_user()
        budget.bind(w)
        assert budget.charge() and budget.left(w) == budget.user_cap() - 1
    finally:
        budget._WHO.set(None)


def test_each_upstream_attempt_is_charged(monkeypatch):
    """上游回 503 再重试成功：请求了两次就扣两笔，人和全站的计数都对得上。原来只扣一笔。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    replies = [(None, "http 503"), ({"choices": [{"message": {"content": "好"}}]}, "")]
    posts = []

    def fake_post(url, key, payload, timeout):
        posts.append(1)
        return replies[len(posts) - 1]

    monkeypatch.setattr(llm, "_post", fake_post)
    uid = _consented_guest()
    budget.bind(uid)
    try:
        assert llm.chat("s", "u") == "好"
    finally:
        budget._WHO.set(None)
    day = budget._today()
    assert len(posts) == 2 and store.llm_calls(day, uid) == 2 and store.llm_calls(day, "*") == 2


def test_no_retry_without_budget_for_it(monkeypatch):
    """只剩一笔：第一次 503 之后不再重试，不会超额。"""
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_DAILY_GUEST", "1")
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    posts = []
    monkeypatch.setattr(llm, "_post", lambda *a: posts.append(1) or (None, "http 503"))
    uid = _consented_guest()
    budget.bind(uid)
    try:
        assert llm.chat("s", "u") is None
    finally:
        budget._WHO.set(None)
    assert len(posts) == 1 and store.llm_calls(budget._today(), uid) == 1


def test_cancelled_queued_work_is_dropped_at_once():
    """线程都在忙时，排队的活被取消：当场从队里拿掉，带着的请求内容一起放掉；反复发起又断开也堆不起来。
    原来用 ThreadPoolExecutor：取消了的还留在它的内部队列里，上限 1 堆了 64 份（Codex 复现）。"""
    import gc
    import weakref

    class Body:
        pass

    pool = workpool.BoundedPool(1, 1, "t")
    hold, started = threading.Event(), threading.Event()
    blocker = pool.submit(lambda: started.set() or hold.wait(10))
    assert started.wait(5)  # 等它真的占住线程：不然它还在队里，下一个 submit 就是满的
    refs = []
    try:
        for _ in range(64):
            body = Body()
            refs.append(weakref.ref(body))
            f = pool.submit(lambda b=body: b)
            del body
            assert f.cancel()
            assert pool.stats()["queued"] == 0 and len(pool._queue) == 0
        gc.collect()
        assert all(r() is None for r in refs)  # 64 份请求内容都放掉了
    finally:
        hold.set()
    blocker.result(5)


def test_a_cancelled_request_waits_for_its_running_work_before_letting_go():
    """请求在模型那一步被取消（服务在关、外层取消域）：已经在跑的活要等它做完，拿锁的依赖才退出；
    原来立刻出去、锁先放了，切画像插进来，这一轮的结果写进了新画像（Codex 复现）。排队没开始的照旧马上撤掉。"""
    import anyio

    pool = workpool.BoundedPool(1, 4, "t")
    release, done, ran = threading.Event(), threading.Event(), []

    def slow():
        assert release.wait(5)
        done.set()

    def never():
        ran.append(1)

    orig = main._MODEL
    main._MODEL = pool
    try:
        async def go():
            threading.Timer(0.3, release.set).start()
            t0 = time.monotonic()
            with anyio.move_on_after(0.05):
                await main._model_work(slow)()
            return time.monotonic() - t0

        waited = anyio.run(go)
        assert done.is_set() and waited >= 0.25  # 做完才出来

        hold, started = threading.Event(), threading.Event()
        blocker = pool.submit(lambda: started.set() or hold.wait(5))
        assert started.wait(5)

        async def go_queued():
            t0 = time.monotonic()
            with anyio.move_on_after(0.05):
                await main._model_work(never)()
            return time.monotonic() - t0

        assert anyio.run(go_queued) < 1  # 还在排队：马上撤掉，不等前面的
        hold.set()
        blocker.result(5)
        time.sleep(0.05)
        assert ran == [] and pool.stats()["queued"] == 0
    finally:
        main._MODEL = orig


def test_draining_waits_for_running_work_and_drops_the_queue():
    """服务关闭时：在跑的那一轮（浏览器走了照样在写）做完才退，排队没开始的撤掉、不再跑；关完照常接活。
    线程是守护线程，原来不等：进程一退，写到一半就没了（Codex 复现）。"""
    pool = workpool.BoundedPool(1, 4, "t")
    started, wrote, ran = threading.Event(), [], []

    def writing():
        started.set()
        time.sleep(0.3)
        wrote.append(1)

    running = pool.submit(writing)
    queued = pool.submit(lambda: ran.append(1))
    assert started.wait(5)
    t0 = time.monotonic()
    pool.drain(wait=5)
    assert wrote == [1] and time.monotonic() - t0 >= 0.2
    assert queued.cancelled() and ran == [] and pool.stats()["queued"] == 0
    running.result(1)
    assert pool.submit(lambda: 7).result(5) == 7


def test_draining_never_starts_queued_work():
    """关的时候排队的整个摘下来再取消：原来先放锁再逐个取消，取消的间隙里刚做完的线程会拿走下一件开始跑（Codex 复现）。
    这里把「间隙」拉长：取消第一件之前先放走在跑的那一件、再等一会儿。"""
    pool = workpool.BoundedPool(1, 4, "t")
    started, release, ran = threading.Event(), threading.Event(), []
    running = pool.submit(lambda: started.set() or release.wait(5))
    assert started.wait(5)
    first = pool.submit(lambda: ran.append("first"))
    pool.submit(lambda: ran.append("second"))
    real_cancel = first.cancel

    def slow_cancel():
        release.set()
        running.result(5)
        time.sleep(0.2)  # 刚空出来的线程有时间去拿下一件
        return real_cancel()

    first.cancel = slow_cancel
    pool.drain(wait=5)
    time.sleep(0.1)
    assert ran == [] and pool.stats()["queued"] == 0
