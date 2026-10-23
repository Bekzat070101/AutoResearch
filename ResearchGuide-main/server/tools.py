# -*- coding: utf-8 -*-
"""工具 registry：模型只能调用这里的工具，且**全部只读**。

契约见 docs/DIALOGUE_CONTRACT.md §5。

为什么不给模型写入型工具：
  写记忆走 memory_ops（经 §7 确定性校验），写行动走 next_action（经代码执行）。
  工具只负责「看一眼世界」，失败就如实返回 ok=false，绝不假装完成。
"""
from __future__ import annotations

from typing import Any, Callable

import curriculum
import mcp_curriculum
import store
import reading_memory
import transcript
from pku_adapter import search_courses

# ---------- 工具定义 ----------
# 每个工具：说明（进 prompt）+ 参数 + 执行函数。参数只做必要校验，多余的一律丢弃。

TOOL_SPECS: dict[str, dict[str, Any]] = {
    "reading.search": {
        "desc": "检索当前画像已通过阅读卡的文献和阅读笔记。问读过什么或提到已读论文时用；query 为空列最近已读。",
        "params": {"query": "可选，论文名、论文ID或笔记关键词", "limit": "可选，1-20，默认5"},
    },
    "reading.detail": {
        "desc": "读取当前画像某篇已读文献的通过卡片、笔记与已核对引文。不能读取其他画像或尚未通过的论文。",
        "params": {"paper_id": "reading.search 返回的 paper_id"},
    },
    "course.search": {
        "desc": "检索北大公开课。用户问到「有没有课」「该上什么课」时用。失败会返回 ok=false，照实说。",
        "params": {"query": "检索词，如「机器学习」「社会心理学」", "limit": "可选，1-10，默认 5"},
    },
    "transcript.summary": {
        "desc": ("查他的成绩单。**默认只给汇总**（总绩点/学分/分学期），"
                 "不整份倒给你；要具体某门课或某类课，传 keyword。"
                 "用户问到「我学过什么」「成绩怎么样」「我修过哪些数学课」时用。"
                 "没有成绩单会如实说没有，别猜。"),
        "params": {"keyword": "可选。课程名里的关键词，如「数学」「Python」。传了才回具体课程",
                   "limit": "可选，1-50。只有传 keyword 时才起作用"},
    },
}

