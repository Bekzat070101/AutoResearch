# -*- coding: utf-8 -*-
"""PsyArXiv / SocArXiv（OSF 预印本）元数据。和 arXiv 分锁。"""
from __future__ import annotations

import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from limits import ReadLimitError, fetch

UA = "QiyanResearchGuide/1.0 (mailto:qiyan@local)"
OSF = "https://api.osf.io/v2/preprints/"
_LOCK = threading.Lock()
_LAST = [0.0]


class OsfError(Exception):
    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def _clean(s: str, n: int = 600) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", " ", s or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n]


def parse_preprints(payload: dict[str, Any], provider: str) -> list[dict[str, Any]]:
    out = []
    for row in payload.get("data") or []:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("id") or "")
        attrs = row.get("attributes") or {}
        title = _clean(str(attrs.get("title") or ""), 300)
        if not oid or not title:
            continue
        desc = _clean(str(attrs.get("description") or ""), 600)
        published = str(attrs.get("date_published") or attrs.get("date_created") or "")[:10]
        tags = [str(t).strip() for t in (attrs.get("tags") or []) if str(t).strip()][:8]
        links = ((row.get("links") or {}).get("html")) or f"https://osf.io/{oid}/"
        names = []
        emb = ((row.get("embeds") or {}).get("contributors") or {}).get("data") or []
        for c in emb:
            name = (((c.get("embeds") or {}).get("users") or {}).get("data") or {}).get("attributes") or {}
            full = name.get("full_name") or ""
            if full:
                names.append(full)
            if len(names) >= 4:
                break
        out.append({
            "id": f"{provider}:{oid}",
            "title": title,
            "summary": desc,
            "published": published,
            "authors": names,
            "url": links,
            "tags": tags,
            "categories": [],
            "source": provider,
        })
    return out


def recent(provider: str, subjects: list[str] | None = None, max_results: int = 25,
           cached_only: bool = False) -> list[dict[str, Any]]:
    if cached_only:
        return []
    q = {
        "filter[provider]": provider,
        "sort": "-date_created",
        "page[size]": str(min(50, max(1, max_results))),
        "embed": "contributors",
    }
    if subjects:
        q["filter[subjects]"] = ",".join(subjects)
    url = OSF + "?" + urllib.parse.urlencode(q)
    with _LOCK:
        wait = 1.0 - (time.time() - _LAST[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.api+json"})
        try:
            raw = fetch(req, timeout=40, max_bytes=2_000_000)
        except urllib.error.HTTPError as exc:
            raise OsfError(f"OSF 返回 HTTP {exc.code}", exc.code) from exc
        except ReadLimitError as exc:
            raise OsfError(f"OSF 响应{exc}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OsfError("连不上 OSF") from exc
        _LAST[0] = time.time()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OsfError("OSF 回包不是 JSON") from exc
    if not isinstance(data, dict):
        raise OsfError("OSF 回包形状不对")
    return parse_preprints(data, provider)[:max_results]
