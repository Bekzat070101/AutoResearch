# -*- coding: utf-8 -*-
"""性能与资源上限：评阅不阻塞事件循环、上传边收边限、.docx 套娃有上限、路径扫描线性、
并发相同请求只打一次上游、项目检索共用有上限的线程池、最新卡查询走索引。全部离线。"""
from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import pku_adapter  # noqa: E402
import project_adapters  # noqa: E402
import projects  # noqa: E402
import store  # noqa: E402
import submission  # noqa: E402
from limits import TTLCache  # noqa: E402
import limits  # noqa: E402
from singleflight import SingleFlight  # noqa: E402


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "perf.db")
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path / "papers")  # 不读本机真实的论文缓存
    store.init_db()
    monkeypatch.setattr(llm, "chat", lambda *a, **k: None)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)


# 计时断言要在慢的 CI 机器上也稳：被模拟的阻塞活都拉长到 BLOCK，「没被拖住」的门槛 QUICK 远小于它。
# 两者差一倍，机器慢一倍也分得清「被拖住了」和「没被拖住」。线性/平方这类断言的门槛都比旧实现的耗时低一个数量级以上。
BLOCK = 1.5
QUICK = 0.75


def _asgi():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t")


def _dns_idle(timeout: float = 5.0) -> None:
    """等前面测试留下的慢解析跑完，免得占着解析线程影响下一个测试。"""
    import limits
    end = time.monotonic() + timeout
    while limits._DNS_INFLIGHT and time.monotonic() < end:
        time.sleep(0.05)


def test_review_runs_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    monkeypatch.setattr(submission, "review", lambda data, p: time.sleep(BLOCK) or {"passed": 0})
    monkeypatch.setattr(projects, "record_review", lambda uid, p, r: None)

    async def go():
        async with _asgi() as c:
            sub = asyncio.create_task(c.post("/api/projects/p/submit?uid=u", content=b"PK\x03\x04zip"))
            await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            waited = time.perf_counter() - t0
            return (await sub).status_code, health.status_code, waited

    sub_code, health_code, waited = asyncio.run(go())
    assert sub_code == 200 and health_code == 200 and waited < QUICK


def test_upload_limit_stops_reading_early(monkeypatch):
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    monkeypatch.setattr(submission, "MAX_ZIP_BYTES", 1000)
    sent = []

    async def body():
        for _ in range(100):
            sent.append(1)
            yield b"x" * 100

    async def go():
        async with _asgi() as c:
            declared = await c.post("/api/projects/p/submit?uid=u", content=b"x" * 2000)
            streamed = await c.post("/api/projects/p/submit?uid=u", content=body())
            return declared.status_code, streamed.status_code

    assert asyncio.run(go()) == (413, 413)
    assert len(sent) < 20  # 超过 1000 字节就停，没有把 10 000 字节都读完


def test_review_queue_is_bounded(monkeypatch):
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    monkeypatch.setattr(main, "_review_full", lambda: True)

    async def go():
        async with _asgi() as c:
            return (await c.post("/api/projects/p/submit?uid=u", content=b"PK")).status_code

    assert asyncio.run(go()) == 503


