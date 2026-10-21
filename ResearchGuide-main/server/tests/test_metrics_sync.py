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
    metrics_mod._INFLIGHT = 0
    metrics_mod._HIST.clear()
    metrics_mod._HSUM.clear()
    metrics_mod._HCOUNT.clear()


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


MON = Path(__file__).resolve().parent.parent.parent / "tools" / "monitoring"


def _families(text: str) -> set[str]:
    return set(re.findall(r"^# TYPE (qiyan_[a-z0-9_]+)", text, re.M))


def test_latency_histogram_boundaries_and_paths():
    """21-D34：同 path 0.1/2/45 秒 → 桶累计、sum 47.1、count 3；第二 path 隔离；HELP/TYPE 只出一次。"""
    for sec in (0.1, 2, 45):
        metrics_mod.observe_request("/api/a", 200, sec)
    metrics_mod.observe_request("/api/b", 200, 0.3)
    text = metrics_mod.render()
    assert text.count("# TYPE qiyan_http_latency_seconds histogram") == 1

    def bucket(path, le):
        m = re.search(rf'^qiyan_http_latency_seconds_bucket\{{path="{re.escape(path)}",le="{re.escape(le)}"\}} (\d+)$', text, re.M)
        assert m, (path, le)
        return int(m.group(1))

    assert bucket("/api/a", "0.05") == 0
    assert bucket("/api/a", "0.1") == 1
    assert bucket("/api/a", "1") == 1
    assert bucket("/api/a", "2") == 2
    assert bucket("/api/a", "30") == 2
    assert bucket("/api/a", "45") == 3
    assert bucket("/api/a", "300") == 3
    assert bucket("/api/a", "+Inf") == 3
    assert re.search(r'^qiyan_http_latency_seconds_sum\{path="/api/a"\} 47\.1$', text, re.M)
    assert re.search(r'^qiyan_http_latency_seconds_count\{path="/api/a"\} 3$', text, re.M)
    assert bucket("/api/b", "0.25") == 0
    assert bucket("/api/b", "0.5") == 1
    assert bucket("/api/b", "+Inf") == 1


def test_inflight_db_ready_and_monotonic_uptime(monkeypatch):
    metrics_mod.observe_start()
    metrics_mod.observe_start()
    text = metrics_mod.render()
    assert re.search(r"^qiyan_http_inflight 2$", text, re.M)
    metrics_mod.observe_request("/api/x", 200, 0.01)
    assert re.search(r"^qiyan_http_inflight 1$", metrics_mod.render(), re.M)
    assert re.search(r"^qiyan_db_ready 1$", text, re.M)
    monkeypatch.setattr(store, "DB_READY", False)
    assert re.search(r"^qiyan_db_ready 0$", metrics_mod.render(), re.M)
    monkeypatch.setattr(store, "DB_READY", True)
    base = metrics_mod._START_MONO
    monkeypatch.setattr(metrics_mod.time, "monotonic", lambda: base + 5.0)
    monkeypatch.setattr(metrics_mod.time, "time", lambda: 0.0)  # 墙钟倒退也不影响
    assert abs(metrics_mod.process_uptime_seconds() - 5.0) < 1e-6
    h = TestClient(main.app).get("/api/health").json()
    assert h["checks"]["uptime_seconds"] >= 0


def _referenced_metrics(text: str) -> set[str]:
    names = set()
    for n in re.findall(r"\b(qiyan_[a-z0-9_]+)", text):
        n = re.sub(r"_(bucket|sum|count)$", "", n) if n.startswith("qiyan_http_latency_seconds") else n
        names.add(n)
    return names


def test_dashboard_and_alert_rules_reference_exported_metrics():
    """面板与告警规则引用的每个指标族都在 /metrics 里；数据源 uid 与 provisioning 一致。"""
    import json
    metrics_mod.observe_request("/api/x", 200, 0.01)
    exported = _families(metrics_mod.render())
    dash_path = MON / "grafana" / "dashboards" / "qiyan-overview.json"
    dash = json.loads(dash_path.read_text(encoding="utf-8"))
    exprs = [t.get("expr", "") for p in dash["panels"] for t in p.get("targets", [])]
    assert exprs
    missing = _referenced_metrics("\n".join(exprs)) - exported
    assert not missing, f"dashboard 引用了 /metrics 没有的指标：{sorted(missing)}"
    ds = (MON / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8")
    uid = re.search(r"^\s*uid:\s*(\S+)", ds, re.M).group(1)
    assert dash.get("uid")
    for p in dash["panels"]:
        assert p.get("datasource", {}).get("uid") == uid, p.get("title")
    rules = (MON / "alerts.yml").read_text(encoding="utf-8")
    missing = _referenced_metrics("\n".join(re.findall(r"expr:\s*(.+)", rules))) - exported
    assert not missing, f"alerts.yml 引用了 /metrics 没有的指标：{sorted(missing)}"
    assert "- alert: QiyanDown" in rules and "- alert: QiyanHigh5xxRatio" in rules


def test_promtool_rules_when_available():
    import shutil
    import subprocess
    tool = MON / ".tools" / "prometheus" / "promtool"
    exe = str(tool) if tool.exists() else shutil.which("promtool")
    if not exe:
        pytest.skip("promtool 不在 tools/monitoring/.tools 也不在 PATH（先跑 fetch-tools.zsh）")
    subprocess.run([exe, "check", "rules", str(MON / "alerts.yml")], check=True, capture_output=True)
    subprocess.run([exe, "test", "rules", str(MON / "tests" / "alerts.test.yml")], check=True, capture_output=True)
