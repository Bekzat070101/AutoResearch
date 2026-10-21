# -*- coding: utf-8 -*-
"""Prometheus 文本指标。无额外依赖，给 Grafana / 探活刮取。"""
from __future__ import annotations

import threading
import time
from typing import Any

_START = time.time()
_START_MONO = time.monotonic()
_LOCK = threading.Lock()
_REQ: dict[tuple[str, str], int] = {}
_PATHS: set[str] = set()
_LAT: list[float] = []
_PATH_CAP = 200
_SCRAPE_ERRORS = 0
_STREAM_ERRORS = 0
_INFLIGHT = 0
_REQ_AGENT: dict[tuple[str, str], int] = {}  # 人工学生（带 X-Qiyan-Agent 头）的请求另计，面板上能和真人分开
# 按路由模板的延迟直方图（21-D34）：桶界固定，Grafana 用 histogram_quantile 算 p95；旧的全站 p95 保留为 legacy
BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 45, 60, 120, 300)
_HIST: dict[str, list[int]] = {}      # path -> 每个桶的非累计计数（渲染时再累计）
_HSUM: dict[str, float] = {}
_HCOUNT: dict[str, int] = {}


def process_uptime_seconds() -> float:
    """进程寿命（单调时钟，墙钟回拨不影响）。health 与 /metrics 共用。"""
    return max(0.0, time.monotonic() - _START_MONO)


def observe_start() -> None:
    global _INFLIGHT
    with _LOCK:
        _INFLIGHT += 1


def observe_request(path: str, status: int, seconds: float, synthetic: bool = False) -> None:
    global _INFLIGHT
    code = str(int(status) if status is not None else 0)
    raw = (path or "").split("?")[0][:80].replace("\\", "").replace('"', "") or "/"
    with _LOCK:
        if raw not in _PATHS:
            if len(_PATHS) >= _PATH_CAP:
                raw = "other"
            else:
                _PATHS.add(raw)
        key = (raw, code)
        _REQ[key] = _REQ.get(key, 0) + 1
        if synthetic:
            _REQ_AGENT[key] = _REQ_AGENT.get(key, 0) + 1
        _LAT.append(seconds)
        if len(_LAT) > 2000:
            del _LAT[:1000]
        counts = _HIST.setdefault(raw, [0] * (len(BUCKETS) + 1))
        i = 0
        while i < len(BUCKETS) and seconds > BUCKETS[i]:
            i += 1
        counts[i] += 1
        _HSUM[raw] = _HSUM.get(raw, 0.0) + max(0.0, float(seconds))
        _HCOUNT[raw] = _HCOUNT.get(raw, 0) + 1
        if _INFLIGHT > 0:
            _INFLIGHT -= 1


def _hist_lines() -> list[str]:
    with _LOCK:
        items = [(p, list(c), _HSUM.get(p, 0.0), _HCOUNT.get(p, 0)) for p, c in _HIST.items()]
    if not items:
        return []
    out = ["# HELP qiyan_http_latency_seconds 按路由模板的请求耗时直方图（秒）", "# TYPE qiyan_http_latency_seconds histogram"]
    for path, counts, total, n in sorted(items):
        acc = 0
        for le, c in zip(BUCKETS, counts):
            acc += c
            out.append(f'qiyan_http_latency_seconds_bucket{{path="{path}",le="{le}"}} {acc}')
        acc += counts[-1]
        out.append(f'qiyan_http_latency_seconds_bucket{{path="{path}",le="+Inf"}} {acc}')
        out.append(f'qiyan_http_latency_seconds_sum{{path="{path}"}} {total}')
        out.append(f'qiyan_http_latency_seconds_count{{path="{path}"}} {n}')
    return out


def _req_snapshot() -> dict[tuple[str, str], int]:
    with _LOCK:
        return dict(_REQ)


def observe_stream_error() -> None:
    global _STREAM_ERRORS
    with _LOCK:
        _STREAM_ERRORS += 1


def scrape_errors() -> int:
    with _LOCK:
        return _SCRAPE_ERRORS


def _err_total() -> int:
    n = 0
    for (_path, code), c in _REQ.items():
        try:
            if int(code) >= 500:
                n += c
        except ValueError:
            pass
    return n


def _line(name: str, val: float, labels: dict[str, Any] | None = None, help_: str = "", typ: str = "gauge") -> str:
    bits = [f"# HELP {name} {help_}", f"# TYPE {name} {typ}"]
    if labels:
        inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
        bits.append(f"{name}{{{inner}}} {val}")
    else:
        bits.append(f"{name} {val}")
    return "\n".join(bits)