def _docx(xml: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_docx_inner_xml_is_bounded():
    assert "你好" in submission._docx_text(_docx("<w:p>你好</w:p>".encode()))
    bomb = _docx(b"<w:t>" + b"a" * (24 * 1024 * 1024) + b"</w:t>")
    assert len(bomb) < 100_000
    t0 = time.perf_counter()
    assert submission._docx_text(bomb) == ""
    assert time.perf_counter() - t0 < 0.5
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", "# 题目\n")
        z.writestr("报告.docx", bomb)
    bundle = submission.read_zip(outer.getvalue())
    assert bundle["texts"]["报告.docx"] == "" and any("报告.docx" in n for n in bundle["notes"])


def test_path_scan_is_linear_and_matches_old_behaviour():
    import re
    old = r"[\w\-./一-鿿]+\.(?:png|jpg|jpeg|svg|csv|xlsx|pdf|ipynb|py|txt|md|json|docx|r|m)"
    for text in ["见 results/fig1.png 和 data.csv。", "./a.PNG b/c.md.bak x.pngx 中文/图.jpg", "a.pngb.csv foo.r.m", "无"]:
        assert set(re.findall(old, text, flags=re.I)) == submission._path_tokens(text)
    t0 = time.perf_counter()
    submission._path_tokens("a" * 64 * 1024)
    assert time.perf_counter() - t0 < 0.5  # 原来 16 KB 就要一秒，64 KB 要十几秒


def test_singleflight_shares_one_call():
    sf, calls = SingleFlight(), []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return 42

    out = []
    ts = [threading.Thread(target=lambda: out.append(sf.do("k", slow))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [1] and out == [42] * 8 and sf.in_flight() == 0


def test_concurrent_identical_arxiv_and_course_lookups_hit_upstream_once(monkeypatch):
    monkeypatch.setattr(arxiv, "_META", TTLCache(60, 16))
    got = []
    feed = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20: got.append(url) or time.sleep(0.2) or feed)
    ts = [threading.Thread(target=arxiv.query, args=({"id_list": "2310.17623"},)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 1

    monkeypatch.setattr(pku_adapter, "_SEARCH_CACHE", TTLCache(60, 16))
    monkeypatch.setattr(pku_adapter.catalog, "available", lambda: False)  # 不走本机学期快照，测实时检索那条路
    monkeypatch.setattr(pku_adapter, "_default_term", lambda: "25-26-1")
    runs = []
    monkeypatch.setattr(pku_adapter, "_run_pku", lambda args, timeout=60: runs.append(args) or time.sleep(0.2) or (0, {"items": []}, ""))
    ts = [threading.Thread(target=pku_adapter.search_courses, args=("统计",)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(runs) == 1


def test_project_searches_share_a_bounded_pool(monkeypatch):
    fetched = []

    def slow(src, direction, stage, terms):
        fetched.append(src["id"])
        time.sleep(0.6)
        return [{"title": "神经网络 实验", "url": f"https://x/{src['id']}"}]

    sources = [{"id": f"s{i}", "name": f"S{i}", "directions": ["ai"], "adapter": "slow", "samples": [{"title": "快照", "url": "https://x/s"}]}
               for i in range(6)]
    monkeypatch.setattr(projects, "registry", lambda: {"sources": sources})
    monkeypatch.setattr(project_adapters, "ADAPTERS", {"slow": slow})
    monkeypatch.setattr(projects, "SEARCH_BUDGET", 0.2)
    monkeypatch.setattr(store, "list_projects", lambda uid: [])
    peak = []

    def run():
        projects.search("u", "ai", 1)
        peak.append(sum(t.name.startswith("source") for t in threading.enumerate()))

    ts = [threading.Thread(target=run) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max(peak) <= projects.SOURCE_WORKERS
    assert sorted(fetched) == sorted(set(fetched))  # 六次相同检索，每个来源只打一次上游
    time.sleep(0.7)


def test_latest_cards_uses_the_version_index():
    with sqlite3.connect(store.DB_PATH) as c:
        plan = c.execute(
            "EXPLAIN QUERY PLAN SELECT data FROM cards c WHERE user_id='u' AND kit_id='k' AND version = "
            "(SELECT MAX(version) FROM cards c2 WHERE c2.user_id=c.user_id AND c2.kit_id=c.kit_id AND c2.arxiv_id=c.arxiv_id)").fetchall()
    assert any("idx_cards_ver" in row[-1] for row in plan)


# ---------- 第二轮 ----------

def test_many_identical_paper_requests_share_one_fetch_without_holding_threads(monkeypatch):
    calls = []
    paper = {"id": "2310.17623", "title": "T", "source": "html", "url": "u", "text": "Abstract\nx\n6 Limitations\ny"}
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: calls.append(aid) or time.sleep(BLOCK) or paper)

    async def go():
        async with _asgi() as c:
            papers = [asyncio.create_task(c.get("/api/papers/2310.17623")) for _ in range(40)]
            await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            waited = time.perf_counter() - t0
            return [r.status_code for r in await asyncio.gather(*papers)], health.status_code, waited

    codes, health, waited = asyncio.run(go())
    assert set(codes) == {200} and health == 200 and len(calls) == 1 and waited < QUICK


def test_identical_searches_do_not_starve_other_sources(monkeypatch):
    slow_calls = []

    def slow(src, direction, stage, terms):
        slow_calls.append(1)
        time.sleep(0.8)
        return [{"title": "神经网络 慢", "url": "https://x/slow"}]

    def fast(src, direction, stage, terms):
        return [{"title": "神经网络 快", "url": "https://x/fast"}]

    sources = [{"id": "slow", "name": "慢", "directions": ["ai"], "adapter": "slow", "samples": []},
               {"id": "fast", "name": "快", "directions": ["ai"], "adapter": "fast", "samples": []}]
    monkeypatch.setattr(projects, "registry", lambda: {"sources": sources})
    monkeypatch.setattr(project_adapters, "ADAPTERS", {"slow": slow, "fast": fast})
    monkeypatch.setattr(projects, "SEARCH_BUDGET", 0.4)
    monkeypatch.setattr(store, "list_projects", lambda uid: [])
    results = []
    ts = [threading.Thread(target=lambda: results.append(projects.search("u", "ai", 1))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    for r in results:
        st = {s["id"]: s for s in r["sources"]}
        assert st["fast"].get("live")  # 快来源没被八个相同的慢任务挤到超时
    assert len(slow_calls) == 1
    time.sleep(0.9)


class _Trickle(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        size = 4096 if self.path == "/big" else 200
        for _ in range(size):
            self.wfile.write(b"x" * (256 if self.path == "/big" else 1))
            self.wfile.flush()
            if self.path != "/big":
                time.sleep(0.02)

    def log_message(self, *a):
        pass


@pytest.fixture()
def trickle():
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Trickle)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_external_reads_have_a_total_deadline_and_byte_cap(trickle, monkeypatch):
    t0 = time.perf_counter()
    with pytest.raises(project_adapters.AdapterError):
        project_adapters.http(trickle + "/slow", timeout=0.2, as_json=False)
    assert time.perf_counter() - t0 < 1.5  # 每 20 毫秒滴一个字节、要滴四秒；单看 socket 超时永远不会触发

    monkeypatch.setattr(project_adapters, "MAX_BYTES", 10_000)
    with pytest.raises(project_adapters.AdapterError):
        project_adapters.http(trickle + "/big", timeout=5, as_json=False)

    monkeypatch.setattr(arxiv, "_LAST_CALL", [0.0])
    t0 = time.perf_counter()
    with pytest.raises(arxiv.ArxivError):
        arxiv._get(trickle + "/slow", timeout=0.2)
    assert time.perf_counter() - t0 < 1.5


def test_stalled_uploads_time_out_and_do_not_block_ready_ones(monkeypatch):
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    monkeypatch.setattr(main, "UPLOAD_IDLE_SECONDS", 0.3)
    monkeypatch.setattr(submission, "review", lambda data, p: {"passed": 1})
    monkeypatch.setattr(projects, "record_review", lambda uid, p, r: None)

    async def stalled():
        yield b"PK"
        await asyncio.sleep(5)
        yield b"never"

    async def go():
        async with _asgi() as c:
            stuck = [asyncio.create_task(c.post("/api/projects/p/submit?uid=u", content=stalled())) for _ in range(6)]
            await asyncio.sleep(0.05)
            ready = await c.post("/api/projects/p/submit?uid=u", content=b"PK\x03\x04zip")
            return ready.status_code, [r.status_code for r in await asyncio.gather(*stuck)]

    t0 = time.perf_counter()
    ready, stuck = asyncio.run(go())
    assert ready == 200 and set(stuck) == {408} and time.perf_counter() - t0 < 3


def test_rarity_reads_peers_in_batches(monkeypatch):
    import json as _json
    now = "2026-10-03T00:00:00+00:00"
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)",
                      [(f"c{i}", f"u{i}", "llm-eval", "2310.17623", 1, _json.dumps({}), "pass", now) for i in range(1000)])
        # 列名写全：facts 表后来加了 valid_until / affects（老库由 store._MIGRATIONS 补），
        # 按位置塞 11 个值会在加了列的库上直接报「13 columns but 11 values」。
        c.executemany(
            "INSERT INTO facts(id,user_id,category,key,value,confidence,source,evidence,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(f"f{i}", f"u{i}", "capability", "card:llm-eval:2310.17623", "v", 0.8,
              "behavior", "[]", "active", now, now) for i in range(1000)])
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"e{i}", f"u{i}", "language", "粤语母语", "", "", now) for i in range(0, 1000, 3)])
    me = store.create_user("me")["uid"]
    store.add_edge(me, "language", "粤语母语")
    store.add_edge(me, "course", "修过《数理统计》")
    opened = []
    real = store._conn
    monkeypatch.setattr(store, "_conn", lambda: opened.append(1) or real())
    import positioning
    t0 = time.perf_counter()
    r = positioning.combo_rarity(me, "llm-eval")
    assert r["status"] == "ok" and len(opened) < 20 and time.perf_counter() - t0 < 2.0


def test_quote_checks_prepare_each_paper_once(monkeypatch):
    import quotes
    import reading
    from test_reading import DIMS, GOOD, PAPER
    quotes.clear_prepared()
    calls = []
    real = quotes._index
    monkeypatch.setattr(quotes, "_index", lambda text: calls.append(1) or real(text))
    review = reading.review_card(reading.kit("llm-eval"), PAPER, GOOD, DIMS, [])
    assert review["pass"] and len(calls) == 1


def test_ttl_cache_drops_expired_entries_and_caps_size(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    c = TTLCache(0.05, 1000)
    for i in range(200):
        c.set(i, i)
    clock[0] += 0.06
    assert c.lookup("other") == (False, None) and len(c) == 0
    small = TTLCache(60, 3)
    for i in range(10):
        small.set(i, [])
    assert len(small) == 3 and small.lookup(9) == (True, []) and small.lookup(0) == (False, None)


# ---------- 第三轮 ----------

def test_malformed_docx_xml_is_stripped_in_linear_time():
    t0 = time.perf_counter()
    assert submission._docx_text(_docx(b"<" * 66_000)) == ""
    assert submission._strip_xml("<w:p><w:t>a</w:t></w:p>x<y") == "a\nx"
    assert time.perf_counter() - t0 < 1.0  # 原来要两秒多


def test_zip_with_too_many_entries_is_rejected_before_parsing(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(submission.MAX_ENTRIES + 500):
            z.writestr(f"d{i}/", b"")
        z.writestr("README.md", "# 题目")
    data = buf.getvalue()
    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(1) or real(*a, **k))
    with pytest.raises(submission.SubmissionError):
        submission.read_zip(data)
    assert opened == []  # 没等 zipfile 把几千个条目对象建出来就拒了


def test_daily_failure_is_shared_not_retried_per_request(monkeypatch):
    from limits import TTLCache as _T
    monkeypatch.setattr(main, "_DAILY_MISS", _T(30, 8))
    uid = store.create_user("t")["uid"]
    calls = []

    def down(*a, cached_only=False, **k):
        if cached_only:
            return None  # 缓存里没有
        calls.append(1)
        time.sleep(BLOCK)
        raise arxiv.ArxivError("连不上 arXiv")

    monkeypatch.setattr(arxiv, "recent", down)

    async def go():
        async with _asgi() as c:
            reqs = [asyncio.create_task(c.get(f"/api/daily?uid={uid}&kit=llm-eval")) for _ in range(40)]
            await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            waited = time.perf_counter() - t0
            rs = await asyncio.gather(*reqs)
            again = await c.get(f"/api/daily?uid={uid}&kit=llm-eval")  # 三十秒内直接用失败结果
            return rs, health.status_code, waited, again

    rs, health, waited, again = asyncio.run(go())
    assert {r.status_code for r in rs} == {200} and all(r.json()["error"] for r in rs)
    assert health == 200 and waited < QUICK and len(calls) == 1 and again.json()["error"]


class _SlowHead(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_GET(self):
        for b in b"HTTP/1.0 200 OK\r\nX-Pad: " + b"a" * 200:
            self.wfile.write(bytes([b]))
            self.wfile.flush()
            time.sleep(0.02)

    def log_message(self, *a):
        pass


def test_deadline_covers_slow_response_headers(monkeypatch):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHead)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/h"
    try:
        t0 = time.perf_counter()
        with pytest.raises(project_adapters.AdapterError):
            project_adapters.http(url, timeout=0.2, as_json=False)
        assert time.perf_counter() - t0 < 1.5  # 响应头要滴四秒多
        monkeypatch.setattr(arxiv, "_LAST_CALL", [0.0])
        t0 = time.perf_counter()
        with pytest.raises(arxiv.ArxivError):
            arxiv._get(url, timeout=0.2)
        assert time.perf_counter() - t0 < 1.5  # 限速锁也只被占这么久
    finally:
        srv.shutdown()


def test_matrix_conflicts_are_grouped_not_paired():
    import json as _json
    import reading
    now = "2026-10-03T00:00:00+00:00"
    cards = []
    for i in range(500):
        aid = f"2401.{i:05d}"
        data = {"arxiv_id": aid, "title": f"P{i}", "version": 1, "status": "pass",
                "dims": {"metric": "准确率", "direction": "up" if i % 2 else "down", "models": "m", "task": "t", "language": "英文"}}
        cards.append((f"c{i}", "u", "llm-eval", aid, 1, _json.dumps(data), "pass", now))
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)", cards)
    m = reading.matrix("u", "llm-eval")
    (g,) = m["flags"]["conflicts"]
    assert (g["up_total"], g["down_total"]) == (250, 250) and len(g["up"]) == reading.CONFLICT_SHOW
    assert len(_json.dumps(m["flags"]["conflicts"])) < 2000


def test_statement_refs_group_once():
    import json as _json
    rows = [(f"{u}-{v}", f"u{u}", "k", v, f"r{v}", _json.dumps({}), f"2026-09-{1 if v <= 5 else 21:02d}T00:00:00+00:00")
            for u in range(60) for v in range(1, 301)]
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO statements VALUES(?,?,?,?,?,?,?)", rows)
    t0 = time.perf_counter()
    refs = store.statement_refs("k", "2026-09-10T00:00:00+00:00")
    assert len(refs) == 60 and set(refs.values()) == {"r5"} and time.perf_counter() - t0 < 0.25  # 原来约半秒


# ---------- 第四轮 ----------

class _SlowReply(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.end_headers()
        for _ in range(100):
            self.wfile.write(b" ")
            self.wfile.flush()
            time.sleep(0.02)

    def log_message(self, *a):
        pass


def test_model_calls_survive_keepalives_but_have_a_total_cap(monkeypatch):
    """DeepSeek 排队时发空行保持连接：空行不断就不算卡住；但总时限照样封顶。"""
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _SlowReply)  # 每 20 毫秒发一个空格，共两秒
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/chat/completions"
    try:
        data, err = llm._post(url, "k", {"m": 1}, 0.2)  # 0.2 秒是「多久没数据」：一直有空格，等到收完
        assert err == "bad json"  # 两秒的空格都收下了（不是在 0.2 秒时被掐断）；内容本身不是 JSON
        monkeypatch.setattr(llm, "LLM_TOTAL_SECONDS", 0.3)
        t0 = time.perf_counter()
        data, err = llm._post(url, "k", {"m": 1}, 0.2)
        assert data is None and err.startswith("limit") and time.perf_counter() - t0 < 1.5
    finally:
        srv.shutdown()


def test_slow_dns_counts_against_the_deadline(monkeypatch):
    import socket
    import urllib.request
    import limits
    _dns_idle()
    release = threading.Event()

    def fake_dns(*a, **k):
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    try:
        t0 = time.perf_counter()
        with pytest.raises(limits.ReadLimitError):
            limits.fetch(urllib.request.Request("http://example.invalid/"), timeout=0.08, max_bytes=1000)
        assert time.perf_counter() - t0 < QUICK
    finally:
        release.set()
        _dns_idle()


def test_prefixed_zip64_is_checked_before_parsing(monkeypatch):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=True) as z:
        for i in range(70_000):  # 超过 65535 条，写成 zip64
            z.writestr(f"d{i}/", b"")
    data = b"JUNK" * 100 + buf.getvalue()
    opened = []
    real = zipfile.ZipFile
    monkeypatch.setattr(zipfile, "ZipFile", lambda *a, **k: opened.append(1) or real(*a, **k))
    with pytest.raises(submission.SubmissionError):
        submission.read_zip(data)
    assert opened == []
    with pytest.raises(submission.SubmissionError):  # 有目录尾、zip64 记录对不上的，不交给 zipfile 猜
        submission._directory_size(data[:-22 - 20 - 56] + b"\0" * 56 + data[-22 - 20:])


