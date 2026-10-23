# -*- coding: utf-8 -*-
"""bioRxiv / medRxiv 预印本：元数据、最新论文、全文（JATS XML），给研读层用。

- 只用 api.biorxiv.org 官方接口和它给的 .source.xml 全文链接；不抓需要登录的东西。
- 分锁：api.biorxiv.org 串行间隔 1 秒；www.biorxiv.org 在 Cloudflare 后面，连打会 1015，
  全文下载单独一把锁、间隔 3 秒。和 arXiv / OSF 各自分锁。
- 内部 id 形如 biorxiv:10.1101.2024.12.06.627082——DOI 的「/」编码成「.」（URL 路径参数
  安全）。bioRxiv 现在混用 10.1101 和 10.64898 两种 DOI 前缀，前缀必须留在 id 里，
  不然没法从 id 还原 DOI 去查详情。
- 全文取不到（老论文没有 JATS、被限流）就退回摘要，source 如实标 abstract，
  retry_after 到点由下次调用重试（语义同 arxiv.fulltext）。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from limits import ReadLimitError, fetch

API = "https://api.biorxiv.org"
UA = "QiyanResearchGuide/1.0 (education; contact via repo)"
SERVERS = ("biorxiv", "medrxiv")
PAGE = 30  # details 接口每页固定 30 条
MAX_PAGES = 6  # recent 最多翻 6 页（180 条）：本地关键词过滤前的量足够填定额了

_API_LOCK = threading.Lock()
_API_LAST = [0.0]
_WEB_LOCK = threading.Lock()
_WEB_LAST = [0.0]

RETRY_NO_XML = 7 * 24 * 3600  # 确实没有 JATS：隔一周再试
RETRY_TRANSIENT = 10 * 60  # 限流、网络错：十分钟后再试

# biorxiv:10.1101.2024.12.06.627082 —— DOI 前缀两段（10.xxxx）+ 后缀（数字、点）
_ID_RE = re.compile(r"^(biorxiv|medrxiv):(10\.\d{4,5}\.[0-9][0-9A-Za-z.\-]*)$", re.I)
_DOI_RE = re.compile(r"^(10\.\d{4,5})/([0-9][0-9A-Za-z.\-]*)$")
_URL_RE = re.compile(
    r"^https?://(?:www\.)?(biorxiv|medrxiv)\.org/content/"
    r"(10\.\d{4,5})/([0-9][0-9A-Za-z.\-]*?)(?:v\d+)?"
    r"(?:\.full(?:-text|\.pdf)?)?/?$", re.I)


class BiorxivError(Exception):
    def __init__(self, msg: str, status: int = 502) -> None:
        super().__init__(msg)
        self.status = status  # HTTP 状态码；400 输入问题，404 没这篇，其余上游错


def is_id(raw: str) -> bool:
    return bool(_ID_RE.match((raw or "").strip()))


def _encode_doi(server: str, doi: str) -> str:
    return f"{server}:{doi.replace('/', '.', 1)}"


def _doi_of(pid: str) -> tuple[str, str]:
    """内部 id → (server, DOI)。DOI 前缀固定是 10.xxxx 两段，所以按前两个「.」切开还原。"""
    m = _ID_RE.match((pid or "").strip())
    if not m:
        raise BiorxivError("不是有效的 bioRxiv 编号（形如 biorxiv:10.1101.2024.12.06.627082）", 400)
    server = m.group(1).lower()
    parts = m.group(2).split(".")
    doi = f"{parts[0]}.{parts[1]}/{'.'.join(parts[2:])}"
    return server, doi


def clean_id(raw: str) -> str:
    """接受内部 id、裸 DOI、doi: 前缀、www.biorxiv.org/content/… 页面链接（可带 v2/.full.pdf）。"""
    s = (raw or "").strip()
    server = ""
    doi = ""
    m = _ID_RE.match(s)
    if m:
        server, doi = m.group(1).lower(), _doi_of(s)[1]
    else:
        m = _URL_RE.match(s)
        if m:
            server, doi = m.group(1).lower(), f"{m.group(2)}/{m.group(3)}"
        else:
            pm = re.match(r"^(biorxiv|medrxiv):(.+)$", s, re.I)
            if pm:
                server = pm.group(1).lower()
            rest = pm.group(2) if pm else re.sub(r"^doi:", "", s, flags=re.I)
            if pm:
                rest = re.sub(r"v\d+$", "", rest)  # biorxiv:10.1101/xxx v2 这种尾巴
            if _DOI_RE.match(rest):
                doi = rest
                server = server or "biorxiv"  # 裸 DOI 不带库信息，按 bioRxiv 查（两个库 DOI 前缀重叠）
    if not server or not doi:
        raise BiorxivError("不是有效的 bioRxiv 编号（形如 biorxiv:10.1101.2024.12.06.627082）", 400)
    return _encode_doi(server, doi)


def _get(url: str, lock: threading.Lock, last: list, interval: float,
         timeout: int = 40, max_bytes: int = 8 * 1024 * 1024) -> bytes:
    with lock:
        wait = interval - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            body = fetch(req, timeout=timeout, max_bytes=max_bytes)
        except urllib.error.HTTPError as exc:
            raise BiorxivError(f"bioRxiv 返回 HTTP {exc.code}", exc.code) from exc
        except ReadLimitError as exc:
            raise BiorxivError(f"bioRxiv 响应{exc}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BiorxivError("连不上 bioRxiv") from exc
        finally:
            last[0] = time.time()
    return body


def _api(url: str) -> bytes:
    return _get(url, _API_LOCK, _API_LAST, 1.0)


def _web(url: str) -> bytes:
    return _get(url, _WEB_LOCK, _WEB_LAST, 3.0)


def parse_details(payload: dict[str, Any], server: str) -> list[dict[str, Any]]:
    """details 接口一条 JSON → 论文列表（同一 DOI 的多个版本各一条，latest 交给调用方选）。"""
    msgs = payload.get("messages") or [{}]
    status = str((msgs[0] or {}).get("status") or "")
    if status and status != "ok":
        raise BiorxivError(f"bioRxiv 接口说：{status}")
    out = []
    for row in payload.get("collection") or []:
        if not isinstance(row, dict):
            continue
        doi = str(row.get("doi") or "")
        title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
        if not doi or not title:
            continue
        m = _DOI_RE.match(doi)
        if not m:
            continue
        version = str(row.get("version") or "1")
        host = "www.biorxiv.org" if server == "biorxiv" else "www.medrxiv.org"
        out.append({
            "id": _encode_doi(server, doi),
            "version": version,
            "title": title[:300],
            "summary": re.sub(r"\s+", " ", str(row.get("abstract") or "")).strip()[:2000],
            "authors": [a.strip() for a in str(row.get("authors") or "").split(";") if a.strip()][:8],
            "published": str(row.get("date") or "")[:10],
            "url": f"https://{host}/content/{doi}v{version}",
            "categories": [str(row.get("category") or "")] if row.get("category") else [],
            "source": server,
            "jats": str(row.get("jatsxml") or ""),
        })
    return out


def _latest(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一篇的多版本只留 version 最大的那条。"""
    best: dict[str, dict[str, Any]] = {}
    for p in papers:
        cur = best.get(p["id"])
        if not cur or int(p["version"]) > int(cur["version"]):
            best[p["id"]] = p
    return list(best.values())


