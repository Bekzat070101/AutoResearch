# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402
import llm  # noqa: E402
import main  # noqa: E402
import reading  # noqa: E402
import store  # noqa: E402
import tutor  # noqa: E402

pytestmark = pytest.mark.real_auth
ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "lib.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    monkeypatch.setattr(llm, "ready", lambda uid=None: False)
    monkeypatch.setenv("ADMIN_TOKEN", "a" * 32)
    monkeypatch.setenv("ARXIV_CACHE_MAX_BYTES", "200")


@pytest.fixture
def client():
    return TestClient(main.app)


def _user(name="u"):
    row = store.create_user(name)
    token = store.create_session(row["uid"], 3600)
    return row["uid"], {"Authorization": f"Bearer {token}"}


def _extract(data, deadline=None):
    text = ("Imported PDF paragraph about methods and results. " * 80)
    return {"text": text, "pages": 2, "title": "Local PDF"}


def test_paper_text_roundtrip_and_quota_keeps_pinned():
    store.put_paper_text("2009.03300", {"title": "pin", "source": "html", "text": "x" * 80, "url": "u"}, pinned=True)
    store.put_paper_text("2310.17623", {"title": "old", "source": "html", "text": "y" * 80, "url": "u"})
    store.put_paper_text("2401.00001", {"title": "new", "source": "html", "text": "z" * 80, "url": "u"})
    assert store.get_paper_text("2009.03300") is not None
    assert store.get_paper_text("2310.17623") is None
    assert store.get_paper_text("2401.00001") is not None
    store.pin_papers(["2401.00001"])
    store.put_paper_text("2402.00001", {"title": "later", "source": "html", "text": "w" * 80, "url": "u"})
    assert store.get_paper_text("2009.03300") is not None
    assert store.get_paper_text("2401.00001") is not None


def test_user_papers_deleted_and_exported_with_account():
    uid, _ = _user("own")
    store.put_user_paper(uid, "local:aaaaaaaaaaaa", "sha", "T", "a.pdf", "text " * 500, 3)
    exp = store.export_user(uid)
    assert exp["user_papers"]
    store.delete_user(uid)
    assert store.get_user_paper(uid, "local:aaaaaaaaaaaa") is None
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM user_papers WHERE user_id=?", (uid,)).fetchone()[0]
    assert n == 0


def test_upload_is_private_to_owner(client, monkeypatch):
    monkeypatch.setattr("pdf.extract_pdf", _extract)
    a, ha = _user("a")
    b, hb = _user("b")
    r = client.post(f"/api/papers/upload?uid={a}", content=b"%PDF-1.4 fake",
                    headers={**ha, "Content-Type": "application/pdf", "X-Filename": "mine.pdf"})
    assert r.status_code == 200, r.text
    pid = r.json()["pid"]
    assert pid.startswith("local:")
    assert client.get(f"/api/papers/{pid}", headers=ha).status_code == 200
    assert client.get(f"/api/papers/{pid}", headers=hb).status_code == 404
    exp = store.export_user(a)
    assert any(row["pid"] == pid for row in exp["user_papers"])
    store.delete_user(a)
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM user_papers WHERE user_id=?", (a,)).fetchone()[0]
    assert n == 0


def test_upload_limits(client, monkeypatch):
    monkeypatch.setattr("pdf.extract_pdf", _extract)
    uid, h = _user("lim")
    big = client.post(f"/api/papers/upload?uid={uid}", content=b"%PDF-" + b"x" * (10 << 20),
                      headers={**h, "Content-Type": "application/pdf"})
    assert big.status_code == 413
    def no_text(*a, **k):
        raise __import__("pdf").PdfError("没有文字层（扫描件）")
    monkeypatch.setattr("pdf.extract_pdf", no_text)
    empty = client.post(f"/api/papers/upload?uid={uid}", content=b"%PDF-1.4 xx",
                        headers={**h, "Content-Type": "application/pdf"})
    assert empty.status_code == 400 and "文字层" in empty.text
    def enc(*a, **k):
        raise __import__("pdf").PdfError("这份 PDF 要密码")
    monkeypatch.setattr("pdf.extract_pdf", enc)
    locked = client.post(f"/api/papers/upload?uid={uid}", content=b"%PDF-1.4 xx",
                         headers={**h, "Content-Type": "application/pdf"})
    assert locked.status_code == 400 and "密码" in locked.text
    monkeypatch.setattr("pdf.extract_pdf", _extract)
    codes = []
    for i in range(11):
        codes.append(client.post(f"/api/papers/upload?uid={uid}", content=b"%PDF-1.4 xx",
                                 headers={**h, "Content-Type": "application/pdf"}).status_code)
    assert 429 in codes


def test_search_route_not_shadowed(client, monkeypatch):
    uid, h = _user("s")
    monkeypatch.setattr(arxiv, "search", lambda q, max_results=20: [{"id": "2009.03300", "title": "MMLU"}])
    r = client.get(f"/api/papers/search?q=sparse+attention&uid={uid}", headers=h)
    assert r.status_code == 200
    assert r.json()["items"][0]["id"] == "2009.03300"
    assert "card_kit" in r.json()["items"][0]