def test_competition_map_counts_demand_with_a_set(monkeypatch):
    import json as _json
    import positioning
    monkeypatch.setattr(positioning, "_cutoff", lambda: "9999")
    old = "2026-09-01T00:00:00+00:00"
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)",
                      [(f"c{i}", f"u{i}", "llm-eval", "2310.17623", 1, _json.dumps({}), "pass", old) for i in range(10_000)])
        c.executemany("INSERT INTO statements VALUES(?,?,?,?,?,?,?)",
                      [(f"s{i}", f"u{i}", "llm-eval", 1, "multi-test" if i % 2 else "single-dup", _json.dumps({}), old) for i in range(10_000)])
    me = store.create_user("me")["uid"]
    t0 = time.perf_counter()
    rows = {r["id"]: r for r in positioning.competition_map(me, "llm-eval")["rows"]}
    assert rows["multi-test"]["demand"]["band"] == "热" and rows["shuffle"]["demand"]["band"] == "冷"
    assert time.perf_counter() - t0 < 3.0


def test_matrix_highlights_every_conflicting_row():
    import json as _json
    import reading
    now = "2026-10-03T00:00:00+00:00"
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)", [
            (f"c{i}", "u", "llm-eval", f"2402.{i:05d}", 1,
             _json.dumps({"arxiv_id": f"2402.{i:05d}", "title": f"P{i}", "version": 1, "status": "pass",
                          "dims": {"metric": "胜率", "direction": "up" if i < 15 else "down"}}), "pass", now)
            for i in range(30)] + [
            ("c99", "u", "llm-eval", "2402.00099", 1,
             _json.dumps({"arxiv_id": "2402.00099", "title": "无关", "version": 1, "status": "pass",
                          "dims": {"metric": "准确率", "direction": "up"}}), "pass", now)])
    m = reading.matrix("u", "llm-eval")
    assert sum(r["conflict"] for r in m["rows"]) == 30 and len(m["flags"]["conflicts"][0]["up"]) == reading.CONFLICT_SHOW


