# -*- coding: utf-8 -*-
"""记忆服务：写入白名单、确定性校验、提交、召回。

契约见 docs/DIALOGUE_CONTRACT.md §3（key registry）、§7（校验）、§4（召回）。

核心原则：**模型提议，代码裁决。**
模型输出的每一条 memory_op 都要过 validate_ops；过不了的丢弃并记入 rejected_ops
（返回给前端和 decision log），绝不静默写库，也绝不让模型自己指定 confidence。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

import evidence
import store
import transcript
from schemas import DECISION_STATUSES, UserFact, now_iso

# ---------- §3 Key Registry ----------

# slug 允许连字符和点，也允许中文：模型很自然会写 interest:machine-learning，
# 也会写 current:选课数据大作业。早期只放行 [a-z0-9_] 会把这类真实信息整条拒掉
# （丢的是用户的话，不是垃圾）——这个错犯过两次，第二次是中文。
_SLUG_RE = re.compile(r"^[a-z0-9\u4e00-\u9fff][a-z0-9\u4e00-\u9fff_.\-]{0,39}$")
_SEP_RE = re.compile(r"[\s_]+")


def canon_key(key: str) -> str:
    """把 key 规范化，让同一个东西尽量只落到一个 key 上。

    允许中文之后，「interest:机器学习 」「interest:机器学习」「interest:机器学习_」
    会长成三条。但要清醒：ASCII-only **从来没有**解决同义重复
    （ai / AI / 人工智能 照样是三条），它只是拒掉了中文。
    所以这里做代码真正做得了的那部分——空格和下划线统一成连字符、去掉首尾分隔符。
    同义不同词那部分交给 single 基数的 supersede 和幂等 add 去兜。
    """
    k = (key or "").strip().lower()
    # 精确 registry key 的下划线是协议的一部分（如 base:math_code），不能改成连字符后拒掉。
    if k in _EXACT:
        return k
    if ":" not in k:
        return k
    ns, slug = k.split(":", 1)
    k = f"{ns}:{_SEP_RE.sub('-', slug.strip()).strip('-. ')}"
    # 在册的精确 key 带下划线（base:math_code）：规范化后要认回原样，原来变成 base:math-code 查不到（Codex 复现）
    return next((e for e in _EXACT if ":" in e and _SEP_RE.sub("-", e) == k), k)


class KeySpec(NamedTuple):
    category: str
    cardinality: str          # single | multi | exclusive
    sources: tuple[str, ...]  # 允许的写入来源
    requires_valid_until: bool = False
    # 时间跨度层。决定注入预算、展示分组，以及「什么时候该重新问」。
    layer: str = "identity"
    # 不填 valid_until 时自动给的存活天数（0 = 不过期）。
    # 「当前实践」天然会过期：不问也不会一直错下去（见 docs/DIALOGUE_CONTRACT.md §4.1）。
    default_ttl_days: int = 0
    # 能不能主动问。False = 识别到就存，但**不主动追问**。
    # 性别/年龄放这里：它们几乎不改变任何建议，却是敏感信息——
    # 追问的收益近乎零，代价是让人觉得像在填表。用户自己说了就存着。
    ask: bool = True


# 层 = 时间跨度。顺序同时是提问顺序和界面展示顺序（契约 §4.1）。
# 为什么按时间跨度分而不是按主题分：变化速度决定「什么时候该重新问」，
# 也决定注入预算——身份类小且必带，倾向类多且只在聊方向时进来。
LAYERS: tuple[tuple[str, str], ...] = (
    ("identity", "身份"),
    ("practice", "当前实践"),
    ("capability", "能力起点"),
    ("experience", "经历积累"),
    ("interest", "倾向"),
    ("constraint", "临时约束"),
)
LAYER_LABELS: dict[str, str] = dict(LAYERS)
DEFAULT_LAYER = "identity"

# 每层注入预算。取代原来「平表按关键词取 top8」——那种做法里一门课的成绩
# 可能挤掉他的方向。每层给固定坑位，层内再按相关度排。
LAYER_BUDGET: dict[str, int] = {
    "identity": 6,
    "practice": 6,
    # 能力层放了两种东西：用户自述（capability:* / base:*）和成绩单推出来的
    # 事实（transcript:*）。后者是硬证据，坑位不够时**它优先**（见 recall_grouped）。
    # 4 个坑装不下「3 类课 + 数学基础 + 编程基础」，所以给 6。
    "capability": 6,
    "interest": 4,
    "experience": 3,
    "constraint": 4,
}


# 精确 key。分「域」见 key 的命名空间，分「层」见 layer。
_EXACT: dict[str, KeySpec] = {
    # 身份
    "grade": KeySpec("background", "single", ("declared",), layer="identity"),
    "school": KeySpec("background", "single", ("declared",), layer="identity", ask=False),
    "major": KeySpec("background", "single", ("declared",), layer="identity"),
    # 院系和专业分开存：常见「信息管理系 / 信息管理与信息系统」这种
    # 院系≠专业的组合，合成一条会丢掉其中一个（这正是 school 曾经被丢掉的老问题）。
    "department": KeySpec("background", "single", ("declared",), layer="identity"),
    # 入学年份：有了它年级可以自己算，否则「大二」每年都得重问一遍。
    "enroll_year": KeySpec("background", "single", ("declared",), layer="identity", ask=False),
    # 存得下、但不主动问（见 KeySpec.ask）
    "gender": KeySpec("background", "single", ("declared",), layer="identity", ask=False),
    "age": KeySpec("background", "single", ("declared",), layer="identity", ask=False),
    "location": KeySpec("background", "single", ("declared",), layer="identity", ask=False),
    # 每周可投入时间：决定任务给多大，是他最该被问清的一件事之一。
    "pace": KeySpec("preference", "single", ("declared",), layer="interest", ask=False),
    # 正在跟的组/老师。本科想进科研，这是最直接的抓手，所以放「当前实践」而不是经历。
    "research_group": KeySpec("practice", "multi", ("declared", "inferred"),
                              layer="practice", default_ttl_days=180),
    "base:math": KeySpec("capability", "single", ("declared",), layer="capability"),
    "base:code": KeySpec("capability", "single", ("declared",), layer="capability"),
    "base:math_code": KeySpec("capability", "single", ("declared",), layer="capability"),
    "base:none": KeySpec("capability", "single", ("declared",), layer="capability"),
    # 文献伴读首次交流：自述阅读经验不冒充提交验证过的 experience 事件。
    "reading_experience": KeySpec("background", "single", ("declared",), layer="practice"),
    "reading_barrier": KeySpec("preference", "single", ("declared",), layer="interest"),
    "reading_support": KeySpec("preference", "single", ("declared",), layer="interest"),
}

# 命名空间 key：<namespace>:<slug>
_NAMESPACED: dict[str, KeySpec] = {
    "card": KeySpec("experience", "multi", ("behavior",), layer="experience", ask=False),
    # 学籍：双专业 / 辅修。变化很慢，但会改变他要补的课，所以归身份层。
    "enroll": KeySpec("background", "multi", ("declared",), layer="identity"),
    "goal": KeySpec("interest", "multi", ("declared",), layer="interest"),
    "interest": KeySpec("interest", "multi", ("declared", "inferred"), layer="interest"),
    # 方向互斥：写新的自动把旧的置 superseded。
    "direction": KeySpec("interest", "exclusive", ("declared",), layer="interest"),
    # 细分科研领域：比 direction 更细（例如 direction:ai 下面的「具身智能」）。
    "field": KeySpec("interest", "multi", ("declared", "inferred"), layer="interest"),
    # 就业去向：方向 / 地域 / 期望薪资。放在倾向层——它和科研方向是同一种
    # 「他想去哪」的信息，只是落点不同。
    "career": KeySpec("interest", "multi", ("declared",), layer="interest", ask=False),
    "style": KeySpec("preference", "single", ("declared",), layer="interest", ask=False),
    # 自然聊天里会出现「会一点 Python」这类能力自述；自述「已掌握」会被降级标注，
    # 见 validate_ops 的 needs_behavior_evidence。
    "capability": KeySpec("capability", "multi", ("declared", "inferred"), layer="capability"),
    # 从成绩单**推导**出来的能力底稿。只有代码能写（sources 是 derived-only）。
    # 和 capability: 分开是有意的：
    #   capability:*  = 用户自述或模型推断（「我会一点 Python」）
    #   transcript:*  = 成绩单推出来的事实（「修过 3 门数学类课程，加权平均 88.3」）
    # 混在一个 key 上会分不清哪部分是硬证据、哪部分是自述——
    # base:code 事故就是把两者搅在一起写出来的。
    "transcript": KeySpec("capability", "multi", ("derived",), layer="capability"),
    # 当前实践：手头在上的课、在做的项目。90 天自动过期，过期后系统会重新问，
    # 而不是拿半年前的项目当「他正在做的事」。（这正是「实践跨度」那一层）
    "current": KeySpec("practice", "multi", ("declared", "inferred"),
                       layer="practice", default_ttl_days=90),
    # 项目：一个项目 = 一个文件夹。
    #
    # 用户要的是「大项目记忆」——KotobaAI 这种东西不该拆成「在做什么」+「能力」
    # 两条互不认识的记录。但**不能做成一块 blob**，因为四条机制都要逐条粒度：
    # 证据核对、TTL、单独改删、按相关度部分注入。
    #
    # 所以是「实体 + 属性」：
    #   project:kotoba            实体本身（这是什么）
    #   project:kotoba.what       它是什么
    #   project:kotoba.stage      到什么阶段了
    #   project:kotoba.stack      用了什么
    #   project:kotoba.blocker    卡在哪
    #   project:kotoba.next       下一步
    #   project:kotoba.traction   成果/数据
    #
    # 存储上仍是分开的事实（每条各有证据、可单独改删）；
    # 展示上合成一张卡（见 recall 和前端 proj-*）；
    # 注入时整组进出（recall 里 project 同组一起捞）——
    # 项目缺一半会导致误判，所以不拆开注入。
    #
    # TTL 180 天：项目比「这周在做什么」活得久，但也不该无限期当现状。
    "project": KeySpec("practice", "multi", ("declared", "inferred"),
                       layer="practice", default_ttl_days=180),
    # 担任的角色：社团、学工、志愿服务。
    # 和 experience 分开是有意的：experience 是「做出过什么」，必须由提交事件写入；
    # role 是「担任什么」，用户自述即可。混在一起会让行为证据被自述稀释。
    "role": KeySpec("experience", "multi", ("declared",), layer="experience", ask=False),
    # 临时约束必须带有效期，否则会变成永久设定。
    "constraint": KeySpec("preference", "multi", ("declared",),
                          requires_valid_until=True, layer="constraint", ask=False),
    # 经历只能由提交事件写入，模型不能写（见 §3 规则 3）。
    "experience": KeySpec("experience", "multi", ("behavior",), layer="experience"),
}


# ---------- 「核对」页的字段清单 ----------
# 用户给的九大类，以及每类里的固定字段。
# 为什么显式写出来、不从 registry 自动推导：用户看到的分类和库里的命名空间
# 不是一对一——「已修课程」在 enrollments 表里而不是 facts 里，
# 「进修方向」横跨身份层和倾向层。自动推导会让界面上的分类跟用户说的对不上。
#
# key 写法：
#   "grade"      精确匹配一条 facts
#   "current:"   前缀匹配（命名空间）
#   "@enroll"    特殊：查 enrollments 表
class Slot(NamedTuple):
    key: str
    label: str
    why: str          # 填了它，能让什么更准 —— 直接显示给用户看
    presumed: str = ""  # 产品的默认前提（本产品只服务北大学生）。显示成已填、不主动问，
                        # 只在用户自己说出别的值时被覆盖。


PRUNED = frozenset({
    "school", "enroll_year", "gender", "age", "location", "pace",
    "career", "style", "role", "constraint",
})


def is_pruned_key(key: str) -> bool:
    head = (key or "").split(":", 1)[0]
    return head in PRUNED


COVERAGE_GROUPS: tuple[tuple[str, str, str, tuple[Slot, ...]], ...] = (
    ("identity", "身份", "你是谁。这层决定我给你什么难度的东西。", (
        Slot("grade", "年级", "大一和大二能做的事不一样"),
        Slot("department", "院系", "院系决定你身边有什么资源"),
        Slot("major", "专业", "专业决定你已经有哪些基础"),
    )),
    ("courses_done", "已修课程", "你学过什么。这是判断起点最硬的依据。", (
        Slot("@enroll", "成绩单", "粘贴一次，我就知道你修过哪些课、考得怎么样"),
    )),
    ("practice", "在修课程与当前在做的事", "你此刻手头的事。它变得最快，所以我最常回来问。", (
        Slot("project:", "在做的项目", "项目是你最实的当前实践——任务要挂在它上面"),
        Slot("current:", "在修课程 / 在做的事", "任务要挂在你正在做的事上才有用"),
        Slot("current:audit", "旁听课程", "旁听说明你在主动往外看"),
    )),
    ("capability", "能力", "从成绩单和经历里推出来的结论，不是我拍脑袋。", (
        Slot("capability:", "已掌握的能力", "决定起点难度"),
        Slot("base:", "基础自评", "数学和编程基础决定路径怎么排"),
    )),
    ("interest", "进修方向", "你想往哪走。没有它，我只能给泛泛的建议。", (
        Slot("direction:", "方向", "决定我给你哪条路径"),
        Slot("field:", "细分科研领域", "越具体，我能找的论文和项目越准"),
        Slot("enroll:", "双专业 / 辅修", "决定你还缺哪些课"),
        Slot("goal:", "目标", "读研、出国、就业，路径不一样"),
    )),
    ("social", "人际交往", "社团、学工、志愿，以及导师课题组。", (
        Slot("research_group", "导师课题组", "本科生进科研最直接的抓手就是进组"),
    )),
    ("experience", "经验积累", "你真做出来过的东西。只有交过成果才算。", (
        Slot("experience:", "做过的项目 / 成果", "这是唯一没法自己说了算的依据，所以最值钱"),
    )),
)


def _slot_filled(uid: str, slot_key: str, facts_keys: set[str],
                 enroll_count: int) -> tuple[bool, str]:
    """这个字段填了没有，以及填的是什么（给界面显示一行值）。"""
    if slot_key == "@enroll":
        return (enroll_count > 0), (f"已录入 {enroll_count} 门" if enroll_count else "")
    if slot_key.endswith(":"):
        hit = sorted(k for k in facts_keys if k.startswith(slot_key))
        return bool(hit), (hit[0] if hit else "")
    hit = slot_key in facts_keys
    return hit, (slot_key if hit else "")


def coverage(uid: str) -> dict[str, Any]:
    """「核对」页用：按类别列出每个固定字段填了没有。

    用户的原话：「把我给出来的这些空都给用户展示出来……表示填好这些信息
    可以让科研导向更准确」。所以每个空格都带一句「填了它有什么用」——
    空格本身是邀请，但得说清代价和收益，否则就是一张逼人填的表。
    """
    alive = active_facts(uid)
    by_key = {f.key: f.value for f in alive}
    keys = set(by_key)
    enroll_count = len(store.list_enrollments(uid))

    groups = []
    total = filled = 0
    for gid, label, why, slots in COVERAGE_GROUPS:
        items = []
        for s in slots:
            ok, val = _slot_filled(uid, s.key, keys, enroll_count)
            if ok and s.key in by_key:
                val = by_key[s.key]
            # 前缀类字段拿真实值显示，别拿 key 当值
            if ok and s.key.endswith(":") and val:
                val = by_key.get(val, "")
            if not ok and s.presumed:
                # 默认前提（本产品只服务北大）算已填：核对页不该给用户留一个
                # 永远填不上、还会诱导对话去问的格子。
                ok, val = True, s.presumed
            items.append({
                "key": s.key, "label": s.label, "why": s.why,
                "filled": ok, "value": val,
                # 值是默认前提（不是他自己说的）——界面用它决定要不要标「默认」
                "presumed": bool(s.presumed) and val == s.presumed,
            })
            total += 1
            filled += 1 if ok else 0
        groups.append({
            "group": gid, "label": label, "why": why, "slots": items,
            "filled": sum(1 for i in items if i["filled"]), "total": len(items),
        })

    return {
        "groups": groups,
        "filled": filled,
        "total": total,
        "missing": total - filled,
    }


def layer_of(key: str) -> str:
    if key.startswith("card:"):
        return "experience"
    spec = spec_for(key)
    return spec.layer if spec else DEFAULT_LAYER


def layer_label(key: str) -> str:
    return LAYER_LABELS.get(layer_of(key), layer_of(key))

# 只有这些来源是「用户自己说的」——模型输出一律按 declared/inferred 处理，
# 永远不能自称 behavior（那是提交事件才有的事实）。
_MODEL_SOURCES = ("declared", "inferred")

_CONFIDENCE_BY_SOURCE = {
    "declared": 0.6,    # 用户自己说的
    "inferred": 0.4,    # 我们推的
    "derived": 0.75,    # 从成绩单这类**官方底稿**推出来的，比自述硬
    "behavior": 0.8,    # 在我们系统里真做出来的
    "user_edit": 1.0,   # 用户在界面上亲手改的（他是关于他自己的最终权威）
}

# 声称「已掌握」的措辞。只有 declared 证据时降级并标注，不得据此升级任务难度。
_MASTERY_WORDS = ("掌握", "熟练", "精通", "独立完成", "独立写", "会写", "aca", "能独立")


def spec_for(key: str) -> KeySpec | None:
    """key → 写入规则；未命中返回 None（该 op 必须被拒绝）。"""
    k = canon_key(key)
    if not k:
        return None
    if k.startswith("card:"):
        parts = k.split(":", 2)
        return _NAMESPACED["card"] if len(parts) == 3 and parts[1] and parts[2] and len(k) <= 250 else None
    if k in _EXACT:
        return _EXACT[k]
    if ":" not in k:
        return None
    ns, slug = k.split(":", 1)
    spec = _NAMESPACED.get(ns)
    if not spec or not _SLUG_RE.match(slug):
        return None
    return spec


def confidence_for(source: str) -> float:
    return _CONFIDENCE_BY_SOURCE.get(source, 0.5)


# ---------- 有效期 ----------


def _parse_when(raw: Any) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def is_expired(fact: UserFact, now: datetime | None = None) -> bool:
    """临时约束到点后不再参与决策（03 §2「时间上下文」）。解析不了的日期视为未过期，不静默丢弃。"""
    when = _parse_when(fact.valid_until)
    if when is None:
        return False
    return when < (now or datetime.now(timezone.utc))


def _plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ---------- §7 确定性校验 ----------


def _evidence_ok(quote: str, pool: list[str]) -> bool:
    """引文必须能在本画像的消息里逐字找到。这是防编造的关键一条。"""
    q = (quote or "").strip()
    if len(q) < 2:
        return False
    return any(q in text for text in pool if text)


# 虚词：出现在引文里不代表支撑了值，算支撑度时排除。
_SUPPORT_STOP = set("的了是在和与及或我你他她它们这那就也都很没为以对把被让给从到个之其会要可能但而且还只又再已经过将")

# 支撑度阈值。>=OK 直接收；中间地带保留但降级为 inferred；太低直接拒。
_SUPPORT_OK = 0.8
_SUPPORT_FLOOR = 0.5
# 只对「有实质内容」的值做支撑度检查。短值（"AI"、"大二"、方向 slug）本来就是
# 规范化缩写，逐字要求它出现在引文里会误杀；真正出问题的是长值里塞了引文没有的东西。
_SUPPORT_MIN_CHARS = 8


def _content_chars(text: str) -> set[str]:
    return {c for c in (text or "")
            if (c.isalnum() or "\u4e00" <= c <= "\u9fff")} - _SUPPORT_STOP


def support_ratio(value: str, quote: str) -> float:
    """值的「内容字」有多少能在引文里找到。

    只验「引文存在」是不够的，这是实测出来的：模型写 base:code 时，值里塞了三门课
    加成绩（来自别的消息），却挂一句「也做过一些个人小项目，但都是Vibecoding的」当依据，
    旧校验照样通过。于是它自己加的「（依赖AI生成）」永久进了画像，
    并直接导致了一个不合身份的任务（见 docs/CHANGELOG.md）。
    所以要补上这条：**引文得撑得住这个值**，不只是碰巧出现过。
    """
    v = _content_chars(value)
    if not v:
        return 1.0
    return len(v & _content_chars(quote)) / len(v)


# ---------- 按字段核对：模型给结构化的值，代码去原话里对（evidence.py） ----------
# 原来是一串字面启发式（年级正则、否定窗口、数字正则），审了十七轮还在补：「不想碰」「not a graduate student」
# 「我没学过Python」→「会」「北京大学」撑「清华大学」都能混过去。现在每种字段一个确定的核对办法：
#   grade        {stage, year}：原话里按固定的表认出同一个（阶段, 第几年），说的是现在；
#   age/enroll_year/pace  {value, unit, per}：原话里有同一个数、同一量纲的单位（小时和分钟、年和月可以换算）；
#   school/department/major：库里的名字或简称在原话里作为学籍出现、说的是现在；
#   interest/goal/field：值就是原话的一段（或常见缩写），不在否定、过去、假设、改口、别人的事里；
#   其余字段（direction 的值常是库里的方向名，不一定是原话）：照旧按支撑度，另外查数字、英文词，和原话在共有片段上的说法要一致。
# 范围都按引文所在的整条消息算（evidence.scan），只认引文那一段里的说法。
_QTY_KEYS = {"age": ("age", "岁"), "enroll_year": ("calyear", "年"), "pace": ("clock", "小时")}
_ENTITY_KEYS = ("school", "department", "major")
_SPAN_KEYS = ("interest", "goal", "field")
# 值外面包着的「对…感兴趣」「想学…」不是值本身：「对机器学习感兴趣」按「机器学习」去原话里找
_FRAMING = re.compile(r"^(?:我)?(?:对于?|想学|想做|想试试|想往|想走|喜欢|关注|关心|希望)?(.+?)(?:感兴趣|有兴趣|很感兴趣|方向|领域|相关)?$", re.S)
_PER_CN = {"w": "周", "d": "天", "mo": "月", "y": "年", "term": "学期"}
# 年龄、每周时间说的是现在（「我打算每周投入九小时」不算，Codex 复现）；入学年份本来就是过去的事（「我去年2025年入学」，Codex 复现）
_QTY_SCOPES = {"enroll_year": evidence.STATE - {evidence.PAST}}
# 本来就是「想」的自由文字字段，原话在打算里不算不一致
_TEXT_WISH = ("career", "direction")
# 能力类允许「只撑住一部分 → 降成 inferred」（docs/DIALOGUE_CONTRACT.md §3.3）；方向值常是库里的方向名。别的自由文字字段不许多出原话没有的东西
_TEXT_LOOSE = ("base", "capability", "direction")
# 值里不算「多出来的东西」的词：包装、程度、时间指代
_FILLER_WORDS = re.compile(r"研究|兴趣|方向|领域|相关|应用|方面|内容|正在|在做|在修|在上|目前|现在|这学期|本学期|已经|还|很|比较|主要|大概|一些|一点|"
                           r"最多|至少|不到|以上|以内|左右|大约|约|只有|只能")
# 入学年份要连着入学说（「2021年我参加了竞赛」不是入学，Codex 复现）；每周时间不能是上课、通勤这类占掉的时间（Codex 复现）
_NOT_FREE_TIME = re.compile(r"课|体育|通勤|睡|上班|兼职|打工|家教|坐车")
# 数量字符串里除了这个量只许有这些：「19岁，计算机基础扎实」不是一个年龄（Codex 复现）
_QTY_FILLER = re.compile(r"(?:每[ \t]?个?(?:周|星期|礼拜|天|日|月|年|学期)|[一1][ \t]?个?(?:周|星期|礼拜|天|月)|今年|周岁|虚岁|入学|入校|级|届|约|大约|大概|"
                         r"左右|差不多|上下|能|可以|投入|学习|时间|per|a|an|week|day|month|weekly|daily|about|"
                         r"around|roughly|hours?|[\s,，。.;；~～:：/])*")
_UNIT_SHOWN = {u: "小时" for u in ("h", "hr", "hrs", "hour", "hours", "钟头", "钟")} | \
              {u: "分钟" for u in ("min", "mins", "minute", "minutes", "分")} | {"周岁": "岁", "years old": "岁", "year old": "岁"}


class _Ctx(NamedTuple):
    text: str                             # 引文所在的整条消息（prep 过）
    windows: evidence.Windows             # 引文在这条消息里的每一处位置
    quote: str                            # 引文原样（比内容字、支撑度用）


def _context(quote: str, pool: list[str] | None = None) -> _Ctx:
    """引文按它所在的整条消息核对范围：模型只引「大四」，「我不是大四」的「不是」照样算（原来只看引文，否定、过去、主语都被裁在外面，Codex 复现）。
    同一句话出现在几条消息里，用最近的那条。"""
    q = evidence.prep(quote).strip()
    for msg in reversed(pool or []):
        t = evidence.prep(msg)
        wins, i = [], t.find(q) if q else -1
        while i != -1 and len(wins) < 64:  # 不重叠地找、最多 64 处：原来逐位找重叠的长引文，几万字是平方级（Codex 复现）
            wins.append((i, i + len(q)))
            i = t.find(q, i + max(1, len(q)))
        if wins:
            return _Ctx(t, evidence.Windows(tuple(a for a, _ in wins), len(q)), quote)
    return _Ctx(q, evidence.Windows((0,), len(q)), quote)


def _inside(ctx: _Ctx, s: int, e: int) -> bool:
    return ctx.windows.contain(s, e)


def _kind(key: str) -> str:
    if key == "grade":
        return "grade"
    if key in _QTY_KEYS:
        return "quantity"
    if key in _ENTITY_KEYS:
        return "entity"
    return "span" if ":" in key and key.split(":")[0] in _SPAN_KEYS else "text"


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        n = float(x)
    except (TypeError, ValueError, OverflowError):  # 10**400 是合法 JSON，原来 float() 抛出来整轮校验中断（Codex 复现）
        return None
    return n if math.isfinite(n) and abs(n) < 1e7 else None


def _parse_qty(key: str, value: Any) -> tuple[evidence.Qty, str, bool] | str | None | object:
    """{value, unit, per} 或者一段写着数的字符串 →（这个量, 存库的写法）。字符串里没有数 → None（按普通文字核对）；
    字典形状不对、或者字符串不只是一个这种量（「19岁，已发表论文」「五周」「3」门课）→ BAD 或拒。
    字典先拼成「每周10小时」再交给同一个解析，免得两套单位规则。"""
    dim, unit0 = _QTY_KEYS[key]
    if isinstance(value, dict):
        if not value or set(value) - {"value", "unit", "per"}:
            return evidence.BAD
        n, unit, per = value.get("value"), value.get("unit") or unit0, value.get("per")
        lo, hi = (n[0], n[1]) if isinstance(n, list) and len(n) == 2 else (n, n)
        lo, hi = _num(lo), _num(hi)
        if lo is None or hi is None or not isinstance(unit, str) or not isinstance(per, (str, type(None))):
            return evidence.BAD
        unit = re.sub(r"^个", "", evidence.prep(unit).strip())
        unit = _UNIT_SHOWN.get(unit, unit)
        per_code = ""
        if per:
            p = evidence.prep(per).strip().removeprefix("每").removeprefix("per ").strip()
            per_code = p if p in _PER_CN else evidence._PER_WORD.get(p, "")
            if not per_code:
                return evidence.BAD
        text = (f"每{_PER_CN[per_code]}" if per_code else "") + evidence.dec(lo) + (f"到{evidence.dec(hi)}" if hi != lo else "") + unit
    else:
        text = str(value or "").strip()
    qs = evidence.quantities(text)
    if not qs:
        return evidence.BAD if isinstance(value, dict) else None
    q = qs[0]
    t = evidence.prep(text)
    if len(qs) != 1 or not _QTY_FILLER.fullmatch(t[:q.start] + " " + t[q.end:]):
        return evidence.BAD if isinstance(value, dict) else "number_not_in_evidence"
    if not q.dim:  # 光一个数：按这个字段本来的单位（年龄 20 = 20 岁，入学 24 = 2024 年）
        lo, hi = q.lo, q.hi
        if dim == "calyear" and lo < 100:
            lo, hi = lo + 2000, hi + 2000
        q = q._replace(dim=dim, lo=lo, hi=hi, base_lo=lo * (60 if dim == "clock" else 1), base_hi=hi * (60 if dim == "clock" else 1))
    if q.dim != dim or (key == "enroll_year" and q.lo != q.hi):
        return evidence.BAD if isinstance(value, dict) else "number_not_in_evidence"
    if not isinstance(value, dict):
        # 带单位的字符串和结构化的值一样严格比：原来「每周7小时」绕过了上下限、per 和入学关系的检查（Codex 复现）；光一个数照旧只比数
        return q, text, bool(qs[0].dim)
    span = evidence.dec(q.lo) + (f"到{evidence.dec(q.hi)}" if q.hi != q.lo else "")  # 范围两头都留（原来只存低的那头，Codex 复现）
    return q, {"age": f"{span}岁", "enroll_year": span}.get(key, text), True


def _qty_reason(key: str, q: evidence.Qty, ctx: _Ctx, strict: bool) -> str | None:
    hits = [h for h in evidence.quantities(ctx.text) if _inside(ctx, h.start, h.end) and evidence.qty_match(q, h, strict=strict)]
    if strict and key == "enroll_year":  # 年份后面紧跟着入学：「2024年入学」「24年进的北大」「2025年我来北大报到」
        hits = [h for h in hits if h.label in ("级", "届") or evidence._ENROLL_AFTER.match(ctx.text, h.end)
                or re.search(r"入学[^，。,；;]{0,6}$", ctx.text[max(0, h.start - 8):h.start])]  # 「入学年份2023」「入学那会是两千零二十三年」
    if strict and key == "pace":  # 「课余」「课后」「课题组」不是课（Codex 复现）；同一分句只看一次
        sc, busy = evidence.scan(ctx.text), {}
        for h in hits:
            a, b = sc.clause(h.start)
            if a not in busy:
                busy[a] = bool(_NOT_FREE_TIME.search(re.sub(r"课[余外后间题]", "", ctx.text[a:b])))
        hits = [h for h in hits if not busy[sc.clause(h.start)[0]]]
    if not hits:
        return "number_not_in_evidence"
    sc = evidence.scan(ctx.text)
    kinds = _QTY_SCOPES.get(key, evidence.STATE)
    return None if any(evidence.is_current(sc, h.start, h.end, kinds) for h in hits) else "negated_in_evidence"


def _occurrence_reason(occ: list[tuple[int, int]], ctx: _Ctx, kinds: frozenset[str]) -> str | None:
    occ = [o for o in occ if _inside(ctx, *o)]
    if not occ:
        return "short_value_not_in_evidence"
    sc = evidence.scan(ctx.text)
    return None if any(evidence.is_current(sc, s, e, kinds) for s, e in occ) else "negated_in_evidence"


def _scope_mismatch(key: str, value: str, ctx: _Ctx) -> bool:
    """自由文字的值和原话在共有片段上说法不一致：原话是否定、过去、打算、假设、别人的事、改口，值却当成他现在的事
    （「我没学过Python」→「会Python」、「我上学期在上概率论」→「在上概率论」、「我朋友在上计量」→ 他在上，Codex 复现）；
    值里有否定原话却没有也不行（「我会 SQL」→「不会 SQL」）。英文词是具体的东西，一个不一致就算；中文片段要全不一致才算。"""
    kinds = evidence.STATE - ({evidence.IRREALIS} if key.split(":")[0] in _TEXT_WISH else set())
    sc = evidence.scan(ctx.text)
    if evidence.has_negation(value) and not evidence.has_negation(ctx.quote) \
            and not any(evidence.kinds_at(sc, a, a + ctx.windows.length, frozenset({evidence.NEG})) for a in ctx.windows.starts):
        return True
    vt = evidence.prep(value)
    verdicts = []
    # 缩写也要换着找：「我不喜欢AI」撑不住「人工智能」（Codex 复现）；片段在值里的说法按换过缩写的那一版找，
    # 原来在原值里找「ml」找不到，「不会机器学习」的否定就丢了（Codex 复现）
    pairs = {(a, form) for form in evidence.span_forms(value) for a in evidence.anchors(form, ctx.quote, min_len=2)}
    for a, form in sorted(pairs):
        occ = [o for o in evidence.span_occurrences(a, ctx.text) if _inside(ctx, *o)]
        if not occ:
            continue
        own = evidence._find(form, a)
        v = evidence.kinds_at(evidence.scan(form), *own[0], kinds) if own else set()
        if _PERFECTIVE.search(vt):  # 「学过高等数学」本来就是说以前的事，原话里的「以前」不算不一致（Codex 复现）
            v = v | {evidence.PAST}
        ok = any(not (q - v) and (evidence.NEG in q) == (evidence.NEG in v)
                 for q in (evidence.kinds_at(sc, s, e, kinds) for s, e in occ))
        if not ok and (a.isascii() or len(a) >= 3):  # 三个字以上的片段对不上就算：「线性代数和实分析都学过」里的「实分析」（Codex 复现）
            return True
        verdicts.append(not ok)
    return bool(verdicts) and all(verdicts)


_PERFECTIVE = re.compile(r"(?:接触|参加|学|修|做|用|写|上|读|看|碰|练|跑)过|曾经|曾|完成了|做完了?|学完了?|写完了?")


def _extra_claims(value: str, ctx: _Ctx) -> bool:
    """值里有原话没说的东西：去掉和原话共有的片段（两个字以上）、数字、虚词和包装词后还剩两个字以上的内容。
    「正在开发大型推荐系统」对「我在看推荐系统入门教程」多出了「开发大型」，「机器学习和量子化学」多出了「量子化学」（Codex 复现）。"""
    rest = evidence.prep(value)
    for a in sorted({a for form in evidence.span_forms(value) for a in evidence.anchors(form, ctx.quote, min_len=2)}, key=len, reverse=True):
        rest = rest.replace(a, " ")
    rest = _FILLER_WORDS.sub(" ", rest)
    return len(_content_chars(re.sub(r"[0-9.]+", " ", rest)) - _content_chars(evidence.prep(ctx.quote))) >= 2


def _words_missing(value: str, ctx: _Ctx) -> bool:
    """值里的英文词（Java、PyTorch）原话里得有同一个词：原来按字母重合，「我会 JavaScript」撑住了「精通 Java」（Codex 复现）。"""
    q = evidence.prep(ctx.quote)
    return any(w.isascii() and not any(evidence._has(q, f) for f in evidence.span_forms(w)) for w in evidence.runs(value))


def _typed_value(key: str, value: Any, ctx: _Ctx) -> tuple | None | object:
    """结构化字段的核对：（存库的值, 拒绝理由或 None[, 只撑住一部分时的支撑度]）。
    不是结构化字段、或者字符串里认不出结构 → None；字典形状不对 → BAD。"""
    kind = _kind(key)
    if kind == "grade":
        g = evidence.parse_grade(value)
        if g is evidence.BAD:
            return g
        if g is None:  # 「本科二年级，GPA4.0」：年级后面夹带了别的，整条不收
            return str(value), "grade_not_in_evidence"
        shown = evidence.render_grade(*g) if isinstance(value, dict) else str(value).strip()
        return shown, (None if evidence.grade_supported(g[0], g[1], ctx.text, ctx.windows) else "grade_not_in_evidence")
    if isinstance(value, dict) and kind != "quantity":
        return evidence.BAD
    if kind == "quantity":
        parsed = _parse_qty(key, value)
        if parsed is None or parsed is evidence.BAD:
            return parsed
        if isinstance(parsed, str):
            return str(value), parsed
        q, shown, strict = parsed
        return shown, _qty_reason(key, q, ctx, strict=strict)
    text = str(value).strip()
    if kind == "entity":
        return text, _occurrence_reason(evidence.entity_occurrences(key, text, ctx.text, ctx.windows), ctx, evidence.STATE)
    if kind == "span":
        occ = [o for o in evidence.span_occurrences(text, ctx.text) if _inside(ctx, *o)]
        core = _FRAMING.match(text)
        if not occ and core and core.group(1) != text:
            occ = [o for o in evidence.span_occurrences(core.group(1), ctx.text) if _inside(ctx, *o)]
        if occ:
            return text, _occurrence_reason(occ, ctx, evidence.WISH)
        # 不是原话：值里得有原话的一大段（四个字以上的中文或一个英文词）当锚，锚说的是现在；收下也只算我们推的
        anchors = [o for a in evidence.anchors(text, ctx.quote) for o in evidence.span_occurrences(a, ctx.text)]
        ratio = support_ratio(text, ctx.quote)
        # 值里带换行时原来 _FRAMING 匹配不上、.group 抛异常，整轮对话中断（Codex 复现）
        if not anchors or ratio < _SUPPORT_FLOOR or _words_missing(text, ctx) or _extra_claims(core.group(1) if core else text, ctx):
            return text, "short_value_not_in_evidence"
        if not _numbers_supported(text, ctx.quote):  # 「每周读5篇」不能借「每周读2篇机器学习论文」的锚混进来（Codex 复现）
            return text, "number_not_in_evidence"
        return text, _occurrence_reason(anchors, ctx, evidence.WISH), min(ratio, _SUPPORT_OK - 0.001)
    return None


# 下面几个是老接口，测试和调用方还在用，都落到 evidence 上（只有引文、没有整条消息时按引文本身算）
def _grades_in(text: str) -> list[tuple[str | None, int | None]]:
    return [(g.stage, g.year) for g in evidence.current_grades(text)]


def _quantities(text: str) -> set[tuple[float, str]]:
    return {(q.lo, q.label) for q in evidence.quantities(text)}


def _numbers_supported(value: str, quote: str) -> bool:
    return evidence.numbers_supported(value, quote)


def _negated_in(value: str, quote: str) -> bool:
    ctx = _context(quote)
    occ = evidence.span_occurrences(value, ctx.text)
    return bool(occ) and _occurrence_reason(occ, ctx, evidence.WISH) == "negated_in_evidence"


def _grade_supported(value: str, quote: str) -> bool | None:
    g = evidence.parse_grade(value)
    if g is None or g is evidence.BAD:
        return None if not evidence.grade_mentions(evidence.prep(value)) else False
    ctx = _context(quote)
    return evidence.grade_supported(g[0], g[1], ctx.text, ctx.windows)


def _with_aliases(text: str) -> str:
    low = evidence.prep(text)
    extra = [b for group in evidence.ALIAS_GROUPS for a in group if evidence._has(low, a) for b in group if b != a]
    return low + " " + " ".join(extra)


def _short_value_supported(key: str, value: str, quote: str, ctx: _Ctx | None = None) -> bool:
    """短值也得撑得住：结构化字段按上面的核对；别的短值至少要有一个内容字（算上常见缩写）出现在引文里，
    英文词要整个出现，说法要和原话一致。全是虚词的值（「会」）原来算作撑得住，「我没学过Python」→ base:code=会 就这么混进去。"""
    ctx = ctx or _context(quote)
    typed = _typed_value(key, value, ctx)
    if typed is not None and typed is not evidence.BAD:
        return typed[1] is None
    v = _content_chars(_with_aliases(value))
    if not v:
        raw = {c for c in str(value) if not c.isspace()}
        return bool(raw) and raw <= set(quote)
    return bool(v & _content_chars(_with_aliases(quote))) and not _words_missing(value, ctx) and not _scope_mismatch(key, value, ctx)


# 这条记忆改变未来的哪个决策。填不出就没资格进画像。
# 依据是「写入代价不对称」：少记一条只是下次再问一遍，记错一条会静默污染之后每个决策，
# 而且没人会发现——所以默认不写，要写就举证。
AFFECTS: tuple[tuple[str, str], ...] = (
    ("task_difficulty", "任务难度"),
    ("task_kind", "任务类型"),
    ("direction_choice", "方向建议"),
    ("question_next", "下一轮问什么"),
    ("course_pick", "课程推荐"),
    ("pace", "推进节奏"),
    ("feedback", "反馈方式"),
)
AFFECT_LABELS: dict[str, str] = dict(AFFECTS)
AFFECT_KEYS = set(AFFECT_LABELS)

# 只有这些 op 需要举证「改变了什么决策」；retract/support 是对已有事实的操作。
_AFFECT_REQUIRED = ("add", "replace")

# 模型 op 里这些字段只能是字符串或不填。
_OP_FIELDS = ("op", "key", "value", "category", "source", "evidence_quote", "target_fact_id", "affects", "valid_until")


def validate_ops(uid: str, ops: list[dict[str, Any]], evidence_pool: list[str]) -> tuple[list[dict], list[dict]]:
    """逐条校验。返回 (accepted, rejected)，rejected 带 reason 以便前端和日志展示。

    注意：这里不抛异常、不静默通过——任何一条拿不准就拒绝，并留下可读理由。
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    facts_by_id = {f.id: f for f in store.list_facts(uid)}

    for raw in ops or []:
        if not isinstance(raw, dict):
            rejected.append({"op": None, "reason": "not_an_object"})
            continue
        # 值是数字（入学年份 2024、年龄 20）先转成字符串：原来一律当类型不对拒掉，年份就记不下来（Codex 复现）
        num = raw.get("value")
        if isinstance(num, int) and not isinstance(num, bool) and abs(num) < 10 ** 12:
            raw = {**raw, "value": str(num)}
        elif isinstance(num, float) and math.isfinite(num) and abs(num) < 1e12:
            raw = {**raw, "value": f"{num:g}"}
        # 字段只收字符串（或不填）：原来 key 是列表就 .strip() 抛异常、整轮 500，valid_until 是 [] / {} 过了校验到 SQLite 才炸（Codex 复现）
        bad = next((f for f in _OP_FIELDS if not isinstance(raw.get(f), (str, type(None)))), None)
        # 年级和数量字段的值可以是结构化的对象（{stage, year} / {value, unit, per}），形状对不对在下面按字段查
        if bad == "value" and isinstance(raw.get("value"), dict) and isinstance(raw.get("key"), str) \
                and _kind(canon_key(raw["key"])) in ("grade", "quantity") \
                and not any(not isinstance(raw.get(f), (str, type(None))) for f in _OP_FIELDS if f != "value"):
            bad = None
        if bad:
            rejected.append({"op": raw.get("op"), "key": raw.get("key"), "value": raw.get("value"), "raw": raw,
                             "reason": f"bad_type:{bad}"})
            continue
        op = str(raw.get("op") or "").strip().lower()
        key = canon_key(raw.get("key") or "")
        item = {"op": op, "key": key, "value": raw.get("value"), "raw": raw}

        if op not in ("add", "replace", "retract", "support"):
            rejected.append({**item, "reason": "bad_op"})
            continue

        spec = spec_for(key)
        if spec is None:
            rejected.append({**item, "reason": "key_not_in_registry"})
            continue

        if spec.sources == ("behavior",):
            rejected.append({**item, "reason": "behavior_only:经历类事实只能由提交事件写入"})
            continue

        if spec.sources == ("derived",):
            # 成绩单推导由 memory.sync_transcript_facts 确定性生成。
            # 模型不能写：它没有底稿，只能编判断——那正是 base:code 事故。
            rejected.append({**item, "reason": "derived_only:成绩单结论由代码从底稿推导，模型不能写"})
            continue

        source = str(raw.get("source") or "declared").strip().lower()
        if source not in _MODEL_SOURCES:
            # 模型不得自称 behavior
            source = "declared"

        if not _evidence_ok(str(raw.get("evidence_quote") or ""), evidence_pool):
            rejected.append({**item, "reason": "evidence_not_found"})
            continue

        # 闸门③：必须说清这条记忆改变未来的哪个决策。
        affects = str(raw.get("affects") or "").strip().lower()
        if op in _AFFECT_REQUIRED:
            if not affects:
                rejected.append({**item, "reason": "no_affects:没说清它改变哪个决策"})
                continue
            if affects not in AFFECT_KEYS:
                rejected.append({**item, "reason": f"bad_affects:{affects}"})
                continue

        target = None
        changed_key = False
        retire_id = None
        if op in ("replace", "retract", "support"):
            tid = str(raw.get("target_fact_id") or "").strip()
            target = facts_by_id.get(tid)
            if target is None:
                rejected.append({**item, "reason": "target_fact_not_found"})
                continue
            if op == "replace" and str(target.key) != key:
                # 「把 direction:ai 改成 direction:math」：原来会存成 direction:ai = 数学，显示改了、规划和选项目还按 AI（Codex 复现）。
                # 键变了就不是改一条，是换一条：新值按 add 写，被替换的那条明确撤掉——
                # 不能只靠 add 的同类覆盖：goal:phd → goal:work 这种可多值的键不会被覆盖，两个目标会同时留着。
                retire_id = target.id
                op, target, changed_key = "add", None, True

        raw_value = raw.get("value")
        value = "" if isinstance(raw_value, dict) else str(raw_value or "").strip()
        if op in ("add", "replace") and not value and not isinstance(raw_value, dict):
            rejected.append({**item, "reason": "empty_value"})
            continue
        if len(value) > 200:
            value = value[:200]

        # 闸门②：引文得撑得住这个值。结构化字段按字段核对，别的按支撑度。
        quote = str(raw.get("evidence_quote") or "").strip()
        ratio = 1.0
        if op in ("add", "replace"):
            ctx = _context(quote, evidence_pool)
            typed = _typed_value(key, raw_value if isinstance(raw_value, dict) else value, ctx)
            if typed is evidence.BAD:
                rejected.append({**item, "reason": "bad_type:value"})
                continue
            if typed is not None:
                value, why = typed[:2]
                ratio = typed[2] if len(typed) > 2 else 1.0
                if why:
                    rejected.append({**item, "reason": why})
                    continue
            else:
                substantive = len(_content_chars(value)) >= _SUPPORT_MIN_CHARS
                # 短值也算支撑度（算上缩写）：原来一个字对上就收，「编程能力非常扎实」被「我刚开始学编程」撑住（Codex 复现）
                v = _content_chars(evidence.prep(value))
                ratio = support_ratio(value, quote) if substantive else (len(v & _content_chars(_with_aliases(quote))) / len(v) if v else 1.0)
                if v and ratio < _SUPPORT_FLOOR:
                    rejected.append({**item, "reason": f"value_exceeds_evidence:{ratio:.2f}" if substantive else "short_value_not_in_evidence"})
                    continue
                if key.split(":")[0] not in _TEXT_LOOSE and _extra_claims(value, ctx):
                    rejected.append({**item, "reason": f"value_exceeds_evidence:{ratio:.2f}"})
                    continue
                if _words_missing(value, ctx):
                    rejected.append({**item, "reason": "short_value_not_in_evidence"})
                    continue
                if _scope_mismatch(key, value, ctx):
                    rejected.append({**item, "reason": "negated_in_evidence"})  # 「我没学过Python」撑不住「会Python」
                    continue
                if not substantive and not _short_value_supported(key, value, quote, ctx):
                    # 短值原来跳过支撑度检查：学生说「我大二」，模型写年级「博士」也照样按 declared 存进去（Codex 复现）
                    rejected.append({**item, "reason": "short_value_not_in_evidence"})
                    continue
                if not _numbers_supported(value, quote):
                    rejected.append({**item, "reason": "number_not_in_evidence"})
                    continue

        valid_until = raw.get("valid_until")
        if spec.requires_valid_until:
            if not valid_until or _parse_when(valid_until) is None:
                rejected.append({**item, "reason": "constraint_requires_valid_until"})
                continue
        elif valid_until and _parse_when(valid_until) is None:
            valid_until = None

        notes: list[str] = ["replace_key_changed_as_add"] if changed_key else []
        # 引文只撑住一部分 → 说明值里有模型的推断成分。不丢，但降级成 inferred，
        # 免得「用户说的」和「模型推的」在画像里长得一样（这是 base:code 那次事故的根因）。
        if op in ("add", "replace") and ratio < _SUPPORT_OK:
            source = "inferred"
            notes.append("partially_inferred")
        if spec.category == "capability" and source != "behavior":
            if any(w in value for w in _MASTERY_WORDS):
                # 自述「已掌握」但没有行为证据：保留但不据此升级难度（02 §10.1）
                notes.append("needs_behavior_evidence")

        # 「当前实践」不填有效期时自动给一个，过期后会被重新问，而不是一直当现状用。
        if spec.default_ttl_days and not valid_until:
            valid_until = _plus_days(spec.default_ttl_days)

        accepted.append({
            "op": op,
            "key": key,
            "category": spec.category,
            "cardinality": spec.cardinality,
            "layer": spec.layer,
            "value": value,
            "source": source,
            "confidence": confidence_for(source),
            "valid_until": valid_until,
            "affects": affects or None,
            "support_ratio": round(ratio, 3),
            "evidence_quote": str(raw.get("evidence_quote") or "").strip()[:300],
            "target_fact_id": target.id if target else None,
            "retire_fact_id": retire_id,
            "notes": notes,
        })

    return accepted, rejected


