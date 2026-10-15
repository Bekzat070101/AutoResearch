# -*- coding: utf-8 -*-
"""对话内核：环境观察 → 决策 → 工具调用 → 记忆更新。

契约见 docs/DIALOGUE_CONTRACT.md。一轮最多两次模型调用：

  请求
   └─ 组装环境包（代码，只读，按当前画像）
       └─ 调用① TurnProposal
           ├─ 校验 memory_ops → 提交（memory.py）
           ├─ tool_intent == null ? 用 dialogue.reply 收口
           └─ tool_intent != null
               ├─ 执行工具（tools.py，只读）
               └─ 调用② TurnReply（带工具结果）

与 W0 的区别：W0 是「代码用规则选好动作，模型只改措辞」（planner 的 S0/S1/S2）。
这里反过来——模型决定做什么，代码决定能不能做。
"""
from __future__ import annotations

import json
import re
from typing import Any

import llm
import memory
import store
import tools
import transcript
import workbench
from schemas import now_iso

RECENT_TURNS = 12          # 进环境包的最近消息条数
# 召回**不再有一个总的条数上限**。原来这里是 limit=10，而 recall 是按层拉平的，
# 于是靠后的层（倾向、临时约束）会被这个总数截掉——等于把「平表抢坑位」换了个地方犯。
# 现在预算完全由 memory.LAYER_BUDGET 逐层决定（各层名额之和就是上限）。
# 留一个很大的安全上限只为防止异常数据把上下文撑爆，正常路径碰不到它。
RECALL_HARD_CAP = 40

VALID_MOVES = ("answer", "clarify", "propose_action", "execute_action", "correct", "acknowledge_stop")
VALID_NEXT_ACTIONS = ("explore_direction",)


# ---------- §4 环境包 ----------


def build_env(uid: str, conversation_id: str, message: str) -> dict[str, Any]:
    """按契约 §4 的顺序组装本轮环境包。先按当前画像取，再组装。"""
    # 按层召回：每层先取够自己的名额，再按层序拉平。不做全局截断（见 RECALL_HARD_CAP）。
    grouped, expired = memory.recall_grouped(uid, message)
    recalled = [f for _layer, _label in memory.LAYERS for f in grouped.get(_layer, [])]
    recalled = recalled[:RECALL_HARD_CAP]
    recent = store.list_messages(uid, conversation_id=conversation_id, limit=RECENT_TURNS)
    facts = memory.active_facts(uid)
    direction_fact = next((f for f in facts if f.key.startswith("direction:")), None)
    pending = store.pending_action(uid)
    ctx = projects_context(uid)
    pid = store.active_portrait_id(uid) or "default"
    trail = store.get_trail(uid, pid) or {}
    trail_dir = str(trail.get("dir") or "")
    # 用户这句话里出现的院系/专业（多半是简称：「信管」「数院」），代码先把库里的
    # 原文事实查好放进环境包。这样他一句「我是信管的大二」之后，模型手上就有名单，
    # 不用等他再说「帮我查一下」，也不会去反问「你是哪个学校」。
    lib_facts = tools.kb_env_facts(uid, message)

    return {
        "uid": uid,
        "portrait_id": uid,
        "memory_version": store.get_memory_version(uid),
        "time": now_iso(),
        "current_message": message,
        "recent_context": [{"role": m["role"], "text": m["text"]} for m in recent],
        "active_plan": {
            "direction": trail_dir or (direction_fact.key.split(":", 1)[1] if direction_fact else ""),
            "trail_dir": trail_dir,
            "direction_source": (direction_fact.key if direction_fact else ""),
            "practice_stage": ctx.get("stage"),
            "practice_stage_label": ctx.get("stage_label"),
            "pending_action": ({"action_id": pending["id"], "action": pending["action"],
                                "title": pending["title"], "status": pending["status"]}
                               if pending else None),
        },
        "recalled_memory": [
            {"id": f.id, "key": f.key, "category": f.category, "value": f.value,
             "source": f.source, "confidence": f.confidence,
             "layer": memory.LAYER_LABELS.get(memory.layer_of(f.key), memory.layer_of(f.key)),
             "affects": f.affects or None,
             "valid_until": f.valid_until,
             "evidence_quote": ((f.evidence or [{}])[-1].get("quote") if f.evidence else None),
             # 成绩单推出来的事实没有「用户原话」可引，它的依据是具体的课程和分数。
             # 把依据一起给模型：它才能在用户问「你怎么知道的」时说出是哪几门课，
             # 也才不会把这条硬证据当成又一个模糊印象。
             "derived_from": ([f"{e.get('course')} {e.get('grade')}"
                               for e in (f.evidence or [])
                               if e.get("type") == "transcript"] or None)}
            for f in recalled
        ],
        "ladder": memory.ladder_state(uid),
        "recent_events": [
            {"kind": e["kind"], "at": e["created_at"], "payload": e.get("payload")}
            for e in store.list_events(uid, kinds=["course_done", "project_submitted",
                                                   "task_submitted"], limit=5)
        ],
        "world_knowledge": [],
        # 库内事实（代码查的，不是模型的印象）：这一轮说到院系/专业时非空
        "lib_facts": lib_facts,
        "tool_results": [],
        # 可观测：召回了哪些、过滤了哪些过期项、基于哪个 memory_version（契约 §4）
        "trace": {
            "recalled": [f.id for f in recalled],
            "filtered_expired": expired,
            "memory_version": store.get_memory_version(uid),
            "lib_facts": [f["用户说到的词"] for f in lib_facts],
        },
    }


def projects_context(uid: str) -> dict[str, Any]:
    """延迟导入，避免 dialogue ↔ projects 的循环依赖。"""
    import projects
    try:
        return projects.context(uid)
    except Exception:
        return {}


# ---------- prompt ----------


