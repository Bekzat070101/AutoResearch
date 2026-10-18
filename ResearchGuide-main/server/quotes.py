# -*- coding: utf-8 -*-
"""引文定位：学生（或学生的 Agent）写的一句引文，是不是逐字出自原文、出自哪一节。

只做「规范化后的逐字匹配」，不做模糊匹配：差一个词就算找不到。
规范化只抹掉排版噪声：连字、弯引号、断行连字符、多余空白、全半角标点。
"""
from __future__ import annotations

import re
import threading
import unicodedata
from array import array
from collections import OrderedDict
from typing import Any, Iterable

import arxiv
from singleflight import SingleFlight

_LIG = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
_PUNCT = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "−": "-",
          " ": " ", "，": ",", "。": ".", "：": ":", "；": ";", "（": "(", "）": ")", "「": '"', "」": '"'}
MIN_LEN = 12


def _units(text: str):
    """逐段产出 (规范化后的一个字, 它在原文里的位置)。引文和论文都走这一条路，两边规范化结果一致。
    一段 = 一个字 + 后面跟着的组合符号（以及韩文的中声、终声字母），整段做 NFKC，才能把分开写的字合起来；
    转小写也逐字做：「İ」转小写是两个字，每个字都要有位置，否则后面的位置全部错开。"""
    n, i = len(text), 0
    while i < n:
        j = i + 1
        while j < n and not text[j].isascii() and (unicodedata.combining(text[j]) or "\u1160" <= text[j] <= "\u11ff"):
            j += 1
        chunk = text[i:j]
        for ch in (chunk if chunk.isascii() else unicodedata.normalize("NFKC", chunk)):
            for c in _LIG.get(ch, _PUNCT.get(ch, ch)):
                for low in c.lower():
                    yield low, i
        i = j


def norm(s: str) -> str:
    s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s or "")  # 断行连字符
    s = "".join(c for c, _ in _units(s))
    return re.sub(r"\s+", " ", s).strip()


def _index(text: str) -> tuple[str, array]:
    """规范化全文，同时记下规范化后每个字符对应「原文」的位置，用来报告章节。规范化和 norm() 走同一条路。"""
    out, pos = [], array("I")
    for c, i in _units(text):
        if c.isspace():
            if out and out[-1] == " ":
                continue
            c = " "
        out.append(c)
        pos.append(i)
    return "".join(out), pos


# 按总字数留，不按篇数：几篇不同的论文同时交卡时，提前建好的索引不能在评阅前就被挤掉；
# 每个字约占 6 字节（规范化文本两份加位置表），八百万字约 50 MB 封顶，最近用过的那篇总会留下
PREPARED_BUDGET = 8_000_000
_PREPARED: OrderedDict[str, tuple[str, array, str, array]] = OrderedDict()
_PREPARED_LOCK = threading.Lock()
_PREPARING = SingleFlight()  # 二十几张卡同时交同一篇论文：只建一次，其余等它（lru_cache 只缓存建好的，不合并正在建的）


def _prepared(text: str) -> tuple[str, array, str, array]:
    """一篇论文只规范化一次：一张卡要定位六七句，原来每句都把全文重做一遍。
    以全文字符串为键（Python 会缓存字符串的哈希）；总字数超过预算时先丢最久没用的。"""
    with _PREPARED_LOCK:
        hit = _PREPARED.get(text)
        if hit is not None:
            _PREPARED.move_to_end(text)
            return hit
    return _PREPARING.do(text, lambda: _prepare(text))


def _prepare(text: str) -> tuple[str, array, str, array]:
    with _PREPARED_LOCK:
        hit = _PREPARED.get(text)
    if hit is not None:  # 排在前面的那一个刚建好
        return hit
    hay, pos = _index(text)
    hay2, pos2, last = [], array("I"), 0
    for m in re.finditer(r"(\w)- (\w)", hay):  # 去断行连字符时位置表跟着删掉同样两格：否则跨行断词的引文找得到却对不回原文位置，报不出章节
        cut = m.end(1)
        hay2.append(hay[last:cut])
        pos2.extend(pos[last:cut])
        last = cut + 2
    got = (hay, pos, "".join(hay2) + hay[last:], pos2 + pos[last:]) if last else (hay, pos, hay, pos)
    with _PREPARED_LOCK:
        _PREPARED[text] = got
        while len(_PREPARED) > 1 and sum(len(k) for k in _PREPARED) > PREPARED_BUDGET:
            _PREPARED.popitem(last=False)
    return got


def prepare(text: str) -> None:
    """提前建好索引（交卡入口在事件循环里共等这一步）。"""
    _prepared(text)


def clear_prepared() -> None:
    with _PREPARED_LOCK:
        _PREPARED.clear()


MAX_OCCURRENCES = 64  # 同一句在全文里出现的次数，看这么多处就够了


def locate(quote: str, paper: dict[str, Any], prefer: Iterable[str] | None = None,
           within: tuple[int, int] | None = None) -> dict[str, Any]:
    """返回 {found, section, where, reason, at}。paper 是 arxiv.fulltext() 的结果。
    prefer：想要它出自哪些章节（局限要出自局限段）。同一句在全文出现多次时，有一处在这些章节里就报那一处；
    原来只看第一次出现：引言里先出现过一次，真在局限段里的引文也被判成「出自引言」（Codex 复现）。
    within=(start, end)：只认落在这段原文偏移里的那一处（带读按站判题用）；at 是命中处的原文偏移。"""
    q = norm(quote).strip(' "\'')
    if len(q) < MIN_LEN:
        return {"found": False, "reason": f"引文太短（至少 {MIN_LEN} 个字符），没法当证据", "at": None}
    hay, pos, hay2, pos2 = _prepared(paper.get("text") or "")
    wanted = set(prefer or ())
    first: tuple[str | None, int] | None = None
    anywhere = False
    for h, p in ((hay, pos), (hay2, pos2)) if hay2 != hay else ((hay, pos),):
        i, seen = h.find(q), 0
        while i >= 0 and seen < MAX_OCCURRENCES:
            anywhere = True
            src_pos = p[i] if i < len(p) else 0
            if within is None or within[0] <= src_pos < within[1]:
                sec = arxiv.section_at(paper["text"], src_pos)
                if first is None:
                    first = (sec, src_pos)
                if not wanted or sec in wanted:
                    return _located(sec, paper, src_pos)
            i, seen = h.find(q, i + 1), seen + 1
        if first is not None and not wanted:
            break
    if first is None:
        if anywhere and within is not None:
            return {"found": False, "reason": "这句不在这一节", "at": None}
        return {"found": False, "reason": "原文里找不到这句" + ("（只拿到了摘要，正文核对不了）" if paper.get("source") == "abstract" else ""), "at": None}
    return _located(first[0], paper, first[1])


def _located(sec: str | None, paper: dict[str, Any], src_pos: int | None = None) -> dict[str, Any]:
    where = arxiv.section_cn(sec) if sec else ("摘要" if paper.get("source") == "abstract" else "正文")
    return {"found": True, "section": sec, "where": where, "reason": "", "at": src_pos}