# ---------- 第五轮 ----------

def test_identical_course_searches_share_without_holding_threads(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "search_courses", lambda q, limit=5, term="": calls.append(q) or time.sleep(BLOCK) or {"ok": True, "items": []})

    async def go():
        async with _asgi() as c:
            reqs = [asyncio.create_task(c.get("/api/explore/courses?query=统计")) for _ in range(40)]
            await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            waited = time.perf_counter() - t0
            return [r.status_code for r in await asyncio.gather(*reqs)], health.status_code, waited

    codes, health, waited = asyncio.run(go())
    assert set(codes) == {200} and health == 200 and waited < QUICK and len(calls) == 1


def test_one_slow_host_cannot_fill_the_resolver_pool(monkeypatch):
    import socket
    import limits
    _dns_idle()
    real = socket.getaddrinfo

    def fake(host, *a, **k):
        if host == "slow.invalid":
            time.sleep(BLOCK)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    for _ in range(4):
        with pytest.raises(limits.ReadLimitError):
            limits._Watch(time.monotonic() + 0.05).resolve("slow.invalid", 80)
    assert limits._Watch(time.monotonic() + QUICK).resolve("fast.invalid", 80)  # 四次慢解析只占了一个线程
    monkeypatch.setattr(socket, "getaddrinfo", real)
    _dns_idle()