def render() -> str:
    global _SCRAPE_ERRORS
    import llm
    import store
    cfg = llm.config()
    users = sessions = expired = papers = paper_bytes = dir_rows = dir_nodes = 0
    users_reg = users_guest = users_agent = agent_runs = agent_stuck = agent_done = 0
    paper_by_src: dict[str, int] = {}
    paper_pinned = 0
    scrape_ok = True
    try:
        with store._conn() as c:
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            users_reg = c.execute(
                "SELECT COUNT(*) FROM users WHERE username IS NOT NULL AND username != ''"
            ).fetchone()[0]
            users_guest = max(0, users - users_reg)
            try:
                users_agent = c.execute("SELECT COUNT(*) FROM agent_accounts").fetchone()[0]
                agent_runs = c.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
                agent_stuck = c.execute("SELECT COALESCE(SUM(stuck),0) FROM agent_runs").fetchone()[0]
                agent_done = c.execute("SELECT COUNT(*) FROM agent_events WHERE kind LIKE 'done:%'").fetchone()[0]
            except Exception:
                users_agent = agent_runs = agent_stuck = agent_done = 0
            sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            expired = c.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (time.time(),)).fetchone()[0]
            try:
                papers = c.execute("SELECT COUNT(*) FROM paper_text").fetchone()[0]
                paper_bytes = c.execute("SELECT COALESCE(SUM(bytes),0) FROM paper_text").fetchone()[0]
                paper_pinned = c.execute("SELECT COUNT(*) FROM paper_text WHERE pinned=1").fetchone()[0]
                for r in c.execute("SELECT source, COUNT(*) n FROM paper_text GROUP BY source"):
                    paper_by_src[r[0] or "unknown"] = r[1]
            except Exception:
                pass
            try:
                dir_rows = c.execute("SELECT COUNT(*) FROM paper_dir").fetchone()[0]
                dir_nodes = c.execute("SELECT COUNT(DISTINCT node_id) FROM paper_dir").fetchone()[0]
            except Exception:
                dir_rows = dir_nodes = 0
    except Exception:
        scrape_ok = False
        with _LOCK:
            _SCRAPE_ERRORS += 1
    lats = sorted(_LAT[-500:])
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    lines = [
        _line("qiyan_up", 1, help_="服务进程存活", typ="gauge"),
        _line("qiyan_process_uptime_seconds", process_uptime_seconds(), help_="进程寿命秒数（单调时钟；不等于函数计算实例寿命）"),
        _line("qiyan_db_ephemeral", 1 if store.ephemeral() else 0, help_="1=库在临时盘，实例回收会丢账号"),
        _line("qiyan_db_ready", 1 if getattr(store, "DB_READY", True) else 0, help_="1=库已初始化可用；0=盘没挂上、接口在回 503"),
        _line("qiyan_llm_enabled", 1 if cfg.get("enabled") else 0, help_="兜底模型是否可用"),
        _line("qiyan_http_inflight", _INFLIGHT, help_="正在处理的 HTTP 请求数（含还在推的 SSE 流）"),
    ]
    if scrape_ok:
        lines += [
            _line("qiyan_users", users, help_="用户总数"),
            _line("qiyan_users", users_reg, {"kind": "registered"}, help_="注册用户数"),
            _line("qiyan_users", users_guest, {"kind": "guest"}, help_="访客用户数"),
            _line("qiyan_users", users_agent, {"kind": "synthetic"}, help_="人工学生（AI agent）账号数，已含在总数里"),
            _line("qiyan_agent_runs_total", agent_runs, help_="人工学生上线次数", typ="counter"),
            _line("qiyan_agent_stuck_total", agent_stuck, help_="人工学生说「不知道怎么办」的次数", typ="counter"),
            _line("qiyan_agent_plan_done_total", agent_done, help_="人工学生完成的目标项次数", typ="counter"),
            _line("qiyan_sessions", sessions, help_="会话行数"),
            _line("qiyan_sessions_expired", expired, help_="已过期未清会话"),
            _line("qiyan_paper_text_rows", papers, help_="正文库条数"),
            _line("qiyan_paper_text_pinned", paper_pinned, help_="已 pin 正文条数"),
            _line("qiyan_paper_text_bytes", paper_bytes, help_="正文库字节"),
            _line("qiyan_paper_dir_rows", dir_rows, help_="方向目录条数"),
            _line("qiyan_paper_dir_nodes", dir_nodes, help_="已有目录的方向节点数"),
        ]
        for src, n in sorted(paper_by_src.items()):
            lines.append(_line("qiyan_paper_text_rows", n, {"source": src}, help_="按来源的正文库条数"))
    import account_store
    lines += [
        _line("qiyan_account_snapshot_users", account_store.snapshot_users(), help_="最近一份账号副本里的用户数"),
        _line("qiyan_account_snapshot_age_seconds", account_store.snapshot_age(),
              help_="距上次写账号副本的秒数，-1=本进程还没写过"),
    ]
    lines += [
        _line("qiyan_scrape_errors_total", _SCRAPE_ERRORS, help_="刮取失败次数", typ="counter"),
        _line("qiyan_stream_errors_total", _STREAM_ERRORS, help_="SSE 流内 error 次数", typ="counter"),
        _line("qiyan_http_errors_5xx_total", _err_total(), help_="进程内 5xx 累计（等于分路 code>=500 之和）", typ="counter"),
        _line("qiyan_http_latency_p95_seconds", p95, help_="legacy：进程内近 500 次全站 p95；按路径请看 qiyan_http_latency_seconds 直方图"),
    ]
    lines += _hist_lines()
    with _LOCK:
        agent_req = dict(_REQ_AGENT)
    if agent_req:
        lines.append("# HELP qiyan_agent_http_requests_total 人工学生发出的 HTTP 请求次数")
        lines.append("# TYPE qiyan_agent_http_requests_total counter")
        for (path, status), n in sorted(agent_req.items()):
            lines.append(f'qiyan_agent_http_requests_total{{path="{path}",code="{status}"}} {n}')
    seen_help = set()
    for (path, status), n in sorted(_req_snapshot().items()):
        name = "qiyan_http_requests_total"
        if name not in seen_help:
            lines.append(f"# HELP {name} HTTP 请求次数")
            lines.append(f"# TYPE {name} counter")
            seen_help.add(name)
        lines.append(f'{name}{{path="{path}",code="{status}"}} {n}')
    return "\n".join(lines) + "\n"
