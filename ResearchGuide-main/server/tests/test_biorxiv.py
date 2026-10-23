# -*- coding: utf-8 -*-
"""biorxiv/medrxiv 模块：id 清洗、details 解析、JATS 抽取、翻页过滤、全文退路，加端点/收集/预热接线。全离线。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import paper_collect  # noqa: E402
import reading  # noqa: E402

import biorxiv  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "biorxiv.db")
    store.init_db()


def _user(name="bio"):
    row = store.create_user(name)
    token = store.create_session(row["uid"], 3600)
    return row["uid"], {"Authorization": f"Bearer {token}"}


def test_clean_id_variants():
    assert biorxiv.clean_id("biorxiv:10.1101.2024.12.06.627082") == "biorxiv:10.1101.2024.12.06.627082"
    assert biorxiv.clean_id("10.1101/2024.12.06.627082") == "biorxiv:10.1101.2024.12.06.627082"
    assert biorxiv.clean_id("doi:10.64898/2026.09.28.755022") == "biorxiv:10.64898.2026.09.28.755022"
    assert biorxiv.clean_id("https://www.biorxiv.org/content/10.1101/2024.12.06.627082v3") == "biorxiv:10.1101.2024.12.06.627082"
    assert biorxiv.clean_id("https://www.biorxiv.org/content/10.1101/2024.12.06.627082v2.full.pdf") == "biorxiv:10.1101.2024.12.06.627082"
    assert biorxiv.clean_id("medrxiv:10.1101/2024.01.01.123456v2") == "medrxiv:10.1101.2024.01.01.123456"
    assert biorxiv.clean_id("biorxiv:10.64898/2026.09.28.755022") == "biorxiv:10.64898.2026.09.28.755022"
    assert biorxiv.is_id("biorxiv:10.64898.2026.09.28.755022")
    assert biorxiv.is_id("medrxiv:10.1101.2024.01.01.123456")
    assert not biorxiv.is_id("2310.17623")
    assert not biorxiv.is_id("psyarxiv:abcde")
    with pytest.raises(biorxiv.BiorxivError) as exc:
        biorxiv.clean_id("2310.17623")
    assert exc.value.status == 400


def _entry(doi, version="1", title="A study", abstract="An immunology study of mice.",
           date="2026-10-01", category="neuroscience",
           jats="https://www.biorxiv.org/content/early/2026/10/01/x.source.xml"):
    return {"title": title, "authors": "A, B.; C, D.", "doi": doi, "date": date,
            "version": version, "type": "new results", "license": "cc_by",
            "category": category, "jatsxml": jats, "abstract": abstract,
            "published": "NA", "server": "bioRxiv"}


def test_parse_details_and_latest():
    payload = {"messages": [{"status": "ok", "total": "3"}],
               "collection": [_entry("10.1101/2024.12.06.627082", "1", title="Good fire v1"),
                              _entry("10.1101/2024.12.06.627082", "3", title="Good fire"),
                              _entry("10.64898/2026.09.28.755022", "1", title="LIFG")]}
    rows = biorxiv.parse_details(payload, "biorxiv")
    assert len(rows) == 3
    top = biorxiv._latest(rows)
    assert len(top) == 2
    by_id = {p["id"]: p for p in top}
    assert by_id["biorxiv:10.1101.2024.12.06.627082"]["title"] == "Good fire"
    assert by_id["biorxiv:10.1101.2024.12.06.627082"]["version"] == "3"
    p = by_id["biorxiv:10.64898.2026.09.28.755022"]
    assert p["url"] == "https://www.biorxiv.org/content/10.64898/2026.09.28.755022v1"
    assert p["authors"] == ["A, B.", "C, D."]
    assert p["categories"] == ["neuroscience"]
    assert p["source"] == "biorxiv"
    with pytest.raises(biorxiv.BiorxivError):
        biorxiv.parse_details(
            {"messages": [{"status": "Both dates must be in yyyy-mm-dd format"}], "collection": []},
            "biorxiv")


def _jats(body_extra=""):
    long_p = "<p>" + "Fire ecology shapes western forests in measurable ways. " * 60 + "</p>"
    return f"""<?xml version="1.0"?>
<article xmlns:hwp="http://schema.highwire.org/Journal">
 <front><abstract><title>Abstract</title><p>Sum text.</p></abstract></front>
 <body>
  <sec><title>Introduction</title><p>Fire is good sometimes. <xref>1</xref></p>
   <sec><title>Data</title><p>We used remote sensing.</p></sec>
  </sec>
  <sec><title>Results</title>
   <p>See table below.</p>
   <table-wrap id="tw1"><caption><title>Area burned by severity</title></caption>
    <table><thead><tr><th>Severity</th><th>Hectares</th></tr></thead>
     <tbody><tr><td>low</td><td>120</td></tr><tr><td>high</td><td>80</td></tr></tbody>
    </table></table-wrap>
   <fig><caption><title>Map of fires</title></caption></fig>
   {long_p}
   {body_extra}
  </sec>
 </body>
 <back><ref-list><ref><p>skip me</p></ref></ref-list></back>