def test_zip_text_is_bounded_while_extracting():
    import os
    import tracemalloc
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", "# 题目\n")
        for i in range(4):
            z.writestr(f"notes{i}.txt", os.urandom(4_000_000))
    data = buf.getvalue()
    tracemalloc.start()
    bundle = submission.read_zip(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert all(len(t) <= submission.MAX_TEXT_CHARS for t in bundle["texts"].values())
    assert peak < 20 * 1024 * 1024  # 原来每个 4 MB 文件整份解压、整份解码再攒到最后截


def test_rarity_counts_pairs_from_peer_bitmasks(monkeypatch):
    import positioning
    me = store.create_user("me")["uid"]
    texts = [f"边{i:03d}" for i in range(120)]
    now = "2026-10-03T00:00:00+00:00"
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"m{i}", me, "background", t, "", "", now) for i, t in enumerate(texts)])
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)",
                      [(f"c{u}", f"u{u}", "llm-eval", "2310.17623", 1, "{}", "pass", now) for u in range(5000)])
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"p{u}-{j}", f"u{u}", "background", texts[(u + j * 7) % 120], "", "", now) for u in range(5000) for j in range(3)])
    t0 = time.perf_counter()
    r = positioning.combo_rarity(me, "llm-eval")
    assert time.perf_counter() - t0 < 2.0
    assert r["total"] == 120 * 119 // 2 and len(r["pairs"]) == positioning.RARITY_SHOW


def test_abstract_fallback_is_retried_after_transient_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path / "papers")
    monkeypatch.setattr(arxiv, "papers", lambda ids: [{"title": "T", "summary": "abstract only", "url": "u"}])
    html = "<html><body><article><p>" + "Full text sentence. " * 200 + "</p></article></body></html>"  # 真实页面以 </html> 结尾
    state = {"err": arxiv.ArxivError("连不上 arXiv")}

    def get(url, timeout=20, max_bytes=0):
        if state["err"]:
            raise state["err"]
        return html.encode()

    monkeypatch.setattr(arxiv, "_get", get)
    assert arxiv.fulltext("2310.17623")["source"] == "abstract"
    state["err"] = None
    assert arxiv.fulltext("2310.17623")["source"] == "abstract"  # 十分钟内不重复打
    monkeypatch.setattr(arxiv.time, "time", lambda: 10 ** 12)
    assert arxiv.fulltext("2310.17623")["source"] == "html"  # 到期后再试，拿到了正文
    state["err"] = arxiv.ArxivError("arXiv 返回 HTTP 404", 404)  # 确实没有 HTML 版：隔一周再试
    got = arxiv._fetch_fulltext("2401.00001")
    assert got["source"] == "abstract" and got["retry_after"] - arxiv.time.time() > 6 * 24 * 3600


# ---------- 第六轮 ----------

def test_expired_queued_dns_lookups_are_cancelled(monkeypatch):
    import socket
    import limits
    _dns_idle()

    def fake(host, *a, **k):
        if host != "fresh.invalid":
            time.sleep(BLOCK)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    for host in ["r1", "r2", "r3", "r4"]:  # 占满四个解析线程，已经开始跑，取消不了
        with pytest.raises(limits.ReadLimitError):
            limits._Watch(time.monotonic() + 0.02).resolve(host, 80)
    for host in ["q1", "q2", "q3", "q4"]:  # 排在队里，等的人超时走了：应当被取消
        with pytest.raises(limits.ReadLimitError):
            limits._Watch(time.monotonic() + 0.02).resolve(host, 80)
    # 正在跑的四个 BLOCK 秒后空出线程；没被取消的话，排队的四个还要再占 BLOCK 秒
    assert limits._Watch(time.monotonic() + 1.6 * BLOCK).resolve("fresh.invalid", 80)
    _dns_idle()


class _ShortBody(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"x" * 100)  # 声明 1000 字节，只给 100 就断开

    def log_message(self, *a):
        pass


def test_truncated_bodies_are_not_accepted_as_complete(tmp_path, monkeypatch):
    import urllib.request
    import limits
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ShortBody)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError) as exc:
            limits.fetch(urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/"), timeout=2, max_bytes=10_000)
        assert not isinstance(exc.value, limits.ReadLimitError)
    finally:
        srv.shutdown()
    # 没声明长度、提前断开的正文页：看页尾没有 </html>，当临时失败，只给摘要并很快重试
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path / "papers")
    monkeypatch.setattr(arxiv, "papers", lambda ids: [{"title": "T", "summary": "abstract", "url": "u"}])
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: b"<html><article>" + b"half " * 1000)
    got = arxiv.fulltext("2310.17623")
    assert got["source"] == "abstract" and got["retry_after"] - time.time() < arxiv.RETRY_TRANSIENT + 5