def _registry_doc() -> str:
    """把可写 key 按「时间跨度层」列出来。

    分层不只是展示用的：它同时是提问顺序、注入预算和「什么时候该重新问」的依据。
    让模型看到层名，它更容易判断一条信息属于「身份」还是「当前实践」。

    **只列模型真的写得了的 key。** 代码推出来的（transcript:*）和只能由提交事件
    写入的（experience:*）都不列——列了就是叫它去撞一堵墙：写回来必被拒，
    白花一次调用，还让它以为自己写成功了。
    """
    writable = {"declared", "inferred"}
    lines = ["可写 key（不在表内的会被拒绝）。按「时间跨度层」分组："]
    for layer, label in memory.LAYERS:
        keys = []
        for key, spec in memory._EXACT.items():
            if spec.layer == layer and set(spec.sources) & writable:
                keys.append(f"{key}（{spec.cardinality}{'，别问' if not spec.ask else ''}）")
        for ns, spec in memory._NAMESPACED.items():
            if spec.layer != layer or not (set(spec.sources) & writable):
                continue
            extra = "，必须带 valid_until" if spec.requires_valid_until else ""
            if spec.default_ttl_days:
                extra = f"，不填有效期时自动给 {spec.default_ttl_days} 天"
            if not spec.ask:
                extra += "，别问"
            keys.append(f"{ns}:<slug>（{spec.cardinality}{extra}）")
        if keys:
            lines.append(f"· {label}［{layer}］: " + "；".join(keys))
    lines.append("")
    lines.append("有几种 key 你**写不了**，写了会被拒：")
    lines.append("· experience:<slug>：经历只能由提交事件写入。用户讲了做过的事，"
                 "写进对应的 practice / interest 层，别自己造一条经历。")
    lines.append("· transcript:<slug>：成绩单推出来的结论，由代码从底稿算出来。"
                 "它已经在「我记住的」里了（source=derived，带 derived_from），"
                 "**直接引用，不要重写**。用户说他修过什么课，让他去「我的」页粘成绩单。")
    lines.append("另外要注意 capability:<slug> 是**用户自述或你推断**的能力印象"
                 "（「我会一点 Python」），和 transcript:* 的硬证据是两回事，别混着写。")
    lines.append("标了「别问」的字段（性别、年龄）：用户主动说了就存，**但不要追问**——"
                 "它们几乎不改变你的建议，追问只会让人觉得像在填表。")
    lines.append("「当前实践」= 他此刻手头在做的事（在上的课、在做的项目、在跟的组、卡在哪），"
                 "这是最值钱也最容易过期的一层，问到了就写，别塞进别的层。")
    lines.append("")
    lines.append("【项目要写成一个文件夹，不要拆散】")
    lines.append("用户提到他在做的一个具体项目（比如自己的 App、网站、课题、比赛作品）时，"
                 "用 project:<slug> 建一个项目实体，属性用点号挂在它下面：")
    lines.append("· project:<slug>          这个项目是什么（一句话）")
    lines.append("· project:<slug>.what     同上，用于补充说明")
    lines.append("· project:<slug>.traction 成果、数据、用户量")
    lines.append("· project:<slug>.stage    到什么阶段了")
    lines.append("· project:<slug>.stack    用了什么技术/方法")
    lines.append("· project:<slug>.blocker  卡在哪、缺什么")
    lines.append("· project:<slug>.next     下一步打算做什么")
    lines.append("slug 用项目名的英文小写（kotoba、campus-map）。**只写你真有依据的属性**，"
                 "没提到的就不要建那条——空属性比没有属性更糟，它会显得我们知道得比实际多。")
    lines.append("同一件事不要既写 project: 又写 capability:：项目的事实归 project:，"
                 "「他因此会了什么」才归 capability:。两边都写会得出互相矛盾的结论。")
    return "\n".join(lines)


_SYSTEM_BASE = """你是「启研」，面向北大本科一二年级学生的科研导师。

你像一个熟悉又克制的学长：陈述事实、给出理由，不煽情、不卖课、不堆感叹号。
你面前的学生不知道自己适不适合科研、该研究什么、从哪开始。你的价值不是给一堆介绍，
而是让他**真的做出一个行动**，并且让下一步有依据。

前提：**他就是北大的学生**（这个产品只服务北大，默认如此）。不要问「你是哪个学校」——
那是在问一件已经知道的事。他若自己说起别的学校，就如实说知识库只覆盖北大培养方案。

三条铁律：
1. 行动大于信息。但**不要每句话都塞一个任务**——信息足够或用户想动手时才收口到具体下一步。
2. 个性化必须有依据。你说的每句关于他的话，都要来自下面「本轮环境」里的事实。
3. 不编造。不知道就说不知道；工具失败就如实说失败，不要编一个课程或项目顶上。

回答长度跟着问题走：简单确认可以很短；复杂问题允许分段讲透，不要为了短而牺牲清楚。
要解释、可以提假设、可以找反例、可以承认还不知道。不要复述已知信息来凑字数。
不要问下面「本轮环境」里已经知道的事。一次只问一个最值得问的问题。

【怎么问才算自然：按「提问阶梯」走，别跳级】
「本轮环境」里有 `提问阶梯`，告诉你哪些已经有数、最早的空白层是哪一层。
顺序是：身份 → 倾向 → 能力起点 → 当前实践。经历和约束不进阶梯。

跳级就是不自然。真实反馈：用户只说了「大二」，系统却跳过「在哪个院系」直接问「上过哪些课」——
它还没弄清他是谁，就开始问他的能力细节了。

规则：
- **默认他就是北大学生，不要问「你是哪个学校」**。这个产品只服务北大，培养方案知识库也只有
  北大的；「你是哪个学校」是在问一件已经知道的事，用户只会觉得你没认真听。
  他要是自己说「我是清华的」这类，就如实告诉他：知识库只覆盖北大培养方案，其他的只能给通用建议。
- 要问就问**最早的空白层**。上一层的空白会改变下一层问题的问法，所以顺序不能颠倒。
- **一次只问一个问题**，而且问具体、能一句话答的（「在上的哪门课让你觉得有意思」），
  不要问抽象的（「你的兴趣是什么」）。
- 如果本轮对话上下文明显在别的地方（他在追问一个概念、刚说了件急事），
  就顺着上下文走，不必强行回到阶梯——**阶梯管的是「你想了解他时问什么」，不是每轮都必须问**。
- 已经填过的层不要再问。他说过院系就不再问院系，哪怕那层别的东西还空着。
- 能推出来的别问。他要是已经在做数据分析大作业，就别再问「你有没有编程基础」。"""


