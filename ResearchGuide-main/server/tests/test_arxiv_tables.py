# -*- coding: utf-8 -*-
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arxiv  # noqa: E402
import store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "arxiv.db")
    store.init_db()


HTML_TABLE = """<html><body><article>
<p>Intro line</p>
<table>
  <caption>Scores</caption>
  <tr><th>A</th><th>B</th><th>C</th></tr>
  <tr><td>1</td><td>2</td><td>3</td></tr>
</table>
<p>After table</p>
</article></body></html>"""


def test_html_to_text_keeps_table_structure():
    got = arxiv.parse_html(HTML_TABLE)
    assert "[[table:t1]]" in got["text"]
    assert "Intro line" in got["text"]
    assert "After table" in got["text"]
    assert got["tables"]
    t = got["tables"][0]
    assert t["id"] == "t1"
    assert t["headers"] == ["A", "B", "C"]
    assert t["rows"] == [["1", "2", "3"]]
    assert arxiv._html_to_text(HTML_TABLE) == got["text"]


def test_html_to_text_without_table_has_empty_tables():
    got = arxiv.parse_html("<html><article><p>Hello world</p></article></html>")
    assert got["tables"] == []
    assert "Hello world" in got["text"]


def test_empty_table_is_dropped():
    page = "<html><article><p>X</p><table><tr></tr></table><p>Y</p></article></html>"
    got = arxiv.parse_html(page)
    assert "[[table:" not in got["text"]
    assert got["tables"] == []
    assert "X" in got["text"] and "Y" in got["text"]


def test_td_first_row_becomes_headers_when_later_rows_have_numbers():
    page = """<html><article>
<table>
  <tr><td>Model</td><td>STEM</td></tr>
  <tr><td>GPT-3</td><td>36.7</td></tr>
</table>
</article></html>"""
    t = arxiv.parse_html(page)["tables"][0]
    assert t["headers"] == ["Model", "STEM"]
    assert t["rows"] == [["GPT-3", "36.7"]]


def test_numeric_first_row_stays_body():
    page = """<html><article>
<table>
  <tr><td>1</td><td>2</td></tr>
  <tr><td>3</td><td>4</td></tr>
</table>
</article></html>"""
    t = arxiv.parse_html(page)["tables"][0]
    assert t["headers"] == []
    assert t["rows"] == [["1", "2"], ["3", "4"]]


def test_old_cache_served_without_refetch(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2009.03300.json").write_text(json.dumps({
        "id": "2009.03300", "title": "MMLU", "source": "html",
        "text": "old flattened table\n25.0", "url": "u",
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: calls.append(url) or b"nope")
    got = arxiv.fulltext("2009.03300")
    assert got["text"] == "old flattened table\n25.0"
    assert got.get("needs_reparse") is True
    assert calls == []


def test_html_cache_with_tables_is_kept(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2009.03300.json").write_text(json.dumps({
        "id": "2009.03300", "title": "MMLU", "source": "html",
        "text": "[[table:t1]]", "url": "u", "tables": [{"id": "t1", "caption": "", "headers": ["A"], "rows": [["1"]], "at": 0}],
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: calls.append(url) or b"nope")
    got = arxiv.fulltext("2009.03300")
    assert got["tables"][0]["id"] == "t1"
    assert calls == []


def test_reparse_failure_keeps_old_text_and_backs_off(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    (tmp_path / "2009.03300.json").write_text(json.dumps({
        "id": "2009.03300", "title": "MMLU", "source": "html",
        "text": "old flattened table", "url": "u",
    }), encoding="utf-8")
    calls = []

    def down(url, timeout=20, max_bytes=0):
        calls.append(url)
        raise arxiv.ArxivError("连不上 arXiv")

    monkeypatch.setattr(arxiv, "_get", down)
    first = arxiv.fulltext("2009.03300")
    second = arxiv.fulltext("2009.03300")
    assert first["text"] == second["text"] == "old flattened table"
    assert "tables" not in first
    assert first.get("needs_reparse") is True
    assert calls == []


def test_fulltext_tries_html_before_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    html = "<html><head><title>From HTML</title></head><body><article class='ltx_document'>"
    html += "<h1 class='ltx_title_document'>From HTML</h1><p>" + ("Full text sentence. " * 200) + "</p></article></body></html>"
    meta = []
    monkeypatch.setattr(arxiv, "papers", lambda ids: meta.append(ids) or [{"title": "From Meta", "summary": "abs", "url": "u"}])
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: html.encode())
    got = arxiv.fulltext("2310.17623")
    assert got["source"] == "html"
    assert got["title"] == "From HTML"
    assert meta == []


def test_fulltext_hint_skips_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(arxiv, "papers", lambda ids: (_ for _ in ()).throw(AssertionError("hint 时不应打元数据")))
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: (_ for _ in ()).throw(arxiv.ArxivError("连不上 arXiv")))
    got = arxiv.fulltext("2310.17623", hint={"title": "Hint T", "summary": "hint abstract"})
    assert got["source"] == "abstract"
    assert got["title"] == "Hint T"
    assert got["text"] == "hint abstract"


def test_cache_write_failure_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(arxiv, "CACHE_DIR", tmp_path / "ro")
    html = "<html><article><p>" + ("Full text sentence. " * 200) + "</p></article></html>"
    monkeypatch.setattr(arxiv, "_get", lambda url, timeout=20, max_bytes=0: html.encode())
    monkeypatch.setattr(arxiv, "papers", lambda ids: (_ for _ in ()).throw(AssertionError("html 成功不应打元数据")))
    orig = Path.write_text

    def boom(self, *a, **k):
        if self.suffix in {".tmp", ".json"}:
            raise OSError("read-only")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", boom)
    got = arxiv.fulltext("2310.17623")
    assert got["source"] == "html"


def test_illegal_id_is_400():
    with pytest.raises(arxiv.ArxivError) as exc:
        arxiv.clean_id("not-an-id")
    assert exc.value.status == 400


def test_papers_and_tutor_illegal_id_are_400(tmp_path, monkeypatch):
    import store
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "id400.db")
    store.init_db()
    user = store.create_user("id400")
    token = store.create_session(user["uid"], 3600)
    client = TestClient(main.app)
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/papers/abc", headers=h).status_code == 400
    r = client.get(f"/api/tutor/abc?uid={user['uid']}&kit=llm-eval", headers=h)
    assert r.status_code == 400