def _rows(pid: str) -> list[dict[str, Any]]:
    """按 DOI 查 details，返回该论文所有版本（collection 一版一条）。"""
    aid = clean_id(pid)
    server, doi = _doi_of(aid)
    url = f"{API}/details/{server}/{urllib.parse.quote(doi)}/na/json"
    try:
        data = json.loads(_api(url).decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise BiorxivError("bioRxiv 回包不是 JSON") from exc
    if not isinstance(data, dict):
        raise BiorxivError("bioRxiv 回包形状不对")
    return parse_details(data, server)


def paper(pid: str) -> dict[str, Any] | None:
    got = _latest(_rows(pid))
    return got[0] if got else None


def _hits(text: str, kws: list[str]) -> bool:
    t = text.lower()
    return not kws or any(k in t for k in kws)


def recent(server: str = "biorxiv", category: str = "", keywords: list[str] | None = None,
           max_results: int = 25, days: int = 30, cached_only: bool = False) -> list[dict[str, Any]]:
    """最近 days 天该分类下的论文，标题或摘要命中任一关键词；没有关键词就只按分类。"""
    if cached_only:
        return []
    server = server if server in SERVERS else "biorxiv"
    kws = [k.lower() for k in (keywords or []) if k]
    today = datetime.now(timezone.utc).date()
    frm = (today - timedelta(days=days)).isoformat()
    to = today.isoformat()
    cat = (category or "").strip().lower()
    out: list[dict[str, Any]] = []
    total = None
    for cursor in range(0, PAGE * MAX_PAGES, PAGE):
        url = f"{API}/details/{server}/{frm}/{to}/{cursor}/json"
        if cat:
            url += "?" + urllib.parse.urlencode({"category": cat})
        try:
            data = json.loads(_api(url).decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BiorxivError("bioRxiv 回包不是 JSON") from exc
        rows = parse_details(data, server)
        if total is None:
            msgs = (data.get("messages") or [{}])[0] or {}
            try:
                total = int(str(msgs.get("total") or 0))
            except ValueError:
                total = 0
        for p in _latest(rows):
            if _hits(p["title"] + " " + p["summary"], kws):
                out.append(p)
        if len(out) >= max_results or not rows or (total and cursor + PAGE >= total):
            break
    return out[:max_results]


# ---------- 全文（JATS XML） ----------

_DROP_TAGS = {"graphic", "inline-graphic", "media", "supplementary-material",
              "ext-link", "disp-formula", "inline-formula", "xref"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text_of(el: ET.Element) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _cells(tr: ET.Element) -> list[str]:
    cells = []
    for c in tr:
        if _local(c.tag) in ("td", "th"):
            cells.append(_text_of(c)[:500])
    return cells


def _tables_of(wrap: ET.Element, tid: str) -> dict[str, Any]:
    caption = ""
    for child in wrap:
        if _local(child.tag) in ("caption", "title"):
            caption = _text_of(child)
            break
    table = None
    for t in wrap.iter():
        if _local(t.tag) == "table":
            table = t
            break
    rows: list[list[str]] = []
    headers: list[str] = []
    if table is not None:
        for i, tr in enumerate([e for e in table.iter() if _local(e.tag) == "tr"]):
            row = _cells(tr)
            if not any(row):
                continue
            if i == 0 and all(_local(c.tag) == "th" for c in tr if _local(c.tag) in ("td", "th")):
                headers = row
            else:
                rows.append(row)
    return {"id": tid, "caption": caption, "headers": headers, "rows": rows, "at": 0}


def parse_jats(xml_text: str) -> dict[str, Any]:
    """JATS 全文 → {text, tables}，契约同 arxiv.parse_html：表格抽走、正文留 [[table:tN]]。

    <sec><title> 单独成行（arxiv.sections() 的正则直接可用）；<fig> 只留说明文字；
    公式、图、参考文献列表丢弃。
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise BiorxivError("bioRxiv 全文不是 XML") from exc
    if _local(root.tag) != "article":
        raise BiorxivError("bioRxiv 全文不是 JATS")
    body = next((e for e in root if _local(e.tag) == "body"), None)
    lines: list[str] = []
    tables: list[dict[str, Any]] = []

    def walk(el: ET.Element) -> None:
        for child in el:
            tag = _local(child.tag)
            if tag == "sec":
                title = next((t for t in child if _local(t.tag) == "title"), None)
                if title is not None and _text_of(title):
                    lines.append(_text_of(title)[:200])
                walk(child)
            elif tag == "p":
                txt = _text_of(child)
                if txt:
                    lines.append(txt)
            elif tag == "table-wrap":
                tables.append(_tables_of(child, f"t{len(tables) + 1}"))
                lines.append(f"[[table:t{len(tables)}]]")
            elif tag == "fig":
                cap = next((t for t in child if _local(t.tag) in ("caption", "fig-group")), None)
                txt = _text_of(cap) if cap is not None else ""
                if txt:
                    lines.append(txt[:300])
            elif tag in _DROP_TAGS or tag in ("front", "back"):
                continue
            else:
                walk(child)

    if body is not None:
        walk(body)
    text = "\n".join(ln for ln in lines if ln)
    for t in tables:
        pos = text.find(f"[[table:{t['id']}]]")
        t["at"] = pos if pos >= 0 else 0
    return {"text": text, "tables": tables}


def _cached(aid: str) -> dict[str, Any] | None:
    try:
        import store
        row = store.get_paper_text(aid)
    except Exception:
        return None
    if not row or row.get("source") in ("html", "pdf"):
        return row
    if row.get("source") == "abstract" and time.time() < float(row.get("retry_after") or 0):
        return row  # 摘要还没到重试时间，直接复用；到点了当没有，重新取
    return None


def fulltext(pid: str) -> dict[str, Any]:
    """{id, source: 'html' | 'abstract', text, tables, url}；取到就入库，语义同 arxiv.fulltext。

    多版本论文最新版的 jats 路径可能 404，原始版本的路径更稳，两个都试。
    """
    aid = clean_id(pid)
    got = _cached(aid)
    if got is not None:
        return {k: got.get(k) for k in ("id", "title", "source", "text", "url", "tables", "retry_after")}
    entries = _rows(aid)
    latest = _latest(entries)
    if not latest:
        raise BiorxivError(f"bioRxiv 上没有 {aid}", 404)
    meta = latest[0]
    result = {"id": aid, "title": meta["title"], "source": "abstract",
              "text": meta.get("summary") or "", "url": meta.get("url") or "",
              "tables": []}
    earliest = sorted(entries, key=lambda p: int(p["version"]))[0]
    jats_urls = []
    for p in [meta, earliest]:  # 最新版优先，404 再退回最早版的原始路径
        if p.get("jats") and p["jats"] not in jats_urls:
            jats_urls.append(p["jats"])
    retry = RETRY_NO_XML
    for jats in jats_urls:
        try:
            xml_text = _web(jats).decode("utf-8", errors="replace")
            parsed = parse_jats(xml_text)
            if len(parsed["text"]) > 2000:
                result.update(source="html", text=parsed["text"],
                              tables=parsed.get("tables") or [])
            break
        except BiorxivError as exc:
            if exc.status == 404:
                continue  # 这个版本的路径真没有，退回最早版的路径再试
            retry = RETRY_TRANSIENT  # 限流、网络错、拿到报错页：换路径也一样，别再打
            break
    if result["source"] == "abstract":
        result["retry_after"] = time.time() + retry
    try:
        import store
        store.put_paper_text(aid, result)
    except Exception:
        pass
    return result
