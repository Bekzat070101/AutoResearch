"""Verified arXiv DOI metadata via DataCite. Metadata is never full text.

Only the fixed public DataCite endpoint is contacted; caller input cannot
choose a host. Keep the existing arXiv IDs so reading/triage/cards still agree.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

from limits import TTLCache, fetch

API = "https://api.datacite.org/dois"
_CACHE = TTLCache(6 * 3600, 512)
_SEARCH = TTLCache(600, 128)
_ID = re.compile(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})(?:v\d+)?", re.I)


def _get(url):
    return json.loads(fetch(urllib.request.Request(url, headers={
        "User-Agent": "QiyanResearchMentor/0.3", "Accept": "application/vnd.api+json"
    }), timeout=6, max_bytes=2 * 1024 * 1024))


def parse(row):
    a = row.get("attributes") or {}
    match = _ID.fullmatch(str(a.get("doi") or row.get("id") or ""))
    titles = a.get("titles") or []
    if not match or not titles or not titles[0].get("title"):
        return None
    aid = match.group(1)
    dates = [d["date"][:10] for d in a.get("dates", [])
             if d.get("dateType") == "Submitted" and d.get("date")]
    summary = next((d.get("description", "") for d in a.get("descriptions", [])
                    if d.get("descriptionType") == "Abstract"), "")
    result = {"id": aid, "title": titles[0]["title"], "summary": summary,
              "url": "https://arxiv.org/abs/" + aid, "source": "arxiv",
              "metadata_source": "datacite", "metadata_url": API + "/10.48550/arXiv." + aid,
              "authors": [c.get("name", "") for c in a.get("creators", [])],
              "published": min(dates) if dates else "",
              "categories": [s.get("subject", "") for s in a.get("subjects", [])
                             if s.get("subjectScheme") == "arXiv"]}
    _CACHE.set(aid, result)
    return result


def cached(aid):
    return _CACHE.lookup(aid)[1]


def paper(aid):
    if not re.fullmatch(r"\d{4}\.\d{4,5}", aid):
        raise ValueError("无效的 arXiv 编号")
    hit = cached(aid)
    if hit:
        return hit
    return parse(_get(API + "/10.48550/arXiv." + aid).get("data") or {})


def search(q, limit=10):
    tokens = re.findall(r"[A-Za-z][A-Za-z-]{1,30}", q)[:6]
    if not tokens:
        return []
    terms = " AND ".join(tokens)
    query = f"prefix:10.48550 AND (titles.title:({terms}) OR descriptions.description:({terms}))"
    key = (query.lower(), min(limit, 20))
    hit, result = _SEARCH.lookup(key)
    if hit:
        return result
    data = _get(API + "?" + urllib.parse.urlencode({"query": query, "page[size]": key[1], "sort": "relevance"}))
    result = [p for row in data.get("data", []) if (p := parse(row))]
    _SEARCH.set(key, result)
    return result
