# -*- coding: utf-8 -*-
"""arXiv：元数据、最新论文、全文（HTML 版），给研读层用。

- 只用 arXiv 官方公开接口和 arxiv.org/html 页面；不抓需要登录的东西。
- 官方要求连续请求间隔约 3 秒，这里串行加锁并缓存：元数据 6 小时，全文落盘长期复用。
- 全文取不到（老论文没有 HTML 版）就退回摘要，并在结果里写明 `source`，引文核对据此如实说明。
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Any

import logs
from limits import ReadLimitError, TTLCache, fetch
from singleflight import SingleFlight

API = "https://export.arxiv.org/api/query"
ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
UA = "QiyanResearchMentor/0.3 (education; contact via repo)"
_DEFAULT_CACHE = Path(__file__).resolve().parent / "data" / "papers"
CACHE_DIR = Path(os.environ.get("ARXIV_CACHE_DIR") or (
    "/tmp/papers" if os.environ.get("FC_FUNCTION_NAME") else _DEFAULT_CACHE))
META_TTL = 6 * 3600
_LOCK = threading.Lock()
_LAST_CALL = [0.0]
_META = TTLCache(META_TTL, 512)
MAX_ATOM_BYTES = 5 * 1024 * 1024
MAX_HTML_BYTES = 40 * 1024 * 1024  # 最长的 HTML 版论文（如 HELM）也在这个量级以内
_FLIGHT = SingleFlight()  # 同一 URL / 同一篇论文并发只取一次（限速锁下，重复请求每个都要排 3 秒）
ID_RE = re.compile(r"^\d{4}\.\d{4,5}$")
LOCAL_RE = re.compile(r"^local:[0-9a-f]{12}$")
_SEARCH = TTLCache(3600, 256)
_SEARCH_FAIL = TTLCache(30, 64)
ZH_RE = re.compile(r"[\u4e00-\u9fff]")


class ArxivError(Exception):
    def __init__(self, msg: str, status: int = 0) -> None:
        super().__init__(msg)
        self.status = status  # HTTP 状态码；网络错误、超时为 0


def is_local(raw: str) -> bool:
    return bool(LOCAL_RE.match((raw or "").strip()))


def clean_id(raw: str) -> str:
    """接受 2310.17623、2310.17623v2、arxiv.org/abs/… 等写法，返回不带版本号的 id。"""
    s = (raw or "").strip()
    s = re.sub(r"^https?://(www\.)?arxiv\.org/(abs|pdf|html)/", "", s)
    s = re.sub(r"\.pdf$", "", s)
    s = re.sub(r"v\d+$", "", s)
    if not ID_RE.match(s):
        raise ArxivError("不是有效的 arXiv 编号（形如 2310.17623）", 400)
    return s


def _get(url: str, timeout: int = 20, max_bytes: int = MAX_ATOM_BYTES) -> bytes:
    """timeout 是总时限（连上、收完一起算），不只是两次收到数据之间的间隔；拿着限速锁的请求不能被慢速响应无限拖住。"""
    with _LOCK:  # 官方要求慢一点：同一时间只发一个请求，间隔 ≥3 秒
        wait = 3.0 - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            body = fetch(req, timeout=timeout, max_bytes=max_bytes)  # 总时限含响应头：拿着限速锁的请求最多占 timeout 秒
        except urllib.error.HTTPError as exc:
            raise ArxivError(f"arXiv 返回 HTTP {exc.code}", exc.code) from exc
        except ReadLimitError as exc:
            raise ArxivError(f"arXiv 响应{exc}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ArxivError("连不上 arXiv") from exc
        finally:
            _LAST_CALL[0] = time.time()
    return body


def _entries(xml_bytes: bytes) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root.findall("a:entry", ATOM):
        raw_id = (e.findtext("a:id", "", ATOM) or "").rsplit("/", 1)[-1]
        title = re.sub(r"\s+", " ", e.findtext("a:title", "", ATOM)).strip()
        if not raw_id or title == "Error":
            continue
        clean_id = re.sub(r"v\d+$", "", raw_id)
        out.append({
            "id": clean_id,
            "title": title,
            "summary": re.sub(r"\s+", " ", e.findtext("a:summary", "", ATOM)).strip(),
            "authors": [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)],
            "published": (e.findtext("a:published", "", ATOM) or "")[:10],
            "updated": (e.findtext("a:updated", "", ATOM) or "")[:10],
            "categories": [c.get("term") for c in e.findall("a:category", ATOM)],
            "url": f"https://arxiv.org/abs/{clean_id}",
        })
    return out


def query(params: dict[str, Any], cached_only: bool = False) -> list[dict[str, Any]] | None:
    """cached_only=True：只看缓存，没有就返回 None，绝不打 arXiv（给「缓存命中不排队」的快路径用）。"""
    url = API + "?" + urllib.parse.urlencode(params)

    def load() -> list[dict[str, Any]]:
        hit, data = _META.lookup(url)
        if hit:
            return data
        try:
            data = _entries(_get(url))
        except ET.ParseError as exc:
            raise ArxivError("arXiv 返回的不是 Atom") from exc
        _META.set(url, data)
        return data

    hit, data = _META.lookup(url)
    if hit:
        return data
    if cached_only:
        return None
    return _FLIGHT.do(("query", url), load)


def count(search_query: str) -> int:
    """命中总数（opensearch:totalResults），不取条目。给「势头」用。"""
    # max_results=0 会让 arXiv 返回 500，取 1 条
    url = API + "?" + urllib.parse.urlencode({"search_query": search_query, "max_results": 1})
    try:
        root = ET.fromstring(_FLIGHT.do(("count", url), lambda: _get(url)))
    except ET.ParseError as exc:
        raise ArxivError("arXiv 返回的不是 Atom") from exc
    total = root.findtext("{http://a9.com/-/spec/opensearch/1.1/}totalResults")
    if total is None:
        raise ArxivError("arXiv 没有返回总数")
    return int(total)


def papers(ids: list[str]) -> list[dict[str, Any]]:
    ids = [clean_id(i) for i in ids]
    if not ids:
        return []
    import datacite
    cached = [datacite.cached(aid) for aid in ids]
    if all(cached):
        return cached
    try:
        return query({"id_list": ",".join(ids), "max_results": len(ids)})
    except ArxivError as original:
        try:
            return [p for aid in ids if (p := datacite.paper(aid))]
        except Exception:
            raise original


_CAT_OK = re.compile(r"^[a-z\-]+(\.[A-Za-z\-]+)?$")
_KW_OK = re.compile(r"^[A-Za-z0-9 .\-]+$")


def recent(categories: list[str], keywords: list[str], max_results: int = 30, cached_only: bool = False) -> list[dict[str, Any]] | None:
    """某些分类下最近提交、标题或摘要命中关键词的论文，按提交时间倒序。"""
    categories = [c for c in categories if _CAT_OK.match(c or "")]
    keywords = [k for k in keywords if _KW_OK.match(k or "")]
    if not categories:
        return [] if not cached_only else None
    cats = " OR ".join(f"cat:{c}" for c in categories)
    kws = " OR ".join(f'abs:"{k}"' if " " in k else f"abs:{k}" for k in keywords)
    q = f"({cats}) AND ({kws})" if kws else cats
    return query({"search_query": q, "sortBy": "submittedDate", "sortOrder": "descending", "max_results": max_results}, cached_only)


# ---------- 全文 ----------

_DROP = re.compile(r"<(script|style|math|svg|nav|header|footer)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BLOCK_NO_TABLE = re.compile(
    r"</?(p|div|section|h[1-6]|li|br|figcaption|caption)[^>]*>", re.I
)
_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.S | re.I)


def _cell_text(cell: str) -> str:
    cell = re.sub(r"<math[^>]*alttext=\"([^\"]*)\"[^>]*>.*?</math>",
                  lambda m: f" {html.unescape(m.group(1))} ", cell, flags=re.S)
    cell = _DROP.sub(" ", cell)
    text = html.unescape(_TAG.sub(" ", cell))
    return re.sub(r"\s+", " ", text).strip()


def _headerish(row: list[str]) -> bool:
    """没有数字的首行，多半是表头。latexml 常用 <td> 写表头，不能只认 <th>。"""
    return bool(row) and not any(re.search(r"\d", c or "") for c in row)


def _table_to_rows(raw: str) -> list[list[str]]:
    rows = []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", raw, flags=re.S | re.I):
        cells = re.findall(r"<(?:th|td)\b[^>]*>.*?</(?:th|td)>", tr, flags=re.S | re.I)
        row = [_cell_text(c) for c in cells]
        if any(row):
            rows.append(row)
    return rows


def parse_html(page: str) -> dict:
    """正文 + 表格。表格从原文抽出后换成 [[table:tN]]，避免再被摊成一行字。"""
    start = page.find("<article")
    if start >= 0:
        page = page[start:]
    tables: list[dict] = []
    n = 0

    def _repl(m: re.Match) -> str:
        nonlocal n
        n += 1
        tid = f"t{n}"
        raw = m.group(0)
        rows = _table_to_rows(raw)
        caption = ""
        cap = re.search(r"<caption\b[^>]*>(.*?)</caption>", raw, flags=re.S | re.I)
        if cap:
            caption = _cell_text(cap.group(1))
        first = re.search(r"<tr\b[^>]*>.*?</tr>", raw, flags=re.S | re.I)
        headers: list[str] = []
        body = rows
        if first and re.search(r"<th\b", first.group(0), re.I) and rows:
            headers = rows[0]
            body = rows[1:]
        elif len(rows) >= 2 and _headerish(rows[0]) and any(not _headerish(r) for r in rows[1:]):
            headers = rows[0]
            body = rows[1:]
        if not rows:
            return "\n"
        tables.append({"id": tid, "caption": caption, "headers": headers, "rows": body, "at": 0})
        return f"\n[[table:{tid}]]\n"

    page = _TABLE_RE.sub(_repl, page)
    page = re.sub(
        r"<math[^>]*alttext=\"([^\"]*)\"[^>]*>.*?</math>",
        lambda m: f" {html.unescape(m.group(1))} ",
        page,
        flags=re.S,
    )
    page = _DROP.sub(" ", page)
    page = _BLOCK_NO_TABLE.sub("\n", page)
    page = re.sub(r"</?(?:table|tr|th|td)[^>]*>", "\n", page, flags=re.I)
    text = html.unescape(_TAG.sub(" ", page))
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    for t in tables:
        needle = f"[[table:{t['id']}]]"
        pos = text.find(needle)
        t["at"] = pos if pos >= 0 else 0
    return {"text": text, "tables": tables}


def _html_to_text(page: str) -> str:
    return parse_html(page)["text"]


# 只拿到摘要时，什么时候再试一次 HTML 正文：确实没有 HTML 版（404 或正文太短）隔一周；网络错误、超时、5xx 十分钟
RETRY_NO_HTML = 7 * 24 * 3600
RETRY_TRANSIENT = 10 * 60


_RECENT = TTLCache(60, 16)  # 刚读过的几篇：一次交卡要取三次原文，不必每次都从磁盘读、解析几百 KB 的 JSON
CACHE_MAX_FILES = int(os.environ.get("ARXIV_CACHE_MAX_FILES", "2000"))
CACHE_MAX_BYTES = int(os.environ.get("ARXIV_CACHE_MAX_BYTES", str(512 * 1024 * 1024)))
_QUOTA_LOCK = threading.Lock()
_QUOTA: dict[str, Any] = {"dir": None, "sizes": {}, "bytes": 0}  # 本进程记的缓存目录文件数、总字节；换目录或首次用时扫一遍


def search(q: str, max_results: int = 20) -> list[dict[str, Any]]:
    raw = (q or "").strip()
    if ZH_RE.search(raw):
        raise ArxivError("arXiv 只认英文关键词", 400)
    try:
        aid = clean_id(raw)
        return papers([aid])
    except ArxivError:
        pass
    toks = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,30}", raw) if t.lower() not in {"and", "or", "andnot"}][:6]
    if not toks or not any(len(t) >= 2 for t in toks):
        raise ArxivError("arXiv 只认英文关键词", 400)
    key = tuple(t.lower() for t in toks)
    hit, data = _SEARCH.lookup(key)
    if hit:
        return data
    fail, ferr = _SEARCH_FAIL.lookup(key)
    if fail:
        raise ArxivError(str(ferr or "搜索暂时失败"))
    params = {
        "search_query": " AND ".join(f"all:{t}" for t in toks),
        "start": 0, "max_results": max_results,
        "sortBy": "relevance", "sortOrder": "descending",
    }
    try:
        data = query(params) or []
    except ArxivError as exc:
        import datacite
        try:
            data = datacite.search(raw, max_results)
        except Exception:
            _SEARCH_FAIL.set(key, str(exc))
            raise exc
    _SEARCH.set(key, data)
    return data


def _on_disk(aid: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{aid}.json"
    hit, got = _RECENT.lookup(str(path))  # 按文件路径记，换了缓存目录（测试里常换）就不会串
    if hit:
        return got
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # 坏文件（比如写到一半进程被杀）当作没有缓存，重新取一次会覆盖它
    _RECENT.set(str(path), got)
    return got


def _from_store(aid: str) -> dict[str, Any] | None:
    try:
        import store
        row = store.get_paper_text(aid)
    except Exception:
        return None
    if not row:
        return None
    return {k: row[k] for k in row if k in ("id", "title", "source", "text", "url", "tables", "retry_after")}


def _load(aid: str) -> dict[str, Any] | None:
    # 以库为准：磁盘只在 DB 缺行时回退。旧摘要 JSON 不能盖掉已入库的 html/pdf。
    db = _from_store(aid)
    disk = _on_disk(aid)
    if db:
        if disk and disk.get("source") == db.get("source") and disk.get("text") == db.get("text"):
            return disk
        return db
    if disk:
        try:
            import store
            if not store.get_paper_text(aid):
                store.put_paper_text(aid, disk)
        except Exception:
            pass
        return disk
    return None


def _save(aid: str, result: dict[str, Any]) -> None:
    try:
        import store
        store.put_paper_text(aid, result)
    except Exception:
        pass
    _save_disk(aid, result)


def _save_disk(aid: str, result: dict[str, Any]) -> None:
    """先写临时文件再原子替换：别的请求读到的要么是旧版、要么是新版，不会是写了一半的。"""
    path = CACHE_DIR / f"{aid}.json"
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    body = json.dumps(result, ensure_ascii=False)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logs.emit({"warn": "paper_cache_write", "aid": aid})
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _RECENT.set(str(path), result)
        return
    _RECENT.set(str(path), result)
    _enforce_quota(path, len(body.encode("utf-8")))


def _enforce_quota(path: Path, size: int) -> None:
    """每篇论文落一个文件、从不删，线上磁盘会无限涨：超过文件数或总字节上限就按修改时间删最旧的，刚写的那篇不删。"""
    with _QUOTA_LOCK:
        q = _QUOTA
        if q["dir"] != CACHE_DIR:
            sizes = {}
            for p in CACHE_DIR.glob("*.json"):
                try:
                    sizes[p.name] = p.stat().st_size
                except FileNotFoundError:
                    pass
            q.update(dir=CACHE_DIR, sizes=sizes, bytes=sum(sizes.values()))
        else:
            q["bytes"] += size - q["sizes"].get(path.name, 0)
            q["sizes"][path.name] = size
        if len(q["sizes"]) <= CACHE_MAX_FILES and q["bytes"] <= CACHE_MAX_BYTES:
            return
        files = []
        for p in CACHE_DIR.glob("*.json"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            files.append((st.st_mtime, p.name, p, st.st_size))
        files.sort(key=lambda f: (f[0], f[1]))
        sizes = {name: s for _, name, _, s in files}
        total = sum(sizes.values())
        for _, name, p, s in files:
            if len(sizes) <= CACHE_MAX_FILES and total <= CACHE_MAX_BYTES:
                break
            if name == path.name:
                continue
            try:
                p.unlink()
            except FileNotFoundError:
                pass  # 别的进程刚删掉
            del sizes[name]
            total -= s
        q.update(sizes=sizes, bytes=total)


def _needs_reparse(got: dict[str, Any]) -> bool:
    """抽表之前写下的 HTML 正文：没有 tables 字段，表格被摊成一行行字。"""
    return got.get("source") == "html" and "tables" not in got


def _cached(aid: str) -> dict[str, Any] | None:
    """磁盘缓存：正文版一直有效；只有摘要的到了重试时间就当没有（旧缓存没有重试时间，也当到期）。
    抽表之前的正文在用户路径照常返回，带 needs_reparse，重取交给预热。"""
    got = _load(aid)
    if not got:
        return None
    if got.get("source") == "abstract" and time.time() >= got.get("retry_after", 0):
        return None
    if _needs_reparse(got):
        return {**got, "needs_reparse": True}
    return got


def cached(arxiv_id: str) -> dict[str, Any] | None:
    """只看缓存（内存或磁盘），没有或已到重试时间就返回 None，绝不打 arXiv。"""
    return _cached(clean_id(arxiv_id))


def _title_from_html(page: str) -> str:
    block = re.search(r'class="ltx_title_document"[^>]*>(.*?)</(?:h1|div|p)>', page, re.S | re.I)
    raw = block.group(1) if block else ""
    if not raw:
        cap = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        raw = cap.group(1) if cap else ""
    text = html.unescape(_TAG.sub(" ", raw))
    return re.sub(r"\s+", " ", text).strip()[:300]


def fulltext(arxiv_id: str, hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """{id, source: 'html' | 'abstract', text, title}；全文落盘缓存。hint 有标题/摘要时省元数据。"""
    aid = clean_id(arxiv_id)
    got = _cached(aid)
    if got is not None:
        return got
    return _FLIGHT.do(("fulltext", aid), lambda: _fetch_fulltext(aid, hint))


def _fetch_fulltext(aid: str, hint: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        logs.emit({"warn": "paper_cache_write", "msg": "mkdir"})
    got = _cached(aid)
    if got is not None:
        return got
    stale = _on_disk(aid)
    if stale:
        result = {k: stale[k] for k in ("id", "title", "source", "text", "url") if k in stale}
        if stale.get("tables"):
            result["tables"] = stale["tables"]
    elif hint and (hint.get("title") or hint.get("summary")):
        result = {"id": aid, "title": hint.get("title") or aid, "source": "abstract",
                  "text": hint.get("summary") or "", "url": f"https://arxiv.org/abs/{aid}", "tables": []}
    else:
        result = {"id": aid, "title": aid, "source": "abstract", "text": "",
                  "url": f"https://arxiv.org/abs/{aid}", "tables": []}
    retry = RETRY_NO_HTML
    html_ok = False
    try:
        page = _get(f"https://arxiv.org/html/{aid}", timeout=30, max_bytes=MAX_HTML_BYTES).decode("utf-8", errors="replace")
        if "</html>" not in page[-4096:].lower():
            raise ArxivError("arXiv 正文页不完整")
        parsed = parse_html(page)
        text = parsed["text"]
        if len(text) > 2000:
            result.update(source="html", text=text, tables=parsed.get("tables") or [],
                          title=_title_from_html(page) or result.get("title") or aid)
            result.pop("retry_after", None)
            html_ok = True
    except ArxivError as exc:
        if exc.status != 404:
            retry = RETRY_TRANSIENT
    if not html_ok and not (result.get("text") or "").strip() and not (hint and hint.get("summary")):
        meta = papers([aid])
        if not meta:
            raise ArxivError(f"arXiv 上没有 {aid}")
        m = meta[0]
        result.update(title=m["title"], text=m["summary"], url=m["url"], source="abstract", tables=[])
    if result["source"] == "abstract":
        result["retry_after"] = time.time() + retry
    _save(aid, result)
    return result


_SEC_NAMES = (
    r"Abstract|Introduction|Related Work|Background|Method[s]?|Approach|"
    r"Experiments?|Results?|Discussion|Analysis|Limitations?|Future Work|"
    r"Conclusions?|Broader Impact|Ethics Statement|Acknowledg(?:e)?ments?|"
    r"References|Appendix"
)
_SEC_LOOSE = re.compile(rf"^(?:\d+(?:\.\d+)*\s+)?({_SEC_NAMES})\b.*$", re.M | re.I)
_SEC_STRICT = re.compile(
    rf"^(\d+(?:\.\d+)*)\.?\s+({_SEC_NAMES})\b.{{0,40}}$", re.M | re.I)


def sections(text: str, strict: bool = False) -> list[tuple[str, int]]:
    """粗分节：返回 [(标题, 起始位置)]。PDF 用 strict：编号 + 首字母大写关键词 + 行长 ≤60。"""
    return list(_sections(text, strict))


@lru_cache(maxsize=16)
def _sections(text: str, strict: bool = False) -> tuple[tuple[str, int], ...]:
    """同一篇全文每张卡要分好几次节（每句引文一次），只算一次。"""
    out = []
    pat = _SEC_STRICT if strict else _SEC_LOOSE
    for m in pat.finditer(text):
        name = m.group(2 if strict else 1).lower()
        if strict and (m.end() - m.start()) > 60:
            continue
        out.append((name, m.start()))
    return tuple(out)


SECTION_CN = {"abstract": "摘要", "introduction": "引言", "related work": "相关工作", "background": "背景",
              "method": "方法", "methods": "方法", "approach": "方法", "experiment": "实验", "experiments": "实验",
              "result": "结果", "results": "结果", "discussion": "讨论", "analysis": "分析", "limitation": "局限",
              "limitations": "局限", "future work": "未来工作", "conclusion": "结论", "conclusions": "结论",
              "broader impact": "更广的影响", "ethics statement": "伦理声明", "references": "参考文献", "appendix": "附录"}


def section_cn(name: str) -> str:
    return SECTION_CN.get(name, "致谢" if name.startswith("acknowledg") else name)


def section_at(text: str, pos: int) -> str:
    name = ""
    for title, start in sections(text):
        if start <= pos:
            name = title
        else:
            break
    return name