_PROPOSAL_RULES = """【本轮要你输出什么】
一次输出（JSON），包含四件事：你观察到了什么、要不要改记忆、这一轮说什么、要不要调工具。

- understanding.signals：这段话告诉了你什么。可以为零条（寒暄、假设、引用别人都算零条）。
  注意：signals 只是你的观察，**不等于要写进记忆**——写不写看下一节。

【写不写进记忆：默认不写，要写就举证】
先记住一件事——**写错的代价和漏写的代价不对称**：漏写一条，下次再问一遍就行；
写错一条，它会**静默污染之后的每一个决策**，而且没人会发现。
（真实事故：模型把「Vibecoding」自己解读成「依赖 AI 生成、代码不是他写的」写进了能力事实，
之后所有任务都建立在「他不会写代码」这个它自己编的前提上。）

所以每条 `add`/`replace` 你必须同时给出两样东西，缺一条就被代码丢掉：

1. `evidence_quote`：**用户原话里的连续片段**，而且它要**撑得住**你写的 value。引整句陈述（连主语、否定、时间一起），
   别只截值那几个字；代码按整条消息看这句话是不是在说他自己现在的事。
   代码会算「value 的内容有多少能在引文里找到」：
   - 撑得住 → 收
   - 只撑住一部分 → 收，但**降级为 inferred**（区分「他说的」和「你推的」）
   - 大部分撑不住 → **直接拒**。别把别的轮次的内容、你自己的评价塞进 value。
   value 里不要写你的判断（「基础扎实」「能力偏弱」），只写他说了什么。

   下面这几种字段，value 按固定的形状写，代码拿它去原话里逐项核对（对不上就拒）：
   - `grade`：`{"stage": "本科|硕士|博士|高中", "year": 1-6}`。原话没说第几年 year 填 null；
     只说「二年级」「second-year」没说阶段，stage 填 null（或本科）。被否定的、以前的、打算的（「想读研」）、说错又改口的不写。
   - `age`：`{"value": 19, "unit": "岁"}`；`enroll_year`：`{"value": 2024, "unit": "年"}`（「24级」写 2024）；
     `pace`：`{"value": 10, "unit": "小时", "per": "周"}`（per 填 周/天/月，原话没说就填 null；范围写 `[3, 5]`）。
     数和单位要和原话那一处对得上：「两个半小时」是 2.5 小时，「一小时二十分钟」是 80 分钟，别把周数、门数当小时。
   - `school` / `department` / `major`：写名字（院系用环境包里库内认出的规范名），原话里得出现这个名字或它的常见简称
     （北大、信管、数院）。他说的是「想去」「以前在」「不是」的学校院系，不写。
   - `interest:*` / `goal:*` / `field:*`：value **直接抄原话里那几个字**（最短、能说清是什么的那段，如「机器学习」「读博」），
     常见缩写（AI/人工智能、ML/机器学习、读研/读研究生）算同一个。否定的（「不想碰」「没兴趣」「除了X」）、以前的不写。
   其余字段 value 照旧写一句话，只写原话里有的（数字、英文名词、否定都要和原话一致），别添原话没说的项目、领域或程度。
   说的是室友、家人、老师的事，或者「假如」「以前」「打算」的事，不要记成他现在的事实。

2. `affects`：**这条记忆改变你未来的哪个决策**。只能从这几个里挑：
   task_difficulty（任务难度）｜task_kind（任务类型）｜direction_choice（方向建议）｜
   question_next（下一轮问什么）｜course_pick（课程推荐）｜pace（推进节奏）｜feedback（反馈方式）。
   **挑不出来就说明它没资格进记忆，别写。**

【这些该写，别漏】
「默认不写」不等于「尽量不写」。下面这几类只要出现就写：
- **改口**：他推翻自己说过的（「其实我不是…」「我刚才说错了」）→
  `replace` 或 `retract`，把旧的撤掉。只删不写会留下两条互相矛盾的记忆。
- **身份**：年级、院系、专业 → `grade`（写成 {stage, year}）/ `department` / `major`，affects=task_difficulty。
  他自报的是简称（「信管」「数院」）就按环境包里库内认出的规范名称写（`department`=信息管理系）。
  不要问学校、入学年份、性别、年龄、所在地。
- **当前实践**：在上的课、在做的项目、卡在哪 → `current:<slug>`。
  这是他此刻最真实的处境，affects=task_kind。
- **明确的兴趣或目标**：→ `goal:<slug>` / `interest:<slug>` / `direction:<slug>`，
  affects=direction_choice。
- **能力自述**：数学/编程到什么程度 → `base:*` 或 `capability:<slug>`，affects=task_difficulty。

判断口诀：**这条会不会让他下次得到的建议不一样？** 会 → 写，并挑一个 affects；
不会 → 不写。

【以下都不写（这些是最常犯的）】
- 瞬时状态：「我今天有点累」「让我想想」「先这样」——对未来决策零影响。
- 已经知道的重复：已知他大二，他又说「我是大二」——没有新信息。
- **你的推断**：他没说的别替他补。推断最多写 `inferred`，绝不能当 `declared`。
- 假设和转述：「如果我要读研…」「我同学说…」——不是关于他的事实。
- 寒暄：你好、谢谢、再见。
- 本该是事件的：「他交了作业」属于提交事件，不要手写。

只写**会改变以后怎么判断他**的东西。一轮里一条都不写是完全正常的。
- tool_intent：需要看外部信息时才填。不需要就填 null。
  填了工具就必须把 dialogue.reply 留成 null——你要等工具结果回来再写回复。
- next_action：**默认 null。本轮不派任务。** 只有在他需要去定方向时才填
  `explore_direction`，让他看右边的方向排序或方向树，不要派微任务。

  【个性化闸门 —— 填之前必须过】
  1. 不要给 micro_task / review_progress / course_action。那些不再出。
  2. 你必须能说出它依据了「我记住的」里的哪些事实，并把它们的 id 放进 `based_on`。
     **填不出 based_on 就不许填 next_action**（代码会直接丢掉它）。
  3. 用户还没被了解够的时候，正确答案就是 next_action = null，这一轮只问、只讲。

  【什么时候才真的该给】
  - 方向还没定、他需要去看排序或方向树 → explore_direction。
  - 其它时候 next_action = null。对话不再布置微任务。

  【最多问两轮】
  同一个问题（或换个说法问同一件事）**最多连续两轮**。第三轮必须换策略：
  要么给一个引用现有事实的任务，要么用 offered_actions 给 2–3 个可点的选项让他挑，
  而不是继续追问。用户不回就是不想答，继续问会变成逼问。

  【不给的情况】
  - 你手上**一条关于他的事实都没有**（只知道昵称和年级）→ null，这一轮用来问对问题。
  - 用户给了停顿信号（「先别推进」「我就随便看看」「先不用了」）→ null，
    move 用 acknowledge_stop。这时候任何任务都是硬塞。
  - 纯寒暄、用户在反驳你、你只是顺手确认一句 → null。

  **不要每轮都提同一件事。** 上一轮已经说过的任务，除非他主动问起、或者完成了，
  否则不要再说第二遍。他只是没做，不是没听见。反复催比不给更让人烦。

  【反面教材 —— 这些都被代码拒过，别写】
  用户只说「信管大二，想走大数据」，就回「找一段抄过的代码关掉讲义重写一遍」。
  这句对任何一个信管大二学生都成立，等于没有了解他。这一轮该做的是问：
  你更想往「用数据支持决策」还是「写代码做模型」那一侧走？——然后 next_action = null。

  但反过来，如果他已经说过「Python 只会照着教程抄」，再问第三次「你想往哪一侧走」
  也是错的——那条事实足够支撑一个具体任务了，就该给出来。

  `offered_actions` 是给用户省力的：当他在两个选项之间犹豫、或者连续没回答你的开放问题时，
  给 2–3 个可点的短句（每个 ≤20 字）比再问一遍有效。它和 next_action 不冲突，可以同时给。

  action 取值：只许 explore_direction（去看右边的方向排序或方向树）。不派任务。
  `based_on` 是事实 id 数组，例如 ["f_ab12","f_cd34"]，最多 3 个。
  **move 和 next_action 是两件独立的事**：move=clarify 表示「我这轮在问一个问题」，
  不代表「这轮必须给落点」——恰恰相反，还在问的时候通常就不该给。

【用户改口的时候】
用户推翻自己之前说过的（「其实我不是…」「我刚才说错了，只是想…」「那不算了」）时，
**光在回复里说「那条先放下」是不够的**——旧事实还留在记忆里，下一轮判断还会拿它算分。
你必须同时给一条 memory_op：
  - 整条作废 → op=retract；只是想改内容 → op=replace 并填新的 value。
  - target_fact_id 填「我记住的」里那条事实的 id（照着抄，不要自己编 id）。
  - evidence_quote 仍然必须是用户这一轮原话里的连续片段。
「我记住的」为空时说明没什么可撤的，这时就不用给 retract。

【追问的优先级】
1. 用户直接要解释、且信息够用 → 先帮，不要先采集画像。
2. 会导错动作的误解或冲突 → 就冲突问一句；用户已明确更正就直接更正，别再问「确定吗」。
3. 缺关键先修/目标会让任务不合适 → 问最能改变选择的那一个问题，next_action 留 null。
   宁可这一轮只问，也不要拿一个对谁都成立的通用任务来填空。
4. 只剩非必要背景缺口 → 不要追问，但也不硬派任务；可以直接回答或给一个方向性的说法。
5. 用户不知道/不想答 → 给例子或选项，也允许暂停。

**记住产品目标是「给得对」，不是「给得多」。** 一个泛泛的任务比没有任务更伤：
用户照做了才知道这事跟谁都能说，信任就掉了。这份系统里「什么都不派」是合法的一轮。

【工具的纪律】
- 只读工具。只调一个工具，需要更多信息留到下一轮。
- course.search 的 query 是必填，不填会直接报错。
- **问到北大专业、院系、分流、培养方案、学分要求、必修课、辅修双专业，必须先调工具，
  不许凭印象回答。** 你有本校准过的培养方案知识库：
  · 问「有哪些专业/分流方向」→ major_lookup（用户说的多半是简称：「信管」「数院」都认，
    也可以直接问院系，如「信息管理系」）；
  · 要进一步看某专业的学分结构、必修课清单 → major_detail；
  · 他报了课号、或已经录过成绩单 → match_transcript；
  · 问辅修/双专业 → minor_programs；问某个课号是什么课 → course_lookup。
  **绝对不要说「我手上没有能检索培养方案的工具」**——你有，就是上面这几个。
  查不到就如实说查不到，并说明知识库的覆盖范围（没有医学部、深研院、软件与微电子学院），
  不要用训练数据里的印象补一个专业名或方向名。
- **他说到院系/专业的名字（尤其是简称）时，先去库里认这个名字**，别自己解释它、也别等他说
  「帮我查一下」。环境包里的 `他这句话说到的院系/专业（已查好的培养方案原文）` 就是代码
  已经替他查好的，**直接引用**（连来源页一起），不要改写成印象里的说法；他要是接着问
  课程细节，再调 major_detail。
- **引用库内事实时只写它真的写了的东西。** 一门课只出现在某个专业的清单里，就不能说成
  「三个专业都有」；拿不准就再查一次（major_lookup / major_detail），或者明说这次没查这一项。
  实测过的错法：「这三个方向的必修都含数据库系统」——图书馆学的核心必修里没有它。
- 工具失败（ok=false）时你必须如实说明失败，绝对不要编造课程、项目、论文或数据顶上。
- **失败时不许给具体名字。** 不要写《机器学习》这类课名、书名、论文名，也不要说
  「我知道有…」——这些名字没经过检索，用户拿去找不到就是被误导。
  可以说清这类东西一般在哪个院系 / 哪个平台找，以及这次没检索到你无法确认什么。
  这一条比「把话说完整」重要：宁可这次只给方向，也不要给一个查不到的名字。"""