def test_expired_abstract_refresh_keeps_cache_and_backs_off(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2310.17623.json").write_text(_json.dumps(
        {"id": "2310.17623", "title": "T", "source": "abstract", "text": "cached abstract", "url": "u", "retry_after": 0}), encoding="utf-8")
    meta_calls, html_calls = [], []
    monkeypatch.setattr(arxiv, "papers", lambda ids: meta_calls.append(1) or (_ for _ in ()).throw(arxiv.ArxivError("连不上 arXiv")))

    def down(url, timeout=20, max_bytes=0):
        html_calls.append(1)
        raise arxiv.ArxivError("连不上 arXiv")

    monkeypatch.setattr(arxiv, "_get", down)
    first = arxiv.fulltext("2310.17623")
    second = arxiv.fulltext("2310.17623")
    assert first["text"] == second["text"] == "cached abstract"  # 没有因为刷新失败而报错
    assert meta_calls == [] and html_calls == [1]  # 不再取元数据；失败后十分钟内不重试
    assert first["retry_after"] - time.time() > arxiv.RETRY_TRANSIENT - 5


def test_rarity_stays_fast_with_dense_varied_histories():
    """真实的数据库路径：5000 人、每人 60 条和我重合的边、位图各不相同。"""
    import random
    import tracemalloc
    import positioning
    rng = random.Random(7)
    texts = [f"边{i:03d}" for i in range(120)]
    now = "2026-10-03T00:00:00+00:00"
    me = store.create_user("me")["uid"]
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"m{i}", me, "background", t, "", "", now) for i, t in enumerate(texts)])
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)",
                      [(f"c{u}", f"u{u}", "llm-eval", "2310.17623", 1, "{}", "pass", now) for u in range(5000)])
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"p{u}-{j}", f"u{u}", "background", t, "", "", now)
                       for u in range(5000) for j, t in enumerate(rng.sample(texts, 60))])
    t0 = time.perf_counter()
    r = positioning.combo_rarity(me, "llm-eval")
    took = time.perf_counter() - t0
    tracemalloc.start()
    positioning.combo_rarity(me, "llm-eval")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert r["total"] == 7140 and all(p["band"] == "常见" for p in r["pairs"])  # 每对约 1240 人都有
    assert took < 3.0 and peak < 30 * 1024 * 1024, (took, peak)  # 内存是主要的门槛：原来峰值一百七十多 MB


# ---------- 第七轮 ----------

def test_notebook_outputs_are_skipped_without_building_objects():
    import json as _json
    import random
    import tracemalloc
    rng = random.Random(3)

    def zipped(nb):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.md", "# 题目\n")
            z.writestr("analysis.ipynb", _json.dumps(nb, ensure_ascii=False))
        return buf.getvalue()

    # 结构超限（十五万个输出对象）：不解析，只提示清空输出；仍能看出有输出
    heavy = {"cells": [{"cell_type": "code", "source": ["print(x)"],
                        "outputs": [{"output_type": "execute_result", "data": {"v": rng.randrange(10 ** 6)}} for _ in range(150_000)]}]}
    data = zipped(heavy)
    tracemalloc.start()
    bundle = submission.read_zip(data)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    item = next(i for i in bundle["inventory"] if i["path"] == "analysis.ipynb")
    assert item["has_outputs"] is False and bundle["texts"]["analysis.ipynb"] == ""  # 没解析就不给「有输出」
    assert any("清空输出" in n for n in bundle["notes"]) and peak < 40 * 1024 * 1024
    # 限额以内：照常读源码，输出对象（哪怕带 source 键）解析完就丢
    ok = {"cells": [{"cell_type": "markdown", "source": ['# 说明 "引号" {[}]']},
                    {"cell_type": "code", "source": ["print(x)"], "outputs": [{"text": ["1"]}, {"source": ["不是单元"]}]}]}
    bundle = submission.read_zip(zipped(ok))
    assert bundle["texts"]["analysis.ipynb"] == '# 说明 "引号" {[}]\n\nprint(x)'
    assert submission._nb_object([("source", "x")]) == 1 and submission._nb_object([("cell_type", "code")]) == {"cell_type": "code"}
    small = _json.dumps({"cells": [{"cell_type": "code", "source": "a", "outputs": [{"output_type": "stream", "t": "}"}]}, {"cell_type": "markdown", "source": ["b", "c"]}]}).encode()
    assert submission._ipynb_text(small) == ("a\n\nbc", True)


def test_malformed_notebooks_are_rejected_quickly():
    t0 = time.perf_counter()
    assert submission._ipynb_text(b'{"cells":[{"cell_type":"code","source":"a","outputs":[{"t": nope}]}]}') == ("", False)  # 输出值不合法
    assert submission._ipynb_text(b'{"cells":[{"cell_type":"code","source":"a","outputs":[{"t": 1}]}]') == ("", False)  # 少了最后的花括号
    assert submission._ipynb_text(b'{"cells":[{"cell_type":"code","source":"a","outputs":[{"t":"' + b'\\"' * 40_000) == ("", False)  # 没闭合的字符串
    assert time.perf_counter() - t0 < 1.0


def test_concurrent_reviews_build_the_quote_index_once(monkeypatch):
    import quotes
    quotes.clear_prepared()
    calls = []
    real = quotes._index
    monkeypatch.setattr(quotes, "_index", lambda text: calls.append(1) or time.sleep(0.2) or real(text))
    text = "Limitations\n" + "word " * 200_000
    ts = [threading.Thread(target=quotes._prepared, args=(text,)) for _ in range(24)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert calls == [1]


# ---------- 第八轮 ----------

def test_concurrent_card_submissions_wait_for_the_index_off_the_workers(monkeypatch):
    import quotes
    quotes.clear_prepared()
    # 四十个不同的人同时交同一篇（同一个人连交四十张会被每人排队上限挡掉，那是 userlock 的事，见 test_same_user_races）
    uids = [store.create_user(f"t{i}")["uid"] for i in range(40)]
    paper = {"id": "2310.17623", "title": "T", "source": "html", "url": "u",
             "text": "Abstract\n" + "We study things. " * 20_000 + "\n6 Limitations\nIt is limited."}
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: paper)
    built = []
    real = quotes._index
    monkeypatch.setattr(quotes, "_index", lambda text: built.append(1) or time.sleep(BLOCK) or real(text))
    body = {"kit": "llm-eval", "arxiv_id": "2310.17623", "fields": {"claim_quote": "We study things. We study"}, "dims": {}}

    async def go():
        async with _asgi() as c:
            subs = [asyncio.create_task(c.post("/api/cards", json={**body, "uid": u})) for u in uids]
            await asyncio.sleep(0.1)
            t0 = time.perf_counter()
            health = await c.get("/api/health")
            waited = time.perf_counter() - t0
            return [r.status_code for r in await asyncio.gather(*subs)], health.status_code, waited

    codes, health, waited = asyncio.run(go())
    assert set(codes) == {200} and health == 200 and waited < QUICK and built == [1]