def validate_dialogue_ops(uid, ops, evidence_pool):
    """Retry a lexical rejection with the verbatim quote, never a looser gate.

    A model paraphrase can contain unsupported details even when its cited
    user sentence is useful. Preserve only that sentence, re-running every
    existing source, negation, subject, temporal and registry check.
    Identity/grade and behavior/derived fields never take this path.
    """
    accepted, rejected = validate_ops(uid, ops, evidence_pool)
    remaining = []
    for item in rejected:
        raw = item.get("raw") or {}
        if not isinstance(raw, dict) or not isinstance(raw.get("key"), str):
            remaining.append(item)
            continue
        key = canon_key(raw.get("key") or "")
        quote = str(raw.get("evidence_quote") or "").strip()
        unsafe_scope = any(s.kind in (evidence.OTHER, evidence.IRREALIS, evidence.COND,
                                     evidence.MENTION, evidence.QUESTION)
                           for s in evidence.scan(quote).scopes)
        if (raw.get("op") in ("add", "replace")
                and key.split(":")[0] in ("interest", "goal", "field", "career", "current", "base", "project", "style")
                and str(item.get("reason", "")).split(":")[0] in ("value_exceeds_evidence", "short_value_not_in_evidence")
                and 12 <= len(quote) <= 200 and not unsafe_scope):
            retry, _ = validate_ops(uid, [{**raw, "value": quote}], evidence_pool)
            if retry:
                retry[0]["notes"].append("verbatim_quote_recovered")
                accepted.extend(retry)
                continue
        remaining.append(item)
    return accepted, remaining