def _proposal_system() -> str:
    return f"""{_SYSTEM_BASE}

【你能写的记忆】
{_registry_doc()}

【你能用的工具】
{tools.describe()}

{_PROPOSAL_RULES}

【输出格式】只输出一个 JSON 对象：
{{
  "understanding": {{
    "gist": "一句话复述用户这轮在说什么",
    "signals": [{{"kind": "goal|interest|capability|constraint|preference|correction|meta",
                  "value": "这条信息是什么",
                  "evidence_quote": "用户原话里的连续片段"}}],
    "corrections": [{{"targets_fact_id": "f_xxx", "why": "为什么这是纠正"}}]
  }},
  "memory_ops": [
    {{"op": "add|replace|retract|support", "key": "...", "value": "一句话；grade/age/enroll_year/pace 写成上面说的对象",
      "evidence_quote": "必须是用户原话里逐字存在的片段",
      "affects": "task_difficulty|task_kind|direction_choice|question_next|course_pick|pace|feedback",
      "target_fact_id": "replace/retract/support 时必填",
      "valid_until": "constraint:* 必填，ISO 日期"}}
  ],
  "tool_intent": null,
  "dialogue": {{
    "move": "answer|clarify|propose_action|execute_action|correct|acknowledge_stop",
    "reason": "为什么选这个动作（不给用户看）",
    "reply": "回复正文；tool_intent 不为 null 时这里是 null",
    "offered_actions": [{{"label": "可点的下一步"}}]
  }},
  "next_action": null
}}
next_action 非 null 时形如：
  {{"action": "explore_direction", "title": "给用户看的一句话", "direction": "ai",
    "node_id": null, "payload": {{}}, "based_on": ["f_ab12cd34"]}}
tool_intent 非 null 时形如：
  {{"tool": "major_lookup", "args": {{"query": "信管"}}}}
  字段名就叫 tool（不是 name / tool_name），参数放 args 里；工具名只能用上面列出的那些。
  填了 tool_intent 就把 dialogue.reply 留成 null，等工具结果回来再写。"""


def _reply_system() -> str:
    return f"""{_SYSTEM_BASE}

【现在的情况】
你刚才请求了一个工具，结果已经回来了。现在请结合工具结果，写出给用户的回复。
环境包里的 `他这句话说到的院系/专业（已查好的培养方案原文）` 是代码先查好的库内事实，
和工具结果一样可以直接引用（带来源页），别用印象改写。

工具的纪律：结果里 ok=false 表示失败，你必须如实说明失败，绝对不要编造课程、项目或数据顶上。
如果结果为空，就如实说这次没查到，并给一个不依赖该结果的下一步。"""


def _reply_plain_system() -> str:
    return f"""{_SYSTEM_BASE}

【现在做什么】
判断已经做完了，你现在只需要把要对用户说的那段话写出来。
如果环境包里有 `他这句话说到的院系/专业（已查好的培养方案原文）`，那是代码替你查好的库内事实
（带来源页）：说到院系、专业、分流、课程、学分时按它说，别用印象改写，也别再说「我去查一下」。
直接输出正文，不要 JSON、不要用代码块包起来、不要复述这些说明。可以分段。"""


def _env_for_prompt(env: dict[str, Any]) -> str:
    import json
    return json.dumps({
        "时间": env["time"],
        "memory_version": env["memory_version"],
        "用户这轮说": env["current_message"],
        "最近对话": env["recent_context"],
        "当前状态": env["active_plan"],
        "我记住的（可引用其 id）": env["recalled_memory"],
        "提问阶梯（哪层有数了、下一层该问什么）": env.get("ladder"),
        "最近发生的事": env["recent_events"],
        # 代码已经查好的库内事实（只读、带来源页）。直接引用，别用印象改写；
        # 要更细的（完整必修课清单）再调 major_detail。
        "他这句话说到的院系/专业（已查好的培养方案原文）": env.get("lib_facts") or [],
    }, ensure_ascii=False, indent=2)


# ---------- 模型调用 ----------


def _propose(env: dict[str, Any]) -> dict[str, Any] | None:
    """调用①。解析失败重试一次，仍失败返回 None（上层降级）。"""
    user = f"【本轮环境】\n{_env_for_prompt(env)}\n\n请输出这一轮的 JSON。"
    for _ in range(2):
        data = llm.chat_json(_proposal_system(), user, timeout=45, tag="dialogue.proposal",
                             uid=env.get("uid"))
        cleaned = _clean_proposal(data)
        if cleaned is not None:
            return cleaned
    return None


# ---------- 不编造事实的代码侧兜底 ----------
#
# 「不编造事实」是产品三原则之一，不能只靠 prompt 自觉。检索失败后模型仍可能凭记忆
# 列出具体课名/书名（实测发生过：承认「没查成功」，接着给了 7 个课名）。
# 结构化路径（写进记忆的事实）由 memory.py 逐字校验兜住；**正文里的具体名称**没有约束，
# 所以这里补一道检测 + 一次定向重写。正常路径仍是两次调用，只在检测到风险时多一次。

_RISK_NAME_RE = re.compile(r"《[^》\n]{2,40}》")

_REPAIR_SYSTEM = """你负责按规则重写一段回复。

规则：外部检索失败时，回复里**不允许出现任何具体名称**——课名、书名、论文名、项目名都不行，
包括写在《》或「」里的。原因：这些名字没有经过检索，用户拿去找不到就是被误导。

重写要求：保留原回复的意思、结构和语气；删掉所有具体名称；
把「有这么几门课：A、B、C」改成说明这类内容该去哪里查（哪个院系 / 哪个平台），
以及这次没有检索结果时你无法确认什么。不要新增任何名称，不要道歉式空话。
只输出重写后的正文，不要解释你的改动，不要加标题。"""