def test_rarity_memo_is_bounded_for_distinct_descriptions():
    import tracemalloc
    import positioning
    now = "2026-10-03T00:00:00+00:00"
    me = store.create_user("me")["uid"]
    store.add_edge(me, "language", "粤语母语")
    store.add_edge(me, "course", "修过《数理统计》")
    with sqlite3.connect(store.DB_PATH) as c:
        c.executemany("INSERT INTO cards VALUES(?,?,?,?,?,?,?,?)",
                      [(f"c{u}", f"u{u}", "llm-eval", "2310.17623", 1, "{}", "pass", now) for u in range(5000)])
        c.executemany("INSERT INTO edges(id, user_id, kind, text, evidence_url, ref, created_at) VALUES(?,?,?,?,?,?,?)",
                      [(f"p{u}-{j}", f"u{u}", "background", f"同学{u}的第{j}条经历：在某个具体的地方做过某件具体的事", "", "", now)
                       for u in range(5000) for j in range(60)])
    tracemalloc.start()
    r = positioning.combo_rarity(me, "llm-eval")
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert r["status"] == "ok" and peak < 15 * 1024 * 1024, peak  # 三十万条各不相同的描述不再全记在缓存里


# ---------- 自查修复 ----------

def test_corrupt_paper_cache_is_refetched_and_writes_are_atomic(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2310.17623.json").write_text('{"id": "2310.17623", "title": "T", "sou', encoding="utf-8")  # 写到一半被杀
    monkeypatch.setattr(arxiv, "papers", lambda ids: [{"title": "T", "summary": "abstract", "url": "u"}])
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: (_ for _ in ()).throw(arxiv.ArxivError("HTTP 404", 404)))
    got = arxiv.fulltext("2310.17623")  # 坏文件当作没有缓存，重新取一次并覆盖
    assert got["text"] == "abstract" and not list(tmp_path.glob(".*.tmp"))
    assert arxiv.fulltext("2310.17623") is got  # 刚读过的直接从内存给，不再读盘解析


def test_arxiv_waits_do_not_starve_course_searches(monkeypatch):
    monkeypatch.setattr(arxiv, "fulltext", lambda aid: time.sleep(BLOCK) or {"id": aid, "title": "T", "source": "abstract", "text": "x", "url": "u"})
    monkeypatch.setattr(main, "search_courses", lambda q, limit=5, term="": {"ok": True, "items": []})

    async def go():
        async with _asgi() as c:
            papers = [asyncio.create_task(c.get(f"/api/papers/2401.{i:05d}")) for i in range(12)]  # 十二篇不同的论文
            await asyncio.sleep(0.1)
            t0 = time.perf_counter()
            r = await c.get("/api/explore/courses?query=统计")
            waited = time.perf_counter() - t0
            await asyncio.gather(*papers)
            return r.status_code, waited

    code, waited = asyncio.run(go())
    assert code == 200 and waited < QUICK  # 课程检索有自己的线程池，不排在等 arXiv 的后面


def test_slow_but_steady_uploads_finish_and_total_is_capped(monkeypatch):
    monkeypatch.setattr(main, "_project_in_portrait_or_404", lambda uid, pid: ({"id": pid}, ""))
    monkeypatch.setattr(main, "UPLOAD_IDLE_SECONDS", 0.3)
    monkeypatch.setattr(submission, "review", lambda data, p: {"passed": 1})
    monkeypatch.setattr(projects, "record_review", lambda uid, p, r: None)

    async def steady(n):
        for _ in range(n):
            await asyncio.sleep(0.1)  # 慢但一直在传：每次间隔都比空闲时限短
            yield b"PK" * 10

    async def go():
        async with _asgi() as c:
            ok = await c.post("/api/projects/p/submit?uid=u", content=steady(10))  # 一秒，远超 0.3 秒的空闲时限
            monkeypatch.setattr(main, "UPLOAD_TOTAL_SECONDS", 0.5)
            capped = await c.post("/api/projects/p/submit?uid=u", content=steady(20))
            return ok.status_code, capped.status_code

    assert asyncio.run(go()) == (200, 408)


def test_joined_source_job_uses_the_latest_deadline(monkeypatch):
    live = []
    monkeypatch.setattr(project_adapters, "ADAPTERS", {"fast": lambda src, d, s, t: live.append(1) or [{"title": "神经网络", "url": "https://x/1"}]})
    src = {"id": "fast-src", "name": "快", "directions": ["ai"], "adapter": "fast", "samples": [{"title": "快照", "url": "https://x/s"}]}
    gate = threading.Event()
    blockers = [projects._SOURCE_POOL.submit(gate.wait) for _ in range(projects.SOURCE_WORKERS)]  # 先占满来源线程
    first = projects._source_job(src, "ai", 1, "join-test", time.time() + 0.1)  # 最早的检索：0.1 秒后截止
    later = projects._source_job(src, "ai", 1, "join-test", time.time() + 30)  # 后来加入的：还有很久
    assert first is later
    time.sleep(0.3)
    gate.set()
    got, status = later.result(timeout=5)
    assert status.get("live") and live == [1]  # 按最晚的截止时间算，照样实时检索，不给「排队超时」的快照
    [b.result() for b in blockers]