# ---------- 提交 ----------


def _supersede_existing(uid: str, key: str, spec: KeySpec, keep: str | None,
                        decision_id: str) -> list[dict[str, Any]]:
    """按基数把旧事实置为 superseded，返回被改动的记录。"""
    changed: list[dict[str, Any]] = []
    for f in store.list_facts(uid, statuses=list(DECISION_STATUSES)):
        if f.id == keep:
            continue
        hit = f.key == key if spec.cardinality == "single" else (
            f.key.startswith(key.split(":", 1)[0] + ":") if spec.cardinality == "exclusive" else False
        )
        if not hit:
            continue
        store.update_fact(f.id, status="superseded")
        store.add_revision(uid, fact_id=f.id, operation="supersede", old_value=f.value,
                           new_value=None, evidence=f.evidence, decision_id=decision_id)
        changed.append({"id": f.id, "key": f.key, "status": "superseded", "value": f.value})
    return changed


def _retire(uid: str, fid: str | None, evidence: list[dict], decision_id: str, keep: str | None = None) -> list[dict]:
    """改键的 replace：把被替换的那条撤掉（已经不在用的、或者正好就是要留下的那条，不动）。"""
    if not fid or fid == keep:
        return []
    old = store.get_fact(fid)
    if not old or old.status not in DECISION_STATUSES:
        return []
    store.update_fact(old.id, status="superseded")
    store.add_revision(uid, fact_id=old.id, operation="retract", old_value=old.value,
                       new_value=None, evidence=evidence, decision_id=decision_id)
    return [{"id": old.id, "key": old.key, "status": "superseded", "value": old.value, "retracted": True}]