def test_search_rejects_chinese_and_cleans_terms(client):
    uid, h = _user("zh")
    r = client.get(f"/api/papers/search?q=稀疏注意力&uid={uid}", headers=h)
    assert r.status_code == 400
    assert "英文" in r.text
    with pytest.raises(arxiv.ArxivError) as exc:
        arxiv.search("稀疏注意力")
    assert exc.value.status == 400


def test_search_cache_separate_from_meta(monkeypatch):
    calls = []
    def query(params):
        calls.append(params)
        return [{"id": "2009.03300", "title": "MMLU"}]
    monkeypatch.setattr(arxiv, "query", query)
    first = arxiv.search("sparse attention")
    second = arxiv.search("sparse attention")
    assert first and second
    assert len(calls) == 1
    assert "search_query" in calls[0] and "all:sparse" in calls[0]["search_query"]
    assert calls[0]["sortBy"] == "relevance"


def test_pdf_without_headings_splits_into_parts():
    text = ("Plain paragraph without a numbered heading. " * 80)
    sts = tutor.stations({"id": "local:aaaaaaaaaaaa", "source": "pdf", "text": text})
    assert [s["label"] for s in sts] == ["第 1 部分", "第 2 部分", "第 3 部分", "第 4 部分"]


def test_pdf_strict_sections():
    text = "\n".join([
        "1. Introduction",
        "This is the intro " + ("word " * 20),
        "a folded line that is not a heading because it is far too long to be a real section title in a pdf extract",
        "2. Methods",
        "This is the method " + ("word " * 20),
    ])
    names = [n for n, _ in arxiv.sections(text, strict=True)]
    assert "introduction" in names and "method" in names or "methods" in names
    paper = {"id": "local:bbbbbbbbbbbb", "source": "pdf", "text": text + "\n3. Experiments\n" + ("word " * 40)}
    keys = [s["key"] for s in tutor.stations(paper)]
    assert "intro" in keys


def test_local_paper_questions_not_shared(monkeypatch):
    uid, _ = _user("loc")
    text = "1 Introduction\n" + ("A sentence about the claim. " * 30) + "\n2 Methods\n" + ("Step one then step two. " * 30)
    text += "\n3 Experiments\n" + ("Result 12 percent. " * 30) + "\n4 Conclusion\n" + ("We will change one thing. " * 30)
    pid = "local:" + hashlib.sha256((uid + text).encode()).hexdigest()[:12]
    store.put_user_paper(uid, pid, "sha", "Local", "a.pdf", text, 4)
    monkeypatch.setattr(llm, "ready", lambda u=None: True)
    monkeypatch.setattr(llm, "enabled", lambda u=None: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: None)
    tutor.start(uid, "llm-eval", pid)
    with store._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM tutor_questions").fetchone()[0]
    assert n == 0


def test_warm_requires_token_and_reports_remaining(client, monkeypatch):
    monkeypatch.setattr(arxiv, "fulltext", lambda aid, hint=None: {"id": aid, "source": "html", "text": "x", "title": "t"})
    assert client.post("/api/admin/papers/warm?budget=0").status_code == 403
    r = client.post("/api/admin/papers/warm?budget=0", headers={"X-Admin-Token": "a" * 32})
    assert r.status_code == 200
    body = r.json()
    assert body["done"] == 0
    assert body["remaining"] >= 10


def test_deploy_fc_skips_warm_without_admin_token(capsys):
    import ast
    src = (ROOT / "tools" / "deploy" / "deploy_fc.py").read_text(encoding="utf-8")
    assert "请手动预热" in src and "/api/admin/papers/warm" in src
    tree = ast.parse(src)
    fn = next(n for n in tree.body if getattr(n, "name", "") == "warm_after_deploy")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "deploy_fc.py", "exec"), ns)
    out = ns["warm_after_deploy"]("https://example.test", {})
    assert out["skipped"] is True
    assert "请手动预热" in capsys.readouterr().out


def test_requirements_and_wheels_in_sync():
    req = (ROOT / "server" / "requirements.txt").read_text(encoding="utf-8")
    src = (ROOT / "tools" / "deploy" / "download_wheels.py").read_text(encoding="utf-8")
    pkgs = re.search(r"PKGS = \[(.*?)\]", src, re.S).group(1)
    want = [ln.strip() for ln in req.splitlines() if ln.strip() and not ln.startswith("#")]
    have = [p.strip().strip('"').strip("'") for p in pkgs.split(",") if p.strip()]
    assert want == have
    assert any(p.startswith("pypdf") for p in want)


def test_admin_stats_keep_old_keys_and_add_user_papers(client):
    body = client.get("/api/admin/stats", headers={"X-Admin-Token": "a" * 32}).json()
    assert "arxiv_files" in body and "arxiv_bytes" in body
    assert "user_papers" in body


def test_kit_papers_have_zh_and_status_drops_order_phrase():
    k = json.loads((ROOT / "knowledge" / "kits" / "llm-eval.json").read_text(encoding="utf-8"))
    assert "阅读顺序" not in (k.get("status") or "")
    papers = k["papers"]
    assert len(papers) == 10
    for p in papers:
        assert p.get("title_zh")
        assert 2 <= len(p.get("keywords") or []) <= 4