</article>
"""


def test_parse_jats():
    got = biorxiv.parse_jats(_jats())
    text = got["text"]
    lines = text.splitlines()
    assert "Introduction" in lines
    assert "Data" in lines
    assert "We used remote sensing." in text
    assert "[[table:t1]]" in text
    assert "skip me" not in text
    assert "Map of fires" in text
    assert len(got["tables"]) == 1
    t = got["tables"][0]
    assert t["caption"] == "Area burned by severity"
    assert t["headers"] == ["Severity", "Hectares"]
    assert t["rows"] == [["low", "120"], ["high", "80"]]
    assert t["at"] == text.find("[[table:t1]]")
    with pytest.raises(biorxiv.BiorxivError):
        biorxiv.parse_jats("<html><body>error code: 1015</body></html>")


def test_recent_filters_pages_and_dedupes(monkeypatch):
    seen = {"urls": []}

    def fake_api(url):
        seen["urls"].append(url)
        if url.endswith("/0/json") or "/0/json?" in url:
            col = [_entry("10.1101/2026.10.01.000001", abstract="tumor immunology work"),
                   _entry("10.1101/2026.10.01.000002", abstract="nothing relevant here"),
                   _entry("10.1101/2026.10.01.000001", version="2", abstract="tumor immunology v2")]
        elif url.endswith("/30/json") or "/30/json?" in url:
            col = [_entry(f"10.1101/2026.10.02.{i:06d}", abstract=f"immunology paper {i}") for i in range(30)]
        else:
            col = []
        return json.dumps({"messages": [{"status": "ok", "total": "45"}], "collection": col}).encode()

    monkeypatch.setattr(biorxiv, "_api", fake_api)
    got = biorxiv.recent("biorxiv", "neuroscience", ["immunology"], max_results=5, days=7)
    assert len(got) == 5
    assert all("immunology" in (p["title"] + " " + p["summary"]).lower() for p in got)
    ids = [p["id"] for p in got]
    assert len(set(ids)) == len(ids)
    assert "biorxiv:10.1101.2026.10.01.000001" in ids
    assert "biorxiv:10.1101.2026.10.01.000002" not in ids
    assert "category=neuroscience" in seen["urls"][0]
    # 没有关键词就只按分类收
    seen["urls"].clear()

    def fake_api2(url):
        seen["urls"].append(url)
        return json.dumps({"messages": [{"status": "ok", "total": "1"}],
                           "collection": [_entry("10.1101/2026.10.01.000009")]}).encode()

    monkeypatch.setattr(biorxiv, "_api", fake_api2)
    got = biorxiv.recent("biorxiv", "immunology", [], max_results=10, days=7)
    assert [p["id"] for p in got] == ["biorxiv:10.1101.2026.10.01.000009"]
    assert "category=immunology" in seen["urls"][0]
    assert biorxiv.recent("biorxiv", "immunology", [], cached_only=True) == []


AID = "biorxiv:10.1101.2024.12.06.627082"


def _rows_of(*entries):
    """把 API 原始条目转成 _rows 的返回形状（解析后的行）。"""
    return biorxiv.parse_details({"messages": [{"status": "ok"}], "collection": list(entries)}, "biorxiv")


def test_fulltext_html_and_cached_roundtrip(monkeypatch):
    monkeypatch.setattr(biorxiv, "_rows", lambda pid: _rows_of(_entry("10.1101/2024.12.06.627082", "1", jats="JATS_URL")))
    monkeypatch.setattr(biorxiv, "_web", lambda url: _jats().encode())
    got = biorxiv.fulltext(AID)
    assert got["source"] == "html"
    assert got["tables"]
    row = store.get_paper_text(AID)
    assert row and row["source"] == "html"

    def no_net(url):
        raise AssertionError("cached roundtrip should not hit the network")

    monkeypatch.setattr(biorxiv, "_web", no_net)
    monkeypatch.setattr(biorxiv, "_rows", no_net)
    got2 = biorxiv.fulltext(AID)
    assert got2["source"] == "html"


def test_fulltext_rate_limited_falls_back_to_abstract(monkeypatch):
    monkeypatch.setattr(biorxiv, "_rows", lambda pid: _rows_of(_entry("10.1101/2024.12.06.627082", "1", jats="JATS_URL")))

    def boom(url):
        raise biorxiv.BiorxivError("bioRxiv 返回 HTTP 429", 429)

    monkeypatch.setattr(biorxiv, "_web", boom)
    got = biorxiv.fulltext(AID)
    assert got["source"] == "abstract"
    assert got["retry_after"] > time.time() + biorxiv.RETRY_TRANSIENT - 60
    # 没到重试时间：直接复用库里的摘要，不打网
    monkeypatch.setattr(biorxiv, "_rows", lambda pid: (_ for _ in ()).throw(AssertionError("no refetch yet")))
    got2 = biorxiv.fulltext(AID)
    assert got2["source"] == "abstract"
    # 过了重试时间会重新取
    row = store.get_paper_text(AID)
    store.put_paper_text(AID, {**row, "retry_after": time.time() - 1})
    monkeypatch.setattr(biorxiv, "_rows", lambda pid: _rows_of(_entry("10.1101/2024.12.06.627082", "1", jats="JATS_URL")))
    monkeypatch.setattr(biorxiv, "_web", lambda url: _jats().encode())
    got3 = biorxiv.fulltext(AID)
    assert got3["source"] == "html"


def test_fulltext_404_tries_earliest_version(monkeypatch):
    calls = {"n": 0}

    def rows(pid):
        return _rows_of(_entry("10.1101/2024.12.06.627082", "1", jats="https://x/v1.source.xml"),
                        _entry("10.1101/2024.12.06.627082", "3", jats="https://x/v3.source.xml"))

    def web(url):
        calls["n"] += 1
        if "v3" in url:
            raise biorxiv.BiorxivError("bioRxiv 返回 HTTP 404", 404)
        return _jats().encode()

    monkeypatch.setattr(biorxiv, "_rows", rows)
    monkeypatch.setattr(biorxiv, "_web", web)
    got = biorxiv.fulltext(AID)
    assert got["source"] == "html"
    assert calls["n"] == 2  # 最新版 404 后退回最早版

    # 两个版本都 404：退摘要，长重试
    def all_404(url):
        calls["n"] += 1
        raise biorxiv.BiorxivError("bioRxiv 返回 HTTP 404", 404)

    monkeypatch.setattr(biorxiv, "_web", all_404)
    got = biorxiv.fulltext(AID + "2")
    assert got["source"] == "abstract"
    assert got["retry_after"] > time.time() + biorxiv.RETRY_NO_XML - 3600


def test_fulltext_paper_not_found(monkeypatch):
    monkeypatch.setattr(biorxiv, "_rows", lambda pid: [])
    with pytest.raises(biorxiv.BiorxivError) as exc:
        biorxiv.fulltext(AID)
    assert exc.value.status == 404


# ---------- 接线：话题今日、端点打开、收集、预热 ----------

def _paper_dict(i):
    return {"id": f"biorxiv:10.1101.2026.10.01.{i:06d}", "version": "1",
            "title": f"Immunology paper {i}", "summary": "about immunology",
            "authors": ["A, B."], "published": "2026-10-01", "url": "https://www.biorxiv.org/x",
            "categories": ["immunology"], "source": "biorxiv",
            "jats": "https://www.biorxiv.org/content/early/2026/10/01/x.source.xml"}


def test_topic_source_biorxiv_writes_pool(monkeypatch):
    monkeypatch.setattr(biorxiv, "recent", lambda *a, **k: [_paper_dict(1), _paper_dict(2)])
    papers, err, stale = reading.topic_source("bio-immuno")
    assert err == ""
    assert not stale
    assert [p["id"] for p in papers] == ["biorxiv:10.1101.2026.10.01.000001",
                                         "biorxiv:10.1101.2026.10.01.000002"]
    pool = store.get_paper_pool("bio-immuno", reading.arxiv_day())
    assert pool and len(pool["papers"]) == 2


def test_paper_opens_from_db_without_network(monkeypatch):
    uid, h = _user("biou")

    def boom(*a, **k):
        raise AssertionError("db 里有全文就不该打网")

    monkeypatch.setattr(biorxiv, "fulltext", boom)
    monkeypatch.setattr(biorxiv, "paper", boom)
    store.put_paper_text(AID, {"id": AID, "title": "Good fire", "source": "html",
                               "text": "Introduction\n" + "Fire ecology. " * 300,
                               "url": "https://www.biorxiv.org/x", "tables": []})
    r = TestClient(main.app).get(f"/api/papers/{AID}", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "html"
    assert body["lib_at"] == "db"
    assert any(s["name"] == "introduction" for s in body["sections"])


def test_paper_live_fetch_and_error_mapping(monkeypatch):
    uid, h = _user("biou2")
    monkeypatch.setattr(biorxiv, "fulltext", lambda pid: {
        "id": pid, "title": "Good fire", "source": "html",
        "text": "Introduction\n" + "Fire ecology. " * 300,
        "url": "https://www.biorxiv.org/x", "tables": []})
    r = TestClient(main.app).get(f"/api/papers/{AID}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "html"

    def not_found(pid):
        raise biorxiv.BiorxivError(f"bioRxiv 上没有 {pid}", 404)

    monkeypatch.setattr(biorxiv, "fulltext", not_found)
    other = "biorxiv:10.1101.2026.10.01.000009"
    r2 = TestClient(main.app).get(f"/api/papers/{other}", headers=h)
    assert r2.status_code == 404


def test_collect_node_fills_biorxiv_dir(monkeypatch):
    monkeypatch.setattr("arxiv.recent", lambda *a, **k: [])  # q-bio 话题返回空，逼出 bio 话题分支
    monkeypatch.setattr(biorxiv, "recent",
                        lambda server, category, keywords, max_results=25, **k:
                        [_paper_dict(i) for i in range(min(max_results, 10))])
    rec = paper_collect.collect_node("310.34", quota=8, fulltext=0, xlate=False,
                                     metadata_only=True)
    assert rec["dir_n"] >= 8, rec
    rows = store.list_paper_dir("310.34")
    assert any(r["source"] == "biorxiv" for r in rows)
    assert all(r["paper_id"].startswith("biorxiv:") for r in rows if r["source"] == "biorxiv")


def test_warm_fetch_dispatches_by_prefix(monkeypatch):
    calls = []

    def mk(name, result):
        def fn(*a, **k):
            calls.append(name)
            return result
        return fn

    monkeypatch.setattr("osf.fulltext", mk("osf", {"source": "abstract", "text": "x"}))
    monkeypatch.setattr("biorxiv.fulltext", mk("biorxiv", {"source": "html", "text": "y"}))
    monkeypatch.setattr("arxiv.fulltext", mk("arxiv", {"source": "html", "text": "z"}))
    reading.warm_fetch("psyarxiv:abcde")
    reading.warm_fetch(AID)
    reading.warm_fetch("2310.17623")
    assert calls == ["osf", "biorxiv", "arxiv"]


# ---------- source 过滤：服务器上强制铺 biorxiv 目录用的开关 ----------

def test_collect_source_biorxiv_never_hits_arxiv(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("source=biorxiv 不该打 arXiv")

    monkeypatch.setattr("arxiv.recent", boom)
    monkeypatch.setattr(biorxiv, "recent",
                        lambda *a, **k: [_paper_dict(i) for i in range(6)])
    rec = paper_collect.collect_node("310.34", quota=8, fulltext=0, xlate=False,
                                     metadata_only=True, source="biorxiv")
    rows = store.list_paper_dir("310.34")
    assert rows and all(r["source"] == "biorxiv" for r in rows)
    assert rec["dir_n"] == len(rows)  # source 过滤下 dir_n 只数该来源
    assert rec["dir_n"] >= 6
    assert any("不足" in r for r in rec["reasons"])  # 6 < 8，缺口如实记


def test_collect_without_source_uses_both_topics(monkeypatch):
    calls = {"arxiv": 0, "biorxiv": 0}

    def fake_arxiv(categories, keywords, max_results=40, **kw):
        calls["arxiv"] += 1
        return [{"id": "2310.00001", "title": "Quantitative immunology", "summary": "s",
                 "authors": [], "published": "2024-01-01", "url": "u", "categories": [],
                 "source": "arxiv"}]

    def fake_bio(*a, **k):
        calls["biorxiv"] += 1
        return [_paper_dict(i) for i in range(3)]

    monkeypatch.setattr("arxiv.recent", fake_arxiv)
    monkeypatch.setattr(biorxiv, "recent", fake_bio)
    paper_collect.collect_node("310.34", quota=8, fulltext=0, xlate=False, metadata_only=True)
    assert calls["arxiv"] >= 1 and calls["biorxiv"] >= 1
    sources = {r["source"] for r in store.list_paper_dir("310.34")}
    assert sources == {"arxiv", "biorxiv"}


def test_admin_collect_endpoint_passes_source(monkeypatch):
    token = "t" * 32
    monkeypatch.setenv("ADMIN_TOKEN", token)
    seen = {}

    def fake_collect(**kw):
        seen.update(kw)
        return {"ok": True, "nodes": [], "kit_pinned": 0, "ephemeral": False, "next_node": ""}

    monkeypatch.setattr("paper_collect.collect", fake_collect)
    c = TestClient(main.app)
    r = c.post("/api/admin/papers/collect?node=310&source=biorxiv",
               headers={"X-Admin-Token": token})
    assert r.status_code == 200, r.text
    assert seen.get("source") == "biorxiv" and seen.get("node") == "310"
    r2 = c.post("/api/admin/papers/collect?source=ssrn", headers={"X-Admin-Token": token})
    assert r2.status_code == 400