def apply_ops(uid: str, accepted: list[dict], decision_id: str) -> tuple[list[dict], list[dict]]:
    """把校验通过的操作写库。返回 (added, changed)。每条都留 revision。

    幂等：同 key 同 value 已存在时只补证据、不再新增行——同一轮重试不会重复计数（archive M7）。
    """
    added: list[dict] = []
    changed: list[dict] = []
    live_by_key: dict[str, list[UserFact]] = {}
    for f in store.list_facts(uid, statuses=list(DECISION_STATUSES)):
        live_by_key.setdefault(f.key, []).append(f)

    for op in accepted:
        spec = KeySpec(op["category"], op["cardinality"], tuple(_MODEL_SOURCES),
                       requires_valid_until=False)
        kind = op["op"]
        evidence = [{"type": "dialogue_turn", "quote": op["evidence_quote"], "decision_id": decision_id}]

        if kind == "add":
            dup = next((f for f in live_by_key.get(op["key"], []) if f.value == op["value"]), None)
            if dup is not None:
                changed += _retire(uid, op.get("retire_fact_id"), evidence, decision_id, keep=dup.id)
                store.update_fact(dup.id, evidence=list(dup.evidence) + evidence)
                store.add_revision(uid, fact_id=dup.id, operation="support", old_value=dup.value,
                                   new_value=dup.value, evidence=evidence, decision_id=decision_id)
                changed.append({"id": dup.id, "key": dup.key, "value": dup.value,
                                "status": dup.status, "supported": True})
                continue

            changed += _supersede_existing(uid, op["key"], spec, keep=None, decision_id=decision_id)
            fact = UserFact(
                user_id=uid, category=op["category"], key=op["key"], value=op["value"],
                confidence=op["confidence"], source=op["source"], evidence=evidence,
                status="confirmed", valid_until=op["valid_until"],
                affects=op.get("affects") or "",
            )
            store.add_fact(fact)
            store.add_revision(uid, fact_id=fact.id, operation="add", old_value=None,
                               new_value=fact.value, evidence=evidence, decision_id=decision_id)
            live_by_key.setdefault(op["key"], []).append(fact)
            added.append({**fact.to_dict(), "notes": op["notes"]})
            changed += _retire(uid, op.get("retire_fact_id"), evidence, decision_id)

        elif kind == "replace":
            target = store.get_fact(op["target_fact_id"] or "")
            if not target:
                continue
            old_value = target.value
            merged = list(target.evidence) + evidence
            # 来源、可信度、affects 也换成这次验过的：原来只换值，部分推断的新值还挂着旧的 user_edit / 1.0（Codex 复现）
            store.update_fact(target.id, value=op["value"], status="confirmed",
                              evidence=merged, valid_until=op["valid_until"],
                              source=op["source"], confidence=op["confidence"],
                              affects=op.get("affects") or target.affects or "")
            store.add_revision(uid, fact_id=target.id, operation="replace", old_value=old_value,
                               new_value=op["value"], evidence=evidence, decision_id=decision_id)
            changed.append({**(store.get_fact(target.id) or target).to_dict(),
                            "old_value": old_value, "notes": op["notes"]})

        elif kind == "support":
            target = store.get_fact(op["target_fact_id"] or "")
            if not target:
                continue
            merged = list(target.evidence) + evidence
            store.update_fact(target.id, evidence=merged)
            store.add_revision(uid, fact_id=target.id, operation="support", old_value=target.value,
                               new_value=target.value, evidence=evidence, decision_id=decision_id)
            changed.append({"id": target.id, "key": target.key, "value": target.value,
                            "status": target.status, "supported": True})

        elif kind == "retract":
            target = store.get_fact(op["target_fact_id"] or "")
            if not target:
                continue
            store.update_fact(target.id, status="superseded")
            store.add_revision(uid, fact_id=target.id, operation="retract", old_value=target.value,
                               new_value=None, evidence=evidence, decision_id=decision_id)
            changed.append({"id": target.id, "key": target.key, "status": "superseded",
                            "value": target.value, "retracted": True})

    return added, changed