def _repair_names(env: dict[str, Any], reply: str, tool_results: list[dict[str, Any]]) -> str | None:
    """检索失败但正文出现具体名称时，做一次定向重写。失败就返回 None（保留原文并标记风险）。"""
    user = (
        f"【用户这轮说】{(env.get('message') or '')[:300]}\n\n"
        f"【检索结果】{json.dumps(tool_results, ensure_ascii=False)[:800]}\n\n"
        f"【待重写的回复】\n{reply}"
    )
    try:
        fixed = llm.chat(_REPAIR_SYSTEM, user, temperature=0.3, timeout=45, tag="dialogue.repair",
                         uid=env.get("uid"))
    except Exception:
        return None
    fixed = (fixed or "").strip()
    # 重写后仍带名称，说明这次修复没用，别拿它覆盖原文
    if not fixed or _RISK_NAME_RE.search(fixed):
        return None
    return fixed


def _guard_no_fabrication(env: dict[str, Any], reply: str,
                          tool_results: list[dict[str, Any]]) -> tuple[str, bool]:
    """返回 (最终正文, 是否仍有编造风险)。"""
    if not tool_results or all(r.get("ok") for r in tool_results):
        return reply, False
    if not _RISK_NAME_RE.search(reply):
        return reply, False
    fixed = _repair_names(env, reply, tool_results)
    if fixed:
        return fixed, False
    return reply, True


def _reply_with_tools(env: dict[str, Any], tool_results: list[dict[str, Any]],
                      proposal: dict[str, Any]) -> dict[str, Any] | None:
    """调用②。带工具结果生成回复与下一步。"""
    import json
    user = (
        f"【本轮环境】\n{_env_for_prompt(env)}\n\n"
        f"【你的判断】{json.dumps(proposal.get('dialogue', {}), ensure_ascii=False)}\n\n"
        f"【工具结果】\n{json.dumps(tool_results, ensure_ascii=False, indent=2)}\n\n"
        "只输出 JSON：{\"reply\": \"给用户的回复\", \"next_action\": null 或 "
        "{\"action\": \"explore_direction\", "
        "\"title\": \"给用户看的一句话\", \"direction\": \"方向代码\", \"node_id\": null, \"payload\": {}, "
        "\"based_on\": [\"f_xxx\"]}}。"
        "next_action 默认应该是 null：只有能说清「为什么是给他的」时才填，"
        "并在 based_on 里给出「我记住的」里的事实 id；填不出 based_on 就不要填 next_action。"
    )
    data = llm.chat_json(_reply_system(), user, timeout=45, tag="dialogue.reply", uid=env.get("uid"))
    if not isinstance(data, dict):
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    return {"reply": reply[:4000], "next_action": _clean_next_action(data.get("next_action"))}


# ---------- 规整与校验 ----------


