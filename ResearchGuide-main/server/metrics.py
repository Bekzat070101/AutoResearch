# -*- coding: utf-8 -*-
"""Prometheus 文本指标。无额外依赖，给 Grafana / 探活刮取。"""
from __future__ import annotations

import time
from typing import Any

_START = time.time()
_REQ: dict[tuple[str, str], int] = {}
_LAT: list[float] = []


def observe_request(path: str, status: int, seconds: float) -> None:
    code = str(int(status) if status is not None else 0)
    raw = (path or "").split("?")[0][:80].replace("\\", "").replace('"', "")
    key = (raw or "/", code)
    _REQ[key] = _REQ.get(key, 0) + 1
    _LAT.append(seconds)
    if len(_LAT) > 2000:
        del _LAT[:1000]


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
    import llm
    import store
    cfg = llm.config()
    users = sessions = expired = papers = paper_bytes = dir_rows = dir_nodes = 0
    try:
        with store._conn() as c:
            users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            sessions = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            expired = c.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (time.time(),)).fetchone()[0]
            try:
                papers = c.execute("SELECT COUNT(*) FROM paper_text").fetchone()[0]
                paper_bytes = c.execute("SELECT COALESCE(SUM(bytes),0) FROM paper_text").fetchone()[0]
            except Exception:
                pass
            try:
                dir_rows = c.execute("SELECT COUNT(*) FROM paper_dir").fetchone()[0]
                dir_nodes = c.execute("SELECT COUNT(DISTINCT node_id) FROM paper_dir").fetchone()[0]
            except Exception:
                dir_rows = dir_nodes = 0
    except Exception:
        pass
    lats = sorted(_LAT[-500:])
    p95 = lats[int(len(lats) * 0.95)] if lats else 0
    lines = [
        _line("qiyan_up", 1, help_="服务进程存活", typ="gauge"),
        _line("qiyan_process_uptime_seconds", time.time() - _START, help_="进程启动秒数"),
        _line("qiyan_db_ephemeral", 1 if store.ephemeral() else 0, help_="1=库在临时盘，实例回收会丢账号"),
        _line("qiyan_llm_enabled", 1 if cfg.get("enabled") else 0, help_="兜底模型是否可用"),
        _line("qiyan_users", users, help_="注册用户数"),
        _line("qiyan_sessions", sessions, help_="会话行数"),
        _line("qiyan_sessions_expired", expired, help_="已过期未清会话"),
        _line("qiyan_paper_text_rows", papers, help_="正文库条数"),
        _line("qiyan_paper_text_bytes", paper_bytes, help_="正文库字节"),
        _line("qiyan_paper_dir_rows", dir_rows, help_="方向目录条数"),
        _line("qiyan_paper_dir_nodes", dir_nodes, help_="已有目录的方向节点数"),
        _line("qiyan_http_errors_5xx_total", _err_total(), help_="进程内 5xx 累计（等于分路 code>=500 之和）", typ="counter"),
        _line("qiyan_http_latency_p95_seconds", p95, help_="进程内近 500 次 p95 延迟"),
    ]
    seen_help = set()
    for (path, status), n in sorted(_REQ.items()):
        name = "qiyan_http_requests_total"
        if name not in seen_help:
            lines.append(f"# HELP {name} HTTP 请求次数")
            lines.append(f"# TYPE {name} counter")
            seen_help.add(name)
        lines.append(f'{name}{{path="{path}",code="{status}"}} {n}')
    return "\n".join(lines) + "\n"