# ---------- §4 召回 ----------


def active_facts(uid: str) -> list[UserFact]:
    """当前有效的事实：状态在白名单内且未过期。"""
    now = datetime.now(timezone.utc)
    return [f for f in store.list_facts(uid, statuses=list(DECISION_STATUSES))
            if not is_expired(f, now)]


def _terms(query: str) -> set[str]:
    return {t for t in re.split(r"[^\w\u4e00-\u9fff]+", (query or "").lower()) if len(t) >= 2}


def _relevance(f: UserFact, terms: set[str]) -> float:
    """层内排序用。只认多字词命中——原来那个「单字命中 0.05/字」在中文里
    会让一句话的每个字都命中一堆无关事实，基本是噪声，已去掉。"""
    text = f"{f.key} {f.value}".lower()
    hits = sum(1 for t in terms if t in text)
    if f.key.lower() in terms:
        hits += 1.5          # 用户原话直接点名了这个 key
    # 硬证据优先。层内坑位不够时，成绩单推出来的事实应该压过自述和推断——
    # 「修过 3 门数学类课程，加权平均 88.3」比「我觉得我数学还行」更该被看见。
    # 0.9 是刻意小于 1.0：相关度仍然能压过它（用户直接问到某个自述时，
    # 那条应该排前面），但同等相关度时硬证据赢。
    if f.source == "derived":
        hits += 0.9
    return float(hits)