def _as_int(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def _as_text(raw: Any, limit: int = 80) -> str:
    return str(raw or "").strip()[:limit]


# ---------- 执行 ----------


def _course_search(uid: str, args: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    query = _as_text(args.get("query"), 60)
    query_from = "model"
    if not query:
        # 模型偶尔会漏传 query。退回用户这句话当检索词，比直接报错浪费一整轮要好；
        # 仍然如实标明实际用了什么词、从哪来的，不假装是模型指定的。
        query = _as_text((context or {}).get("query_hint"), 60)
        if query:
            query_from = "user_message"
    if not query:
        return {"ok": False, "error": "query is required"}
    limit = _as_int(args.get("limit"), 5, 1, 10)
    res = search_courses(query, limit=limit)
    if not res.get("ok"):
        # 检索失败如实返回，不编造兜底课程（产品三原则）
        return {"ok": False, "error": res.get("error") or "课程检索失败",
                "query": query, "query_from": query_from}
    items = [
        {"name": _as_text(i.get("name") or i.get("title"), 60),
         "teacher": _as_text(i.get("teacher"), 40),
         "term": _as_text(i.get("term"), 20),
         "credits": i.get("credits")}
        for i in (res.get("items") or [])[:limit]
    ]
    return {"ok": True, "query": query, "query_from": query_from, "term": res.get("term"),
            "retrieved_at": res.get("retrieved_at"), "items": items}


def _transcript_summary(uid: str, args: dict[str, Any]) -> dict[str, Any]:
    """查成绩单。

    刻意不回整份成绩单：一个人 20~40 门课，全倒进上下文既挤掉别的东西，
    又逼着模型在几十条里自己找。**默认只给汇总**，要哪门课就带 keyword 来查。
    这一条和记忆注入的 P3 优先级是同一个决定（见 06-信息分类评审.md §四）。
    """
    rows = store.list_enrollments(uid)
    if not rows:
        return {"ok": True, "has_transcript": False,
                "note": "这个画像还没录成绩单。可以请他粘贴一次，或先不问。"}
    courses = [{"course": r["course"], "grade": r["grade"],
                "credits": float(r["credits"] or 0), "term": r["term"],
                "kind": r["kind"], "status": r["status"]} for r in rows]
    summary = transcript.summarize(courses)
    by_term = transcript.summarize_by_term(courses)

    kw = _as_text(args.get("keyword"), 40)
    if kw:
        hit = [c for c in courses if kw in c["course"]]
        limit = _as_int(args.get("limit"), 12, 1, 50)
        return {"ok": True, "has_transcript": True, "keyword": kw,
                "matched_count": len(hit), "matched": hit[:limit],
                "summary": summary,
                "note": ("没有匹配的课" if not hit else
                         f"共 {len(hit)} 门含「{kw}」，只回了前 {min(limit, len(hit))} 门")}

    return {"ok": True, "has_transcript": True, "summary": summary,
            "by_term": by_term,
            "note": "只回了汇总。要看具体课程，带 keyword 再查一次。"}


# ---------- 知识库工具（院系-专业培养方案） ----------
# 定义只有一份，在 server/mcp_curriculum.py 的 TOOLS 里（外部 MCP 客户端读的是它）。
# 这里把同一份定义改写成对话 prompt 要的文字形式，避免同一个能力维护两套说明 ——
# 两边漂移的代价是模型照着旧说明调用，结果查不到。
#
# 对话里不给 kb_stats：它是给外部客户端确认服务健康的，模型没有用得上的场合。

_DIALOGUE_KB_TOOLS = ("major_lookup", "major_detail", "match_transcript",
                      "minor_programs", "course_lookup")

# 卡片里给检索用的同义词表：对搜索有用，对回答没用，回给模型只是白占上下文。
_SLIM_DROP = ("检索别名",)


def _slim_cards(data: dict[str, Any]) -> dict[str, Any]:
    """把命中卡片里的检索用字段去掉（不动原始数据，只动这一份返回）。"""
    for c in data.get("命中") or []:
        for k in _SLIM_DROP:
            c.pop(k, None)
    card = data.get("卡片")
    if isinstance(card, dict):
        for k in _SLIM_DROP:
            card.pop(k, None)
    return data


def _kb_tool(name: str, uid: str, args: dict[str, Any],
             context: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个知识库工具。参数缺了就按契约返回 ok=false，不替用户猜。"""
    if name == "major_lookup":
        query = _as_text(args.get("query"), 60)
        query_from = "model"
        if not query:
            # 和 course.search 同一条纪律：模型漏传时用用户这句话兜，并如实标明来源
            query, query_from = _as_text((context or {}).get("query_hint"), 60), "user_message"
        if not query:
            return {"ok": False, "error": "query is required", "命中": []}

        # 整句当 query 是模型最常犯的错（「信管有哪些专业分流」）。先照原样查一次；
        # 没命中就从原话里把库内的专业/院系名抠出来再查一次，query 里如实写实际用了什么。
        original = query
        data = mcp_curriculum.run_tool(name, {**args, "query": query})
        if data.get("kind") == "未命中":
            found = curriculum.mentions(f"{query} {(context or {}).get('query_hint') or ''}")
            if found:
                retry = mcp_curriculum.run_tool(name, {**args, "query": found[0]})
                if retry.get("命中"):
                    retry["query_original"] = original
                    retry["query_from"] = query_from
                    return _slim_cards(retry)
        data["query_from"] = query_from
        return _slim_cards(data)

    if name == "major_detail":
        return _slim_cards(mcp_curriculum.run_tool(name, args))

    if name == "match_transcript":
        raw = args.get("codes")
        if isinstance(raw, str):
            raw = [raw]
        given = [str(x) for x in (raw or []) if str(x).strip()]
        codes = curriculum.parse_codes(given)          # 里面本来就混着课号时直接取出来
        source = "model"
        notes: list[str] = []

        if given and not codes:
            # 用户说的是课名（「我学过高等数学、线代」），模型多半原样传进来。
            # 课名 → 课号由代码查索引决定，认不出的如实列出来，不模糊猜。
            looked = curriculum.codes_for_names(given)
            codes = looked["codes"]
            source = "model_names"
            notes.append(f"他给的是课名：{len(given)} 门，对上课程索引的 {len(looked['matched'])} 门"
                         f"（共 {len(codes)} 个课号）")
            if looked["missed"]:
                notes.append("这些课名索引里没有、这次不算数：" + "、".join(looked["missed"][:8])
                             + ("…" if len(looked["missed"]) > 8 else ""))

        if not codes and not given:
            # 模型什么也没给：已经录过成绩单就直接用库里的，不用它把课号抄一遍。
            rows = store.list_enrollments(uid)
            names = [r.get("course") for r in rows if r.get("course")]
            source = "transcript"
            if not names:
                return {"ok": True, "has_transcript": False, "source": source,
                        "院系排名": [], "专业排名": [],
                        "note": "他还没录成绩单，也没有给出课号。可以请他粘贴一次，或先按专业名查。"}
            looked = curriculum.codes_for_names(names)
            codes = looked["codes"]
            notes.append(f"成绩单里 {len(names)} 门课，课名对上知识库课程索引的 {len(looked['matched'])} 门"
                         f"（共 {len(codes)} 个课号）")
            if looked["missed"]:
                notes.append("这些课名索引里没有、这次不算数：" + "、".join(looked["missed"][:8])
                             + ("…" if len(looked["missed"]) > 8 else ""))

        if not codes:
            # 给了课名但一条都对不上：不许猜一个相近的课号
            return {"ok": True, "source": source, "识别到课号": 0,
                    "院系排名": [], "专业排名": [],
                    "note": "这些课名一条都没对上知识库的课程索引，认不出专业。"
                            "如实说这次认不出来，别说一个像的。" + "；".join(notes)}

        data = mcp_curriculum.run_tool(name, {**args, "codes": codes})
        data["source"] = source
        if notes:
            data["note"] = "；".join(notes + ([data["note"]] if data.get("note") else []))
        return data

    return mcp_curriculum.run_tool(name, args)


_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "reading.search": lambda uid, args: reading_memory.search(uid, _as_text(args.get("query"), 200), _as_int(args.get("limit"), 5, 1, 20)),
    "reading.detail": lambda uid, args: reading_memory.detail(uid, _as_text(args.get("paper_id"), 160)),
    "course.search": _course_search,
    "transcript.summary": _transcript_summary,
}
for _t in mcp_curriculum.TOOLS:
    if _t["name"] not in _DIALOGUE_KB_TOOLS:
        continue
    _name = _t["name"]
    _props = (_t.get("inputSchema") or {}).get("properties") or {}
    TOOL_SPECS[_name] = {
        "desc": _t["description"],
        # 参数说明用工具表自己的 description：模型看到的就是 MCP 客户端看到的那份
        "params": {k: (v.get("description") or v.get("type") or "") for k, v in _props.items()},
    }
    _HANDLERS[_name] = (lambda n: lambda uid, args, context=None: _kb_tool(n, uid, args, context))(_name)

_NEEDS_CONTEXT = {"course.search", "major_lookup"}


def run(uid: str, intent: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行一个工具意图。未登记的工具名或执行异常都返回结构化失败，不抛给上层。

    context 只用于补全缺失的必填参数（例如用户这句话本身），不是给工具「猜」答案用的。
    """
    name = _as_text((intent or {}).get("tool"), 40)
    if name not in _HANDLERS:
        return {"ok": False, "tool": name, "error": "unknown tool"}
    args = (intent or {}).get("args")
    if not isinstance(args, dict):
        args = {}
    try:
        if name in _NEEDS_CONTEXT:
            result = _HANDLERS[name](uid, args, context)
        else:
            result = _HANDLERS[name](uid, args)
    except Exception as exc:  # 工具失败不能中断整轮对话
        return {"ok": False, "tool": name, "error": f"{type(exc).__name__}: {exc}"}
    return {"tool": name, "args": args, **result}


def describe() -> str:
    """把工具清单渲染成 prompt 里的一段文字。"""
    lines = []
    for name, spec in TOOL_SPECS.items():
        params = "、".join(f"{k}（{v}）" for k, v in spec["params"].items())
        lines.append(f"- {name}：{spec['desc']}\n  参数：{params}")
    return "\n".join(lines)


# ---------- 环境包里的库内事实（代码先替他查好） ----------

# 一次最多看两个名字、每个名字最多几张卡、每张卡最多几门课：
# 环境包是每轮都要进上下文的东西，它不能变成第二个「全量注入」。
KB_ENV_MAX_NAMES = 2
KB_ENV_MAX_CARDS = 4
KB_ENV_MAX_COURSES = 6


def kb_env_facts(uid: str, message: str) -> list[dict[str, Any]]:
    """用户这句话里提到的院系/专业，代码先把库里的原文事实查出来。

    为什么不等模型调工具：用户说「我是信管的大二学生」时，「信管」不是一个规范名称，
    模型既可能凭印象解释它，也可能反问一个已经答过的信息（实测问过「你是哪个学校」）。
    这里是**组装环境包**的活，和按层召回记忆是同一个模式：只读、有界、可追溯——
    用不用、怎么用仍然由模型决定（它想更细再用 major_detail 查）。
    """
    names = curriculum.mentions(message)[:KB_ENV_MAX_NAMES]
    out: list[dict[str, Any]] = []
    for name in names:
        r = _kb_tool("major_lookup", uid, {"query": name})
        if not r.get("ok") or not r.get("命中"):
            continue
        cards = []
        for c in (r.get("命中") or [])[:KB_ENV_MAX_CARDS]:
            cards.append({
                "专业": c.get("专业"),
                "院系": c.get("院系"),
                "毕业总学分": c.get("毕业总学分"),
                "专业必修_方案原文": c.get("专业必修_方案原文"),
                "核心必修课": (c.get("核心必修课") or [])[:KB_ENV_MAX_COURSES],
                "来源页": c.get("来源页"),
            })
        out.append({
            "用户说到的词": name,
            "库里认成": r.get("kind"),
            "院系": r.get("院系") or (cards[0].get("院系") if cards else ""),
            "命中": cards,
            "说明": r.get("说明") or ("这是该院系在培养方案里的全部专业/项目。"
                                      "分流名额与当年方案以院系教务为准。"),
        })
    return out
