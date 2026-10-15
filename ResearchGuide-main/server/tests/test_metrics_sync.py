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
    metrics_mod._LAT.clear()


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