# 项目属性的固定集合。顺序就是界面上的显示顺序。
# 它不只是显示用的排序表——下面 `_project_parts` 靠它来切分 key，
# 所以它是**格式定义**的一部分，增删要一起想。
PROJECT_ATTRS: tuple[str, ...] = ("what", "traction", "stage", "stack", "blocker", "next")
PROJECT_ATTR_LABEL = {"what": "是什么", "traction": "成果", "stage": "阶段",
                      "stack": "技术", "blocker": "卡点", "next": "下一步"}


def _project_parts(key: str) -> tuple[str, str]:
    """把 project:<实体>[.<属性>] 切成 (实体, 属性)。不是项目就返回 ("", "")。

    为什么不能简单按第一个点切：key 里**允许出现点**
    （`_SLUG_RE` 明确放行 `.` 和 `-`），所以
        project:my.app.blocker
    按第一个点切会得到实体 "my"、属性 "app.blocker"——
    于是 `project:my.app` 和 `project:my.other` 会塌成同一个项目，
    两张卡合成一张，属性互相串。
    按最后一个点切又会在 `project:kotoba.next` 上出错（实体变 "kotoba.next"）。

    做法：**只在最后一段是已知属性时才切**。这不是猜，是用格式定义消歧：
      project:kotoba.blocker → ("kotoba", "blocker")
      project:my.app         → ("my.app", "")        ← app 不是属性，整段是实体
      project:my.app.blocker → ("my.app", "blocker")
      project:my.app.why     → ("my.app.why", "")    ← 未知属性，整段当实体
                                                       （宁可建一张新卡，也不要把
                                                        两条无关的东西并到一起）
    """
    if not key.startswith("project:"):
        return "", ""
    rest = key.split(":", 1)[1]
    if not rest:
        return "", ""
    if "." in rest:
        head, _, tail = rest.rpartition(".")
        if head and tail in PROJECT_ATTRS:
            return head, tail
    return rest, ""