def _clean_next_action(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip()
    title = str(raw.get("title") or "").strip()
    if action not in VALID_NEXT_ACTIONS or not title:
        return None
    direction = str(raw.get("direction") or "").strip()
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    based_on = raw.get("based_on")
    if not isinstance(based_on, list):
        based_on = []
    based_on = [str(x) for x in based_on if isinstance(x, (str, int))][:3]
    return {"action": action, "title": title[:120], "direction": direction[:20],
            "node_id": str(raw.get("node_id") or "")[:64] or None, "payload": payload,
            "based_on": based_on}


def _clean_proposal(data: Any) -> dict[str, Any] | None:
    """把模型输出规整成契约形状。形状完全不对就返回 None（触发重试/降级）。"""
    if not isinstance(data, dict):
        return None
    dialogue = data.get("dialogue")
    if not isinstance(dialogue, dict):
        return None
    move = str(dialogue.get("move") or "").strip()
    if move not in VALID_MOVES:
        move = "answer"

    tool_intent = data.get("tool_intent")
    if isinstance(tool_intent, dict):
        # 工具名这个字段模型会自己换写法（真实翻车现场：写成 "name"，
        # 而契约里是 "tool"）。名字认不出就等于没调工具，整轮被判「无效输出」降级。
        # 这里把常见同义词收敛到契约形状，参数名同理（MCP 客户端叫 arguments）。
        intent_name = ""
        for key in ("tool", "name", "tool_name", "toolName"):
            if tool_intent.get(key):
                intent_name = str(tool_intent[key]).strip()
                break
        args = tool_intent.get("args")
        if not isinstance(args, dict):
            args = tool_intent.get("arguments")
        tool_intent = ({"tool": intent_name, "args": args if isinstance(args, dict) else {}}
                       if intent_name else None)
    else:
        tool_intent = None

    reply = dialogue.get("reply")
    reply = str(reply).strip()[:4000] if reply else None

    if tool_intent is not None:
        # 调工具时必须等结果回来再写回复（契约 §1）
        reply = None
        next_action = None
    else:
        next_action = _clean_next_action(data.get("next_action"))
        if not reply:
            return None  # 不调工具又不说话 = 无效输出

    ops = data.get("memory_ops")
    if not isinstance(ops, list):
        ops = []

    understanding = data.get("understanding")
    if not isinstance(understanding, dict):
        understanding = {}
    # gist 在写完事实和助手消息之后才用：原来写成对象/数字/列表，存完了才抛异常、这一轮没有结果（Codex 复现）
    gist = understanding.get("gist")
    understanding = {**understanding, "gist": gist if isinstance(gist, str) else ""}

    offered = dialogue.get("offered_actions")
    if not isinstance(offered, list):
        offered = []
    offered = [{"label": str(a.get("label"))[:80]} for a in offered
               if isinstance(a, dict) and a.get("label")][:3]

    return {
        "understanding": understanding,
        "memory_ops": ops,
        "tool_intent": tool_intent,
        "dialogue": {"move": move, "reason": str(dialogue.get("reason") or "")[:400],
                     "reply": reply, "offered_actions": offered},
        "next_action": next_action,
    }


def _evidence_pool(uid: str, conversation_id: str) -> list[str]:
    """校验引文用的证据池：本画像近期消息（含本轮刚写入的那条）。"""
    msgs = store.list_messages(uid, limit=40)
    # 只认学生自己的话：原来助手消息也在池里，助手上一轮说的「你擅长 Python」能当学生自述写进画像（Codex 复现）
    # 只回「前者」「好」这种确认时也不借助手的话：试过把助手上一句拼进来当证据，结果「好」能把助手整段话变成学生自述、
    # 用户还能伪造拼接的那一段（Codex 第十三轮复现）。宁可这一条不记、下次再问；界面上点选项发的是选项原文，照样有证据
    return [m["text"] for m in msgs if m.get("text") and m.get("role") == "user"]


# ---------- 降级：规则版一轮 ----------


def _degraded_proposal(uid: str, message: str, env: dict[str, Any]) -> dict[str, Any]:
    """没配模型或模型不可用时的规则版。

    保底行为：把用户这条消息交给现有的 onboarding 规则抽取（选项命中 → 预设事实），
    然后按「还没方向就请他看右边排序」给出下一步。规则版不派任务。这样不配 key 也能跑通全链路。
    """
    ops: list[dict[str, Any]] = []
    plan = env.get("active_plan") or {}
    import onboarding
    if not plan.get("direction") and (not env.get("lib_facts") or onboarding.answers_current_round(uid, message)):
        # 还没方向、模型又不可用：走规则版五问（会推进、会给可点的选项），不要一遍遍问同一句。
        # 他这句话里说到了院系/专业时例外：先用本地知识库答他（下面那条分支）——
        # 但点的是当前这一问的选项（「数学课学过一些」）就还是在答题，照样往下走（Codex 复现：原来卡在这一问）
        step = onboarding.advance(uid, message)
        if step is not None:
            return {
                "understanding": {"gist": message[:120], "signals": [], "corrections": []},
                "memory_ops": [],  # 这一轮的回答已经由规则版记成待核对的条目
                "tool_intent": None,
                "dialogue": {"move": "clarify", "reason": "规则降级：模型不可用，走固定的五问",
                             "reply": step["reply"],
                             "offered_actions": [{"id": f"o{i}", "label": label}
                                                 for i, label in enumerate(step["options"])]},
                "next_action": None,
            }

    # 规则版也要过个性化闸门：把依据挂到当前生效的方向事实上。
    dir_id = next((f["id"] for f in env.get("recalled_memory") or []
                   if str(f.get("key", "")).startswith("direction:")), None)
    lib = env.get("lib_facts") or []
    if not plan.get("direction") and lib:
        # 他这句话里说到了院系/专业，而模型这一轮不可用：把代码查好的库内事实直接给他，
        # 别让他看到一句和这句话无关的「先看看你想往哪个方向走」。
        first = lib[0]
        majors = first.get("命中") or []
        names = "、".join(m.get("专业") or "" for m in majors if m.get("专业"))
        reply = (f"你提到的「{first.get('用户说到的词')}」，培养方案里是"
                 f"{first.get('院系') or '这个院系'}的 {'／'.join(n for n in [names] if n)}"
                 f"（{first.get('库里认成') or ''}）。"
                 f"\n（这一轮模型不可用，名单是本地知识库返回的原文。）")
        move, next_action = "answer", None
    elif not plan.get("direction"):
        # 五问已经问完、还没选方向：指到能选方向的地方，而不是再问一遍
        move, reply = "clarify", ("模型这会儿不可用。去「方向」页挑一个方向（不确定就先去「核对」页看看我记下的几条），"
                                  "选好之后我按规则给你排第一件二十分钟的小事。")
        next_action = None
    elif not plan.get("pending_action") and dir_id:
        move, reply = "clarify", "可以看看右边按你说过的排的方向，或去今日读一篇。"
        next_action = None
    else:
        move, reply = "clarify", "接着上次那件事说：现在卡在哪一步？"
        next_action = None

    return {
        "understanding": {"gist": message[:120], "signals": [], "corrections": []},
        "memory_ops": ops,
        "tool_intent": None,
        "dialogue": {"move": move, "reason": "规则降级：未配置模型", "reply": reply,
                     "offered_actions": []},
        "next_action": next_action,
    }


# ---------- 主入口 ----------


def _transcript_turn(uid: str, cid: str, message: str) -> dict[str, Any]:
    """用户把成绩单直接粘进了对话。

    用户的原话：「为什么我直接在对话里输入成绩单，不能直接智能识别？
    只能在核对页面手动输入才能识别」

    **这一轮不调模型。** 理由：成绩单是固定格式的数据，解析是确定性的
    （`transcript.parse_transcript` 有 78 条测试钉着）。让模型去"理解"一份
    60 行的教务导出，只会更慢、更贵，而且可能把「学分 3」当闲聊。
    解析 + 落库 + 如实回报，这才是「智能识别」该有的样子。

    识别不出来时**明确说为什么**，并指向核对页——那里是手动的兜底路径。
    """
    parsed = transcript.parse_transcript(message)
    courses = parsed["courses"]
    warnings = parsed["warnings"]

    written = 0
    if courses:
        clean = []
        for it in courses:
            course = str(it.get("course") or "").strip()
            if not course:
                continue
            clean.append({
                "course": course[:120],
                "grade": transcript.normalize_grade(it.get("grade")),
                "credits": store.clean_credits(it.get("credits")),
                "term": str(it.get("term") or "")[:40],
                "kind": str(it.get("kind") or "")[:40],
                "status": (str(it.get("status") or "completed")
                           if str(it.get("status") or "") in ("completed", "current", "audit")
                           else "completed"),
            })
        if clean:
            # 按学期替换：贴进来的学期以新为准（重复粘贴不会重复），没贴的学期不动。
            # 原来整份替换：只贴这学期两门课，以前导入的所有学期都被删光。
            written = store.replace_terms(uid, clean)
            store.add_event(uid, "transcript_imported", "",
                            {"count": written, "via": "dialogue"})

    derived_summary = memory.sync_transcript_facts(uid) if written else {}
    # sync_transcript_facts 返回的是 **key 的增删改清单**，不是事实本身：
    #   {"added":[key...], "updated":[...], "removed":[...], "keys":[...]}
    # 要报给用户看的是那些结论的原文，所以按 key 把事实取回来。
    derived_keys = set(derived_summary.get("keys") or [])
    derived = [f for f in memory.active_facts(uid)
               if str(f.key).startswith("transcript:") and str(f.key) in derived_keys]
    s = transcript.summarize(courses) if courses else {}

    # ---- 回复：把「我读出了什么」逐项说清，不替用户下结论 ----
    lines: list[str] = []
    if written:
        lines.append(f"我认出这是成绩单，已经读进去了：**{written} 门课**，"
                     f"共 {s.get('passed_credits', 0)} 学分通过。")
        if s.get("gpa") is not None:
            lines.append(f"按教务口径算，加权平均绩点是 **{s['gpa']:.4f}**"
                         f"（只算有百分制成绩的 {s.get('gpa_credits', 0)} 学分）。")
        # 字母等级/五级制**没有**换算成绩点，这件事必须说出来，
        # 否则用户会以为绩点已经把所有课都算进去了。
        ungraded = s.get("ungraded") or []
        if ungraded:
            names = "、".join(f"{u['course']}（{u['grade']}）" for u in ungraded[:6])
            more = f" 等 {len(ungraded)} 门" if len(ungraded) > 6 else ""
            lines.append(f"有 {len(ungraded)} 门是字母等级/五级制，"
                         f"**没有算进绩点**：{names}{more}。"
                         "教务对这类课的换算口径我不敢替你定，所以宁可不算。")
        cur = s.get("in_progress") or []
        if cur:
            names = "、".join(c["course"] for c in cur[:6])
            more = f" 等 {len(cur)} 门" if len(cur) > 6 else ""
            lines.append(f"另外 {len(cur)} 门还在修（成绩没出）：{names}{more}。")
        if derived:
            lines.append("从这份底稿我推出几条结论，写进「能力」了：")
            for d in derived[:4]:
                lines.append(f"· {d.value}")
        lines.append("你可以在右边的「它记住的我」里看到，也可以去「我的」页核对。")
    else:
        lines.append("这看起来像成绩单，但我一门课都没读出来。")
        lines.append("多半是缺了学期标题行（形如「25-26学年度1学期」）——"
                     "教务导出里每一门课都要挂在某个学期下面，没有它我不敢猜是哪门课。")

    if warnings:
        lines.append("")
        lines.append(f"有 {len(warnings)} 处我没看懂，先列出来，不替你补：")
        for w in warnings[:6]:
            lines.append(f"· {w}")

    reply = "\n".join(lines)
    store.add_message(uid, "assistant", reply, conversation_id=cid)
    store.touch_conversation(uid, cid, topic_hint="导入成绩单")

    return {
        "reply": reply,
        "conversation_id": cid,
        "memory_version": store.get_memory_version(uid),
        "facts_added": [],
        "facts_changed": [],
        "rejected_ops": [],
        "tool_results": [],
        # 成绩单导入**不产生行动卡**：这是一次数据录入，不是一步行动。
        "next_action": None,
        "pending_action": _public_action(store.pending_action(uid)),
        "offered_actions": [],
        "move": "answer",
        "degraded": False,
        "fabrication_risk": False,
        "transcript_import": {
            "written": written,
            "warnings": warnings,
            "summary": s,
            # 转成普通 dict：UserFact 不能直接进 JSON
            "derived": [d.to_dict() for d in derived],
            "changed": {"added": derived_summary.get("added") or [],
                        "updated": derived_summary.get("updated") or [],
                        "removed": derived_summary.get("removed") or []},
        },
        "trace": {"recalled": [], "filtered_expired": [], "memory_version": 0,
                  "transcript_import": written},
    }


def turn_steps(uid: str, message: str, conversation_id: str | None = None,
               stream_reply: bool = False):
    """一轮对话，按阶段产出事件。最后一个事件固定是 ("result", 响应体)。

    事件：（"stage", {"name": ...}）表示真实阶段推进；("delta", {"text": ...}) 是回复增量。
    HTTP 层据此做 SSE；turn() 只取 result，两种入口共用同一份逻辑。
    """
    message = (message or "").strip()
    if not message:
        raise ValueError("message is required")

    conv = (store.get_conversation(uid, conversation_id) if conversation_id else None)
    if conv is None:
        conv = store.open_conversation(uid, topic_hint=message[:60])
    cid = conv["id"]

    store.add_message(uid, "user", message, conversation_id=cid)
    store.add_event(uid, "turn", source_id=cid, payload={"chars": len(message)})

    # 粘进来的是成绩单 → 直接导入，不调模型。
    # 放在最前面（早于 observe）：它不需要环境包，也不需要模型判断。
    if transcript.looks_like_transcript(message):
        yield ("stage", {"name": "memory"})
        yield ("result", _transcript_turn(uid, cid, message))
        return

    yield ("stage", {"name": "observe"})
    env = build_env(uid, cid, message)
    degraded = False

    yield ("stage", {"name": "decide"})
    proposal = _propose(env) if llm.ready(uid) else None
    if proposal is None:
        proposal = _degraded_proposal(uid, message, env)
        degraded = True

    # 校验并提交记忆
    yield ("stage", {"name": "memory"})
    accepted, rejected = memory.validate_ops(uid, proposal.get("memory_ops") or [],
                                             _evidence_pool(uid, cid))
    decision_id = store.add_decision(
        uid, conversation_id=cid, memory_version=store.get_memory_version(uid),
        move=proposal["dialogue"]["move"], reason=proposal["dialogue"]["reason"],
        rationale_refs=[f.id for f in memory.active_facts(uid)][:5],
        tool_calls=[], rejected_ops=rejected, degraded=degraded,
    )
    added, changed = memory.apply_ops(uid, accepted, decision_id)
    if added or changed:
        store.bump_memory_version(uid)

    reply = proposal["dialogue"]["reply"]
    next_action = proposal.get("next_action")
    intent = proposal.get("tool_intent")
    fabrication_risk = False

    if intent:
        # 有工具：调用②必须等结果，且要同时给出下一步，所以走 JSON（非流式）。
        yield ("stage", {"name": "tool", "tool": intent.get("tool")})
        result = tools.run(uid, intent, context={"query_hint": message})
        tool_results = [result]
        env["tool_results"] = tool_results
        env["trace"]["tool"] = result.get("tool")
        env["trace"]["tool_ok"] = bool(result.get("ok"))
        yield ("stage", {"name": "compose"})
        second = _reply_with_tools(env, tool_results, proposal) if llm.ready(uid) else None
        if second:
            reply, next_action = second["reply"], second["next_action"]
        else:
            reply = _tool_fallback_reply(result)
            degraded = True
        # 检索失败却在正文点名具体的课/书/论文 —— 违反「不编造事实」，先修再给
        reply, fabrication_risk = _guard_no_fabrication(env, reply, tool_results)
    else:
        # 没有工具：结构化决策已经做完了，回复正文可以真正逐字流式产出。
        tool_results = []
        yield ("stage", {"name": "compose"})
        if stream_reply and llm.ready(uid):
            acc: list[str] = []
            status: dict[str, Any] = {}
            for piece in _stream_reply(env, proposal, status):
                acc.append(piece)
                yield ("delta", {"text": piece})
            streamed = "".join(acc).strip()
            # 流正常收尾才用流出来的正文；断在半截就沿用调用①写好的完整回复（结果事件里给的是它，前端会整段换掉）。
            # 原来半截也当成功回复存下，盖掉了完整的那段（Codex 复现）。调用①本来就是模型的回复，不算降级
            if status.get("finished") and streamed:
                reply = streamed[:4000]
            # 流式失败就沿用调用①已经写好的 reply，不额外再发一次请求

    if not reply:
        reply = "我在，但这一轮我没能组织好回复。你可以再说一句你想弄清楚的问题。"
        degraded = True

    next_action = _validate_next_action(uid, next_action)

    # 用户明确说停：把还开着的行动收掉，不留悬着的东西（由 move 决定，属执行不属决策）
    if proposal["dialogue"]["move"] == "acknowledge_stop":
        store.abandon_open_actions(uid)

    # next_action 只表示「这一轮决定给的落点」，没有就是 null——
    # 不要用「当前还挂着的行动」回填，否则「这轮不该派任务」这个判断就没法从响应里看出来。
    if next_action:
        store.abandon_open_actions(uid)
        saved = store.save_action(
            uid, conversation_id=cid, action=next_action["action"], title=next_action["title"],
            direction=next_action["direction"], node_id=next_action.get("node_id") or "",
            payload=next_action.get("payload") or {}, status="offered",
            based_on=next_action.get("based_on") or [],
        )
        next_action = _public_action(saved)

    # 当前仍开着的行动（界面状态），与上一项分开
    pending_action = _public_action(store.pending_action(uid))

    store.add_message(uid, "assistant", reply, conversation_id=cid)
    store.touch_conversation(uid, cid, topic_hint=(proposal["understanding"].get("gist") or "")[:200])
    _sync_onboard_state(uid, env)

    yield ("result", {
        "reply": reply,
        "conversation_id": cid,
        "memory_version": store.get_memory_version(uid),
        "facts_added": added,
        "facts_changed": changed,
        "rejected_ops": rejected,
        "tool_results": tool_results,
        "next_action": next_action,
        "pending_action": pending_action,
        "offered_actions": proposal["dialogue"]["offered_actions"],
        "move": proposal["dialogue"]["move"],
        "degraded": degraded,
        "fabrication_risk": fabrication_risk,
        "trace": env["trace"],
        "onboard_done": onboard_done(uid),
    })


def turn(uid: str, message: str, conversation_id: str | None = None) -> dict[str, Any]:
    """处理一轮对话。返回契约 §10 的响应形状。"""
    result: dict[str, Any] | None = None
    for kind, payload in turn_steps(uid, message, conversation_id):
        if kind == "result":
            result = payload
    assert result is not None
    return result


def _stream_reply(env: dict[str, Any], proposal: dict[str, Any], status: dict[str, Any] | None = None):
    """调用②（流式）：只产出给用户看的正文。流正常收尾时 status["finished"] 为 True。"""
    import json
    user = (
        f"【本轮环境】\n{_env_for_prompt(env)}\n\n"
        f"【你的判断】{json.dumps(proposal.get('dialogue', {}), ensure_ascii=False)}\n\n"
        "把要对用户说的那段话写出来。"
    )
    yield from llm.chat_stream(_reply_plain_system(), user, tag="dialogue.reply.stream", status=status,
                               uid=env.get("uid"))


def _tool_fallback_reply(result: dict[str, Any]) -> str:
    """调用②失败时的模板化摘要：如实说结果，不编造。

    认得出几种结果形状：`items`（课程/项目）、`命中`（知识库的专业卡）、
    `院系排名`（成绩单认院系）。认不出形状就只报条数——**不要**把 dict 截断当标题，
    那样会给出一串看不出是什么的东西。
    """
    name = result.get("tool") or "工具"
    if not result.get("ok"):
        return f"我去查了一下（{name}），这次没查到：{result.get('error') or '没有结果'}。不编一个给你，我们换个方式继续。"
    items = result.get("items") or result.get("命中") or []
    if items:
        first = items[0]
        label = (first.get("name") or first.get("title") or first.get("专业")
                 or first.get("院系") or str(first)[:60])
        more = f"，另外还有 {len(items) - 1} 个候选" if len(items) > 1 else ""
        return f"查到了（{name}）：{label}{more}。你看看这个方向对不对，我们再往下走。"
    ranks = result.get("院系排名") or []
    if ranks:
        top = ranks[0]
        return (f"查到了（{name}）：按你给的课号，最像的是「{top.get('院系')}」"
                f"（命中 {top.get('命中数')} 门）。这个供参考，我们再往下走。")
    return f"查到了（{name}），但没有合适的结果。先不硬凑，我们换个角度看看。"


def _validate_next_action(uid: str, next_action: dict[str, Any] | None) -> dict[str, Any] | None:
    """方向必须属于当前画像生效的方向；任务必须说得出「为什么是给他的」。

    最后一条是「个性化必须有依据」的代码化：任务要引用至少一条本画像的真实事实
    （`based_on` 里的 fact id 必须存在且生效）。引不到依据的，多半是换个用户也照样成立的
    套话（「关掉教程重写一段代码」），一律不给——宁可这一轮只问只讲，也不要发一个
    不痛不痒的任务。产品的失败模式不是「给得太少」，是「给得不相干」。
    """
    if not next_action:
        return None
    facts = memory.active_facts(uid)
    facts_by_id = {f.id: f for f in facts}

    # —— 个性化闸门 ——
    valid_ids = [i for i in (next_action.get("based_on") or []) if i in facts_by_id]
    if not valid_ids:
        return None
    next_action["based_on"] = valid_ids

    directions = {f.key.split(":", 1)[1] for f in facts if f.key.startswith("direction:")}
    direction = next_action.get("direction") or ""
    if directions:
        if direction not in directions:
            next_action["direction"] = sorted(directions)[0]
    return next_action


def onboard_done(uid: str) -> bool:
    """开场聊完：phase 到 done，或已有方向，或身份层与倾向层都有数。"""
    state = store.get_onboard_state(uid)
    if state.get("phase") == "done":
        return True
    facts = memory.active_facts(uid)
    if any(f.key.startswith("direction:") for f in facts):
        return True
    ladder = memory.ladder_state(uid)
    known = {x["layer"]: x["known"] for x in ladder.get("layers") or []}
    return bool(known.get("identity") and known.get("interest"))


def _sync_onboard_state(uid: str, env: dict[str, Any]) -> None:
    """让旧向导（/api/onboard/*）与新内核看到同一份进度，避免两套状态打架。"""
    state = store.get_onboard_state(uid)
    if state.get("phase") == "done":
        return
    if env["active_plan"].get("direction"):
        store.set_onboard_state(uid, {**state, "phase": "done", "round_no": 999})


# ---------- 行动事件（契约 §6）----------


_ACTION_EVENTS = {
    "accept": "accepted",
    "decline": "abandoned",
    "complete": "completed",
}


def action_event(uid: str, action_id: str, event: str) -> dict[str, Any] | None:
    """用户对某个行动表态。代码落状态，不交给模型。

    **accept 必须产生一个真任务**，这是「对话出任务 → 任务区完成 → 回对话」
    这条链路的唯一接点。原来这里只改状态 + 写事件，`tasks` 表始终是空的，
    于是用户点进任务区什么都没看到，也没法完成——他问过两次这个问题。
    """
    target = _ACTION_EVENTS.get((event or "").strip())
    if not target:
        raise ValueError(f"unknown event: {event}")
    a = store.get_action(uid, action_id)
    if not a:
        return None
    # 先建任务、再记「已接受」：原来反过来，建任务出错时行动已经是「已接受」、却没有任务，
    # 刷新后只剩「去任务区完成」，任务区什么都没有（Codex 复现）。建任务是幂等的：同一张卡重复 accept 不会造出第二个。
    # explore_direction 只导航，不建任务；老的 micro_task 卡仍建。
    task = None
    if target == "accepted" and a.get("action") != "explore_direction":
        task = workbench.task_from_action(uid, a)
    store.update_action(uid, action_id, target)
    store.add_event(uid, f"action_{target}", source_id=action_id, payload={"action": a["action"]})

    if task is not None:
        store.add_event(uid, "task_created", source_id=action_id,
                        payload={"task_id": task.id, "origin": task.origin})
    elif target == "completed":
        # 只记事件是不够的：完成这件事必须有**交付内容**。
        # 真提交走 /api/tasks/{tid}/submit（那里会写 experience 事实 + 出反馈），
        # 这条事件只用于兼容旧路径，界面上已经不给「标记完成」了。
        store.add_event(uid, "task_submitted", source_id=action_id, payload={"title": a["title"]})

    out = _public_action(store.get_action(uid, action_id))
    if task is not None:
        out["task_id"] = task.id
    return out


def _public_action(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """把 actions 表的一行转成对外的行动形状。

    必须只有这一个出口：曾经 turn 路径返回规整形状（action_id），而 history 和
    action_event 直接返回数据库原始行（id），前端在刷新后拿不到 action_id，
    点按钮就 422，错误提示还被渲染成 [object Object]。

    `based_on` 同时附上事实原文，前端要拿它显示「为什么是给你的」。

    `task_id` 一并给出：刷新页面后仍要能点「去完成」。没有它，用户刷新一次
    就找不到那个任务了——而行动卡本身不携带交付物和提交框。
    """
    if not row:
        return None
    ids = row.get("based_on") or []
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except ValueError:
            ids = []
    aid = row.get("id") or row.get("action_id") or ""
    task_id = ""
    if aid:
        for t in store.list_tasks(row.get("user_id") or ""):
            if t.action_id == aid:
                task_id = t.id
                break
    return {
        "action_id": aid,
        "action": row.get("action") or "",
        "title": row.get("title") or "",
        "direction": row.get("direction") or "",
        "status": row.get("status") or "",
        "task_id": task_id,
        "based_on": [{"id": i, "value": _fact_text(i)} for i in ids],
    }


def _fact_text(fact_id: str) -> str:
    """任务依据的事实原文；查不到就返回空串（前端会跳过）。"""
    if not fact_id:
        return ""
    f = store.get_fact(fact_id)
    return (f.value or "") if f else ""


def history(uid: str, conversation_id: str | None = None) -> dict[str, Any]:
    msgs = store.list_messages(uid, conversation_id=conversation_id)
    pending = store.pending_action(uid)
    last = store.last_action(uid)
    # just_finished：刚在任务区交掉的那一步。用户在任务区做完回到对话，
    # 需要看到「你刚做完 X」而不是一片空白——那正是「回到对话」这一步的意义。
    just = None
    if last and last.get("status") == "completed" and (not pending or pending["id"] != last["id"]):
        evs = store.list_events(uid, kinds=["task_submitted"], limit=50)
        ev = next((e for e in reversed(evs) if e.get("source_id") == last["id"]), None)
        just = {
            "action_id": last["id"],
            "title": last.get("title") or "",
            "payload": (ev or {}).get("payload") or {},
        }
    return {
        "messages": [{"role": m["role"], "text": m["text"], "at": m["created_at"]}
                     for m in msgs if m["role"] in ("user", "assistant")],
        "conversations": store.list_conversations(uid),
        "pending_action": _public_action(pending),
        "last_action": _public_action(last),
        "just_finished": just,
        "onboard_done": onboard_done(uid),
    }
