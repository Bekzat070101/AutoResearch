# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import llm  # noqa: E402
import main  # noqa: E402
import metrics as metrics_mod  # noqa: E402
import store  # noqa: E402

pytestmark = pytest.mark.real_auth


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "m.db")
    store.init_db()
    monkeypatch.setattr(llm, "enabled", lambda uid=None: False)
    metrics_mod._REQ.clear()
    metrics_mod._PATHS.clear()
    metrics_mod._LAT.clear()
    metrics_mod._SCRAPE_ERRORS = 0
    metrics_mod._STREAM_ERRORS = 0


def test_unknown_paths_collapse(monkeypatch):
    metrics_mod._REQ.clear()
    metrics_mod._PATHS.clear()
    metrics_mod._LAT.clear()
    c = TestClient(main.app)
    for i in range(512):
        c.get(f"/api/anonymous-{i}")
    text = c.get("/metrics").text
    paths = set(re.findall(r'path="([^"]+)"', text))
    assert "unknown" in paths
    assert not any(p.startswith("/api/anonymous") for p in paths)


def test_metrics_token_required(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "secret-metrics")
    c = TestClient(main.app)
    assert c.get("/metrics").status_code == 401
    assert c.get("/metrics", headers={"Authorization": "Bearer secret-metrics"}).status_code == 200
    assert c.get("/metrics?token=secret-metrics").status_code == 200


def test_metrics_render_concurrent():
    import threading
    metrics_mod._REQ.clear()
    metrics_mod._PATHS.clear()

    def hit():
        for i in range(50):
            metrics_mod.observe_request(f"/api/x{i % 3}", 200, 0.001)
            metrics_mod.render()

    ts = [threading.Thread(target=hit) for _ in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    metrics_mod.render()


def test_5xx_total_matches_path_counters(client=None):
    metrics_mod.observe_request("/api/boom", 500, 0.01)
    metrics_mod.observe_request("/api/ok", 200, 0.01)
    text = metrics_mod.render()
    assert "qiyan_http_errors_5xx_total" in text
    m = re.search(r"^qiyan_http_errors_5xx_total (\d+)", text, re.M)
    assert m and int(m.group(1)) == 1
    assert 'qiyan_http_requests_total{path="/api/boom",code="500"}' in text
    assert "qiyan_paper_dir_rows" in text
    c = TestClient(main.app)
    h = c.get("/api/health").json()
    pool = h["model_pool"]
    assert "queued" in pool and "running" in pool
    assert "threads_max" in pool
    assert pool["max_queued"] != 160 or "pdf_queued" in pool


def test_users_kind_labels_and_scrape_error(monkeypatch):
    store.create_user("访客甲")
    text = metrics_mod.render()
    assert 'qiyan_users{kind="guest"}' in text
    assert 'qiyan_users{kind="registered"}' in text
    assert re.search(r'qiyan_users\{kind="registered"\} 0', text)
    monkeypatch.setattr(store, "_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    before = metrics_mod._SCRAPE_ERRORS
    text2 = metrics_mod.render()
    assert "qiyan_users " not in text2.split("qiyan_scrape_errors_total")[0] or "kind=" in text2
    assert metrics_mod._SCRAPE_ERRORS == before + 1
    assert "qiyan_scrape_errors_total" in text2
    assert re.search(r"^qiyan_users ", text2, re.M) is None