def _project_slug(key: str) -> str:
    """project:kotoba → kotoba；project:kotoba.blocker → kotoba；其它 → ""。"""
    return _project_parts(key)[0]


def _complete_project_groups(selected: list[UserFact], alive: list[UserFact]) -> list[UserFact]:
    """项目整卡进出。

    项目是整体：只知道「KotobaAI 是什么」而不知道「卡在哪」，会得出错误的下一步；
    反过来只知道卡点不知道是什么更糟。所以只要某项目有任何一条进了上下文，
    就把这个项目的全部属性一起带上——**允许超出该层预算**，这是有意的。

    这和「按相关度部分注入」不冲突：那条规则管的是**不同事实之间**谁该进来，
    项目内部则是一个整体，不能切。
    """
    slugs = {s for s in (_project_slug(f.key) for f in selected) if s}
    if not slugs:
        return selected
    have = {f.id for f in selected}
    extra = [f for f in alive
             if f.id not in have and _project_slug(f.key) in slugs]
    return selected + extra


def recall_grouped(uid: str, query: str = "",
                   budgets: dict[str, int] | None = None) -> tuple[dict[str, list[UserFact]], list[str]]:
    """按层召回，每层给固定预算。返回 ({layer: [facts]}, 被过滤的过期 id)。

    为什么不再用「平表按关键词取 top N」：全局抢坑位时，一门课的成绩可能挤掉他的方向，
    而且没有任何信号告诉模型「记忆被截断了」。按时间跨度分层后，身份/当前实践/约束
    这几层小且必带，倾向/经历按相关度填——这正是「不是全量注入，是每层给预算」。
    """
    facts = store.list_facts(uid, statuses=list(DECISION_STATUSES))
    now = datetime.now(timezone.utc)
    expired = [f.id for f in facts if is_expired(f, now)]
    alive = [f for f in facts if not is_expired(f, now)]
    caps = {**LAYER_BUDGET, **(budgets or {})}
    terms = _terms(query)

    grouped: dict[str, list[UserFact]] = {name: [] for name, _ in LAYERS}
    for name, _ in LAYERS:
        members = [f for f in alive if layer_of(f.key) == name]
        # 层内：相关度优先，其次新近——新近只在平局时起作用，不喧宾夺主。
        members.sort(key=lambda f: (_relevance(f, terms), f.created_at or ""), reverse=True)
        grouped[name] = _complete_project_groups(members[:caps.get(name, 4)], alive)
    return grouped, expired


def project_cards(uid: str) -> list[dict[str, Any]]:
    """把 project:* 聚成「一个项目一张卡」，给界面用。

    返回 [{slug, name, facts: [...]}]，属性按固定顺序排。
    存的时候是分开的事实，这里只做展示聚合——不改存储，所以证据、TTL、
    单独改删都还在每一条上。
    """
    facts = [f for f in store.list_facts(uid, statuses=list(DECISION_STATUSES))
             if _project_slug(f.key)]
    order = list(PROJECT_ATTRS)
    label = PROJECT_ATTR_LABEL
    by_slug: dict[str, list[UserFact]] = {}
    for f in facts:
        by_slug.setdefault(_project_slug(f.key), []).append(f)

    out = []
    for slug, group in by_slug.items():
        def attr_of(f: UserFact) -> str:
            return _project_parts(f.key)[1]

        def rank(f: UserFact) -> int:
            a = attr_of(f)
            return order.index(a) if a in order else len(order)

        group.sort(key=rank)
        # 项目名：优先实体那条（属性为空的那条），否则用 slug
        entity = next((f for f in group if not attr_of(f)), None)
        out.append({
            "slug": slug,
            "name": (entity.value if entity else slug) or slug,
            # 属性名要**从同一个切分函数**取，否则卡片名和分组会对不上
            # （这正是 my.app 那类 key 会踩的坑：一边切第一个点、一边切最后一个点）
            # 没有属性的那条是实体本身，显示成「是什么」。
            "attr_label": {f.id: (label.get(attr_of(f), "其它") if attr_of(f) else "是什么")
                           for f in group},
            "facts": group,
        })
    out.sort(key=lambda c: c["slug"])
    return out


def recall(uid: str, query: str, limit: int = 8) -> tuple[list[UserFact], list[str]]:
    """兼容旧签名：按层召回后拉平。limit 只作为兜底上限，不再决定选谁。"""
    grouped, expired = recall_grouped(uid, query)
    flat: list[UserFact] = []
    for name, _ in LAYERS:
        flat.extend(grouped.get(name) or [])
    return (flat[:limit] if limit and limit > 0 else flat), expired


# ---------- 用户自己看/改记忆 ----------
#
# 为什么用户能改：画像里的每一条都会影响之后给他的建议。如果一条是错的而他又看不见，
# 他只能吃下去——「个性化必须有依据」要求这个依据是可查、可纠正的。


def board(uid: str) -> dict[str, Any]:
    """按层返回全部生效记忆，供界面分列展示。每层附上「这层是干什么的」。"""
    now = datetime.now(timezone.utc)
    all_facts = store.list_facts(uid, statuses=list(DECISION_STATUSES))
    alive = [f for f in all_facts if not is_expired(f, now)]
    expired = [f for f in all_facts if is_expired(f, now)]

    layers = []
    other = [f for f in alive if is_pruned_key(f.key)]
    for name, label in LAYERS:
        items = [f for f in alive if layer_of(f.key) == name and not is_pruned_key(f.key)]
        items.sort(key=lambda f: f.created_at or "")
        layers.append({
            "layer": name,
            "label": label,
            # hint 描述的是「这一层」，所以挂在层上；挂在每条 fact 上前端会取不到。
            "layer_hint": LAYER_HINT.get(name, ""),
            "facts": [{
                **f.to_dict(),
                "layer": name,
                "layer_label": label,
                "affects_label": AFFECT_LABELS.get(f.affects or "", ""),
            } for f in items],
        })
    if other:
        other.sort(key=lambda f: f.created_at or "")
        layers.append({
            "layer": "other",
            "label": "其他（不用于选方向）",
            "layer_hint": "旧数据还在，可以看、可以删，不再用来选方向。",
            "facts": [{
                **f.to_dict(),
                "layer": "other",
                "layer_label": "其他（不用于选方向）",
                "affects_label": AFFECT_LABELS.get(f.affects or "", ""),
            } for f in other],
        })
    return {
        "layers": layers,
        # 项目文件夹：一个项目一张卡。存的时候是分开的事实，这里只做展示聚合。
        "projects": project_cards(uid),
        "memory_version": store.get_memory_version(uid),
        "expired_count": len(expired),
        "expired": [{"id": f.id, "key": f.key, "value": f.value,
                     "valid_until": f.valid_until} for f in expired],
    }


