# -*- coding: utf-8 -*-
"""给本机 API 制造一点流量，让仪表盘有线可看：health / paths / 一个访客登录 + me / prefs，每 10 次夹一个 404。
用法：uv run --no-project --offline python tools/monitoring/smoke_traffic.py --base http://127.0.0.1:8100 --seconds 60 --rps 3
不调模型、不花钱；只建一个访客号（/api/auth/login 每小时每 IP 有上限）。"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def call(base: str, method: str, path: str, body=None, token: str | None = None):
    req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            code = resp.status
    except urllib.error.HTTPError as e:
        data, code = e.read(), e.code
    return code, time.perf_counter() - t0, data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--rps", type=float, default=3.0)
    a = ap.parse_args()
    code, _, data = call(a.base, "POST", "/api/auth/login", {"nickname": "监控烟测", "consent": True})
    token = json.loads(data).get("token") if code == 200 else None
    print(f"guest login: {code} token={'yes' if token else 'no'}")
    plan = [("GET", "/api/health", None), ("GET", "/api/paths", None), ("GET", "/api/auth/me", token), ("GET", "/api/me/prefs", token)]
    stats: dict[int, int] = {}
    n, t_end, gap = 0, time.time() + a.seconds, 1.0 / max(0.1, a.rps)
    while time.time() < t_end:
        m, p, tk = ("GET", "/api/nope", None) if n % 10 == 9 else plan[n % len(plan)]
        code, dt, _ = call(a.base, m, p, token=tk)
        stats[code] = stats.get(code, 0) + 1
        n += 1
        time.sleep(gap)
    print(f"{n} requests in {a.seconds}s:", dict(sorted(stats.items())))


if __name__ == "__main__":
    main()