def test_prepared_quote_indexes_survive_several_concurrent_papers():
    import quotes
    quotes.clear_prepared()
    texts = [f"Paper {i}\n" + f"sentence number {i} " * 20_000 for i in range(6)]
    for t in texts:
        quotes.prepare(t)
    assert all(t in quotes._PREPARED for t in texts)  # 原来只留 4 篇，前两篇在评阅前就被挤掉


def test_one_watchdog_thread_serves_all_requests(trickle):
    import urllib.request
    import limits
    before = threading.active_count()
    for _ in range(5):
        with pytest.raises(limits.ReadLimitError):
            limits.fetch(urllib.request.Request(trickle + "/slow"), timeout=0.1, max_bytes=10_000)
    names = [t.name for t in threading.enumerate()]
    assert names.count("fetch-watchdog") == 1 and threading.active_count() <= before + 1  # 不再每个请求一个 Timer 线程


def test_private_http_hooks_still_exist(trickle):
    """limits 依赖 CPython http.client 的两处内部细节；Python 升级改了它们，这里会先失败，而不是总时限悄悄失效。"""
    import http.client
    import urllib.request
    import limits
    assert hasattr(http.client.HTTPConnection("example.invalid"), "_create_connection")
    with urllib.request.urlopen(trickle + "/big", timeout=5) as resp:
        assert limits._socket_of(resp) is not None


def test_large_zip_directory_gets_its_own_message():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(20):
            z.writestr("深" * 20_000 + f"{i}.txt", b"x")  # 条目很少，但每个文件名很长
    with pytest.raises(submission.SubmissionError) as exc:
        submission.read_zip(buf.getvalue())
    assert "文件名" in str(exc.value) and "条目太多" not in str(exc.value)


def test_quote_sections_use_original_offsets():
    import quotes
    quotes.clear_prepared()
    # 引言里有很多连字：整篇先做 NFKC 再数位置会整体错开，把引言的句子报成局限段
    text = "Abstract\nshort.\n1 Introduction\n" + "ﬁ" * 400 + " the intro sentence that we quote here.\n" + "x" * 50 + "\n6 Limitations\nThe real limitation sentence is here.\n"
    paper = {"text": text, "source": "html"}
    assert quotes.locate("the intro sentence that we quote here", paper)["section"] == "introduction"
    assert quotes.locate("The real limitation sentence is here", paper)["section"] == "limitations"
    assert quotes.locate("fififi the intro sentence", paper)["found"]  # 连字照样能对上


def test_last_search_results_expire_and_are_bounded(monkeypatch):
    from limits import TTLCache as _T
    clock = [100.0]
    monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(projects, "_LAST", _T(0.05, 100))
    for i in range(1000):
        projects._LAST.set(f"u{i}", {"c": {"id": "c"}})
    assert len(projects._LAST) == 100  # 有条数上限
    clock[0] += 0.06
    projects._LAST.set("someone", {"c": {"id": "c"}})
    assert len(projects._LAST) == 1  # 过期的随下一次写入一起清掉
    with pytest.raises(KeyError):
        projects.pick("u1", "c")


# ---------- 第十轮 ----------

def test_cached_papers_do_not_queue_behind_slow_fetches(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2310.17623.json").write_text(_json.dumps(
        {"id": "2310.17623", "title": "T", "source": "html", "text": "Abstract\\nx", "url": "u", "tables": []}), encoding="utf-8")  # 带 tables：没有的旧缓存会被重取（上游 w74）
    gate, started, mu, n = threading.Event(), threading.Event(), threading.Lock(), [0]

    def html(*a, **k):  # 没缓存的论文：卡在取正文 HTML 上
        with mu:
            n[0] += 1
            if n[0] >= 4:
                started.set()
        assert gate.wait(5)
        return ("<html><article><p>" + "body " * 500 + "</p></article></html>").encode()

    monkeypatch.setattr(arxiv, "_get", html)

    async def go():
        async with _asgi() as c:
            slow = [asyncio.create_task(c.get(f"/api/papers/2401.{i:05d}")) for i in range(6)]  # 占满 arXiv 线程池
            try:
                assert await asyncio.to_thread(started.wait, 5)
                t0 = time.perf_counter()
                hit = await c.get("/api/papers/2310.17623")
                waited = time.perf_counter() - t0
            finally:
                gate.set()
                await asyncio.gather(*slow)
            return hit.status_code, hit.json()["title"], waited

    code, title, waited = asyncio.run(go())
    assert code == 200 and title == "T" and waited < QUICK


def test_unparsed_notebooks_get_no_output_credit():
    import json as _json
    nb = {"cells": [], "metadata": {"pad": [{"outputs": [1]}] + [{"k": i} for i in range(80_000)]}}  # 零个单元，只靠元数据撑过限额
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.md", "# 题目\\n")
        z.writestr("a.ipynb", _json.dumps(nb))
    bundle = submission.read_zip(buf.getvalue())
    item = next(i for i in bundle["inventory"] if i["path"] == "a.ipynb")
    assert item["has_outputs"] is False and any("不计入" in n for n in bundle["notes"])


def test_quote_normalization_is_consistent_with_expanding_and_composed_characters():
    import unicodedata
    import quotes
    quotes.clear_prepared()
    text = "Abstract\nshort.\n1 Introduction\n" + "\u0130" * 70 + " the intro sentence that we quote here.\n" + "x" * 30 + "\n6 Limitations\nThe real limitation sentence is here.\n"
    paper = {"text": text, "source": "html"}
    assert quotes.locate("the intro sentence that we quote here", paper)["section"] == "introduction"  # İ 转小写是两个字
    han = "한국어 문장을 인용합니다 정말로"
    assert quotes.locate(han, {"text": "1 Introduction\n" + unicodedata.normalize("NFD", han), "source": "html"})["found"]
    assert quotes.locate(unicodedata.normalize("NFD", han), {"text": "1 Introduction\n" + han, "source": "html"})["found"]