LAYER_HINT: dict[str, str] = {
    "identity": "你是谁：年级、院系、专业。几乎不会变。",
    "practice": "你此刻手头在做的事。变化最快，过期后我会重新问。",
    "capability": "你的底子：数学、编程、学过的课。",
    "experience": "你实际完成过的事，只能由提交记录写入。",
    "interest": "你想往哪走：兴趣、目标、方向。",
    "constraint": "临时的限制，到点自动失效。",
}

# 提问阶梯。顺序 = LAYERS 的顺序，这里只写「问到什么算这层有数了」。
# 为什么要有阶梯：用户实测反馈「我只说了个大二，怎么跳过学校先问课程了」——
# 追问不自然，本质上就是**跳级**：还没弄清他是谁、在做什么，就问到了抽象偏好。
#
# 只放 allow-ask 的 key：gender/age 这类 ask=False 的不能算进「这层有数了没有」，
# 否则阶梯会一直卡在身份层等一个我们根本不该问的字段。
LADDER_PROBE: dict[str, tuple[str, ...]] = {
    "identity": ("grade", "department", "major"),
    "practice": ("current:", "project:", "research_group"),
    "capability": ("base:", "capability:"),
    "experience": ("experience:",),
    "interest": ("direction:", "goal:", "interest:", "field:"),
}


LADDER_ORDER = ("identity", "interest", "capability", "practice")


def ladder_state(uid: str) -> dict[str, Any]:
    """各层「有数了没有」，以及最早一个还没数的层。

    代码只负责给出事实（哪层空着），**问还是不问、怎么问，交给模型**。
    这里刻意不做成硬闸门：问题是一句自由文本，靠关键词判它「在问哪一层」很脆，
    而且本轮只有两次模型调用，没有余量再去分类一次。
    所以设计是：**代码给真相，模型定措辞，评测管结果**。

    只有 `ask=True` 的 key 参与「这层有数了没有」的判断：
    `gender`/`age` 这种存得下但不该主动问的字段，不能把阶梯卡住。
    """
    alive = active_facts(uid)
    keys = [f.key for f in alive]
    layers = []
    for name, label in LAYERS:
        probes = LADDER_PROBE.get(name, ())
        if not probes:
            continue
        in_layer = [k for k in keys if layer_of(k) == name]
        hit = [k for k in in_layer
               if (spec_for(k) is None or spec_for(k).ask)
               and any(k == p or k.startswith(p) for p in probes)]
        layers.append({
            "layer": name,
            "label": label,
            "known": bool(hit),
            "have": hit[:4],
            "stored_only": [k for k in in_layer if k not in hit][:4],
            "hint": LAYER_HINT.get(name, ""),
        })
    order = {n: i for i, n in enumerate(LADDER_ORDER)}
    nxt = next((n for n in LADDER_ORDER
                if not next((x["known"] for x in layers if x["layer"] == n), False)), None)
    return {
        "layers": layers,
        "next_layer": nxt,
        "next_label": (LAYER_LABELS.get(nxt, nxt) if nxt else None),
    }


def user_edit(uid: str, fact_id: str, value: str) -> dict[str, Any] | None:
    """用户亲手改一条记忆。返回改后的事实；不属于这个画像则返回 None。

    用户自己说的算最高可信（1.0）——他是关于自己的最终权威，
    这比模型从对话里推断出来的任何东西都硬。
    """
    f = store.get_fact(fact_id)
    if not f or f.user_id != uid:
        return None
    new_value = (value or "").strip()[:200]
    if not new_value:
        return None
    old = f.value
    evidence = list(f.evidence) + [{"type": "user_edit", "quote": new_value, "decision_id": ""}]
    store.update_fact(fact_id, value=new_value, status="confirmed", evidence=evidence)
    # 用户直接改的：来源升级为 user_edit，置信度 1.0，并且不再挂「模型推断」的标注。
    with store._LOCK, store._conn() as c:  # noqa: SLF001 — 单字段更新，避免为它开新 API
        c.execute("UPDATE facts SET source=?, confidence=?, affects=? WHERE id=?",
                  ("user_edit", 1.0, f.affects or "", fact_id))
    store.add_revision(uid, fact_id=fact_id, operation="replace", old_value=old,
                       new_value=new_value, evidence=evidence, decision_id="")
    store.bump_memory_version(uid)
    updated = store.get_fact(fact_id)
    return updated.to_dict() if updated else None


def user_retract(uid: str, fact_id: str) -> bool:
    """用户删掉一条记忆。不是硬删除：置为 retracted 并留 revision，可追溯。"""
    f = store.get_fact(fact_id)
    if not f or f.user_id != uid:
        return False
    store.retract_fact(fact_id)
    # 成绩单推出来的结论要额外记一笔「用户否掉了」。
    # 否则他删掉之后，下次成绩单一动，sync 会把它**原样加回来**——
    # 用户会发现自己删不掉一条系统认定的东西，这比少一条记忆糟得多。
    if str(f.key).startswith("transcript:"):
        store.add_revision(uid, fact_id=fact_id, operation="user_reject",
                           old_value=f.value, new_value=None,
                           evidence=list(f.evidence))
    store.bump_memory_version(uid)
    return True


# ---------- 从成绩单推导能力（transcript:*）----------

def sync_transcript_facts(uid: str) -> dict[str, Any]:
    """按当前成绩单重新推导 `transcript:*` 事实。**幂等**，可以反复调。

    增 / 改 / 撤 三件事一起做。撤这一步最要紧：
    成绩单改了之后，**过期的推导结论必须撤掉**。用户把那些课删了，
    而「他修过 3 门数学类课程」还留着——那比没有更糟，
    因为它看起来是**最硬的那类证据**，模型和用户都会信它。

    这条事实的 value 由 transcript.py 确定性拼出来（只写「修过哪几门、多少分、
    加权平均多少」），模型写不了（registry 里 sources=("derived",)）。
    这是 base:code 那起事故的正面修法：**能力不再是模型凭一段话编的判断。**
    """
    from schemas import UserFact  # 延迟导入，和其它写路径保持一致

    desired = {d["key"]: d for d in
               transcript.derive_capabilities(store.list_enrollments(uid))}
    # 同一个 key 可能有几条（老库）：原来只看最后一条，其余的永远不撤，删了课「Python 90」还挂着（Codex 复现）。
    # 每个 key 留一条（用户改过的优先），多出来的撤掉（用户改过的不动）。
    groups: dict[str, list] = {}
    for f in active_facts(uid):
        if str(f.key).startswith("transcript:"):
            groups.setdefault(str(f.key), []).append(f)
    existing = {}
    dup_removed: list[str] = []
    for key, fs in groups.items():
        fs.sort(key=lambda f: (f.source != "user_edit", str(f.created_at)))
        existing[key] = fs[0]
        for extra in fs[1:]:
            if extra.source == "user_edit":
                continue
            store.retract_fact(extra.id)
            store.add_revision(uid, fact_id=extra.id, operation="retract", old_value=extra.value,
                               new_value=None, evidence=list(extra.evidence))
            dup_removed.append(key)

    # 两种「用户已经表过态」的情况，sync 必须让路，否则就是系统在覆盖用户：
    #   1. 他改过这条 —— 来源变成 user_edit，他才是关于他自己的权威。
    #   2. 他删过这条 —— revision 里记了 user_reject，永久生效。
    # 让路的代价只是少一条注入的结论（成绩单还在，工具照样查得到）；
    # 不让路的代价是他发现有一条东西删不掉、也改不了。
    #
    # 注意两点：user_reject 是**按 key** 记的，不是按 fact id——
    # 用户否掉之后我们可能删掉旧行、以后又新建一行，id 会变，key 不会。
    # 另外被保护的 key 既不能加、不能改，**也不能撤**（他改过的那条要留着）。
    protected: set[str] = set()
    for rev in store.list_revisions(uid):
        if rev.get("operation") != "user_reject":
            continue
        f = store.get_fact(rev["fact_id"])
        if f and str(f.key).startswith("transcript:"):
            protected.add(str(f.key))
    for key, f in existing.items():
        if f.source == "user_edit":
            protected.add(key)

    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []

    for key, d in desired.items():
        if key in protected:
            continue
        evidence = [{"type": "transcript", **e} for e in d["enrollments"]]
        cur = existing.get(key)
        if cur is None:
            store.add_fact(UserFact(
                user_id=uid, category="capability", key=key, value=d["value"],
                confidence=_CONFIDENCE_BY_SOURCE["derived"], source="derived",
                evidence=evidence, status="active",
                affects=d["affects"]))
            store.add_revision(uid, fact_id=key, operation="derive", old_value=None,
                               new_value=d["value"], evidence=evidence)
            added.append(key)
        elif cur.value != d["value"]:
            store.update_fact(cur.id, value=d["value"], evidence=evidence)
            store.add_revision(uid, fact_id=cur.id, operation="replace",
                               old_value=cur.value, new_value=d["value"], evidence=evidence)
            updated.append(key)

    for key, f in existing.items():
        if key in protected or key in desired:
            continue
        # 「以前推得出来、现在推不出来」的正常过期
        store.retract_fact(f.id)
        store.add_revision(uid, fact_id=f.id, operation="retract",
                           old_value=f.value, new_value=None,
                           evidence=list(f.evidence))
        removed.append(key)

    if added or updated or removed or dup_removed:
        store.bump_memory_version(uid)
    return {"added": added, "updated": updated, "removed": removed,
            "protected": sorted(protected), "keys": sorted(desired)}
