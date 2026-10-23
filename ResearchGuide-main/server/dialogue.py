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
import guide
import memory
import onboarding
import store
import tools
import transcript
from schemas import now_iso

RECENT_TURNS = 12          # 进环境包的最近消息条数
# 召回**不再有一个总的条数上限**。原来这里是 limit=10，而 recall 是按层拉平的，
# 于是靠后的层（倾向、临时约束）会被这个总数截掉——等于把「平表抢坑位」换了个地方犯。
# 现在预算完全由 memory.LAYER_BUDGET 逐层决定（各层名额之和就是上限）。
# 留一个很大的安全上限只为防止异常数据把上下文撑爆，正常路径碰不到它。
RECALL_HARD_CAP = 40

VALID_MOVES = ("answer", "clarify", "correct", "acknowledge_stop")


def _transcript_history(uid: str) -> list[dict]:
    """从已保存的准确回执提取简短历史，不新增预览课程记录或重复注入原文。"""
    result = []
    for m in store.list_messages(uid):
        if m["role"] != "assistant":
            continue
        text = m["text"]
        preview = re.match(r"识别出 (\d+) 门课。本次只预览，没有保存课程", text)
        saved = re.match(r"已(?:识别并保存|保存) (\d+) 门课程记录", text)
        if preview:
            result.append({"mode": "preview", "count": int(preview[1]), "saved": False})
        elif saved:
            result.append({"mode": "append" if "原有课程保留" in text[:100] else "replace",
                           "count": int(saved[1]), "saved": True})
    return result[-5:]


def _guide_closing(profile: dict) -> str:
    areas = {a["id"]: a for a in profile["areas"]}
    topic = areas["topic"]["summary"]
    detail = areas["reading"]["summary"] or areas["foundation"]["summary"]
    support = areas["support"]["summary"]
    parts = [f"我先按这些理解你：{topic}。"]
    if detail:
        parts.append(f"阅读背景是：{detail}。")
    if support:
        parts.append(f"希望获得的帮助是：{support}。")
    return "".join(parts) + "\n\n这些信息已适合开始，可以进入研读模块。没聊到的背景以后再补；哪里不准确，也可以直接纠正我，或留在这里继续聊。"


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
        },
        "guidance": {
            "entry_ready": onboard_done(uid),
            "profile": guide.assess(uid),
            "transcript_history": _transcript_history(uid),
            "transcript_available": bool(store.list_enrollments(uid)),
            # 成绩单选项现在固定摆在输入框下面的「可选：粘贴成绩单」折叠里（第四轮 HCI：开场里那段 3/3 被吓到），
            # 开场一出现就算介绍过；不再靠扫消息里的「树洞」「成绩单」字样，长聊或另开会话也不会再主动推销。
            "transcript_introduced": any(m["role"] == "assistant" for m in store.list_messages(uid)),
            "preferences": [{"key": f.key, "value": f.value} for f in facts
                            if f.key.startswith("style:") or f.key == "pace"],
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
            for e in store.list_events(uid, kinds=["transcript_imported"], limit=5)
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
                 "**直接引用，不要重写**。用户可直接在本对话粘贴树洞成绩单，也可去「我的」页核对。")
    lines.append("另外要注意 capability:<slug> 是**用户自述或你推断**的能力印象"
                 "（「我会一点 Python」），和 transcript:* 的硬证据是两回事，别混着写。")
    lines.append("标了「别问」的字段（性别、年龄）：用户主动说了就存，**但不要追问**——"
                 "它们几乎不改变你的建议，追问只会让人觉得像在填表。")
    lines.append("「当前实践」= 他主动谈到的手头事情（在上的课、在做的项目、卡在哪），"
                 "有依据就写，别塞进别的层。它不是引导必答项，不为填满画像专门追问。")
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


_SYSTEM_BASE = """你是「启研」的信息了解与答疑助手，产品定位是科研文献伴读。
你的职责是理解用户背景、维护有依据的画像、回答疑问。具体论文带读、出题和判答由研读模块的独立 Agent 负责。
对话不是软件主要入口；不要接管阅读流程，不布置任务、作业、练习或打卡，不要求交付物或承诺检查成果。
用户主动要学习方法时可以解释、给建议，但不要把建议变成待完成的任务。

像一个认真听人说话的同学：先回应眼前这句话，再决定有没有必要补问。
不要每轮复述画像、评价用户或总结成清单；简单回应可以只有一两句，解释复杂概念再展开。
默认用户是北大学生，不问学校。用户说明其他学校时，如实解释培养方案知识库只覆盖北大。
个人判断必须来自本轮环境中的事实；专业培养方案仅说明学习背景，不能据此认定他已经掌握其中全部课程。
也不能凭院系和年级认定他学过某一门课。例如只说「数院大二」，不能说「你手上已有分析、代数、概率」。
专业知识库用于认标准名称和了解课程背景；对他本人只引用自述、成绩单或真实行为证据。
不要惯常用「这个底子够用了」「起点很顺」「正好」来评价他，更不要把「能开始阅读」说成「已具备相关能力」。
不知道就说明不知道，工具失败就如实说明，不编课程、专业、项目或论文。
解释概念时区分严格定义、常见用法与争议，不把相似情形一律等同，也不把经验规律说成必要条件或保证。

【首次交流与自然提问】
- 用户直接问问题时先回答，不以补画像为回答的前提。可以没有追问，不必每轮以问号结尾。
- 了解背景时只补问当前话题确实需要、会影响回答的一件事，一次最多一个问题。
  年级或专业已知就不重问；用户正在谈阅读主题，就顺着主题聊，不为了填表硬拐到课程。
- 提问阶梯只是已知信息的参考，不是问卷顺序，不要求逐层问完。
  禁止用「这学期印象最深/最有意思的课是什么」作为默认破冰问题。
  用户说想看大模型评测，可以接着问「你更想先看懂评测指标，还是论文中的实验比较？」；
  用户只是问一个概念，直接解释即可，不必套这句示例。
- 用户说不知道、跳过、暂时不答或不分享，就接住并换策略：解释、举例或允许他先用软件。
  不换个说法重复同一问题，不把拒绝理解成基础不足，不用任务替代追问。
- 用户没有回答你上轮的问题、而是提供了别的信息，也算有效回应。接住新信息，不说「上一轮问的还没答」、
  「请先回答」「你还没告诉我」这类追缴回答的话；不把你的提问变成他欠下的清单。
- 环境里的 guidance.entry_ready 为 true 时，现有信息已足以开始使用。
  不再为「完成引导」收集能力、课程、项目或所有固定字段。只有用户的问题需要时才补问。
  页面会给一次「进入启研」邀请，你不必每轮重复催进入，也不要说「回答完才能进入」。
  即使尚未达到推荐条件，用户也能用页面现有按钮跳过交流；不要声称必须交成绩单、定方向或通过测试。

【首次认识与后续交流】
guidance.profile.first_visit=true 才是首次认识；false 时直接按普通对话答疑，不重做开场访谈。
开场已经邀请用户提供院系、专业、年级、成绩单，以及可选的本学期在修课程。
先利用这些客观背景。还没分专业时有院系、年级即可，不要求选定标准专业；院系简称用知识库对齐，不追问已知信息。
知识库帮助你理解标准名称和学科背景；用户没问专业分流或培养方案时，不主动罗列专业、学分或培养要求。
只有成绩单时，下一步先了解尚未知的院系、专业、年级，例如「你现在在哪个院系、几年级？如果已经分专业也可以一起说」。
不要立刻跳到「想读哪方面论文」「研究目标是什么」「读过论文吗」；主题不明确不影响继续认识。
基本背景缺失时一次可以合并问院系、专业、年级；已有的部分不重问，也不每轮重新列一遍开场清单。
成绩单可选，已导入就使用现有课程；不再索要或逐门重问。不能从成绩单猜年级、院系、专业，更不能认定熟练掌握。
本学期在修课是可选补充，用 current:course 或合适的 current:<slug> 保存自述，附原话依据。
身份和成绩单已有、而本学期课程未提及且未拒绝时，可以轻问一次「本学期还有哪些正在上的课程，愿意的话也可以补充」。
在修自述和成绩单分别保存；不能因为一句自述就声称已把成绩单里的成绩或课程状态修改了。
只预览时不把课程当真实背景，不索要正式导入；拒绝分享或不清楚的内容就跳过，不能反复追问。

背景有了以后保持对话：先回应这句话里的内容、疑问或补充，再决定是否继续问一个相关、容易回答的问题。
用户可以主动改变话题；不要为了补齐 profile.missing 逐项采访，也不要每轮「收到」之后马上下一题。
兴趣、目标、实践、阅读经验和困难自然出现时可以跟进；没有提到就不必挨个追问，也不默认人人有科研或项目经历。
例如用户主动说想研究 Agent，可以问是否实际使用过这类产品、怎么用；这个例子不能用作所有用户的固定问题。
用户不清楚主题时，可以顺着正在学的内容或刚才的补充继续聊；不要马上把「先随便看看，之后再来」作为默认出口。
用户明确要求先结束、不再补背景或先进入时尊重他的选择；原有进入按钮和低信息确认保留，不设置额外门槛。

已有信息是否适合开始，继续使用 guidance.profile.ready 的现有判断，不增加新问卷、计时或固定轮数。
它是推荐提示，不是使用权限。不能声称必须先定研究方向、提供成绩单或讲清阅读困难才能进入。
ready=true 时回顾关键理解并允许纠正，剩余信息以后再补；不再继续采访，不反复催进入。
reading_experience / reading_barrier / reading_support 只在用户实际提到时记录，value 用有依据的原话，
category 分别为 background / preference / preference；不为这些字段主动编问题来凑覆盖度。
明确兴趣也尽量保留原话，例如「对 Agent 的可靠性有点好奇」，不要改写成推断性的兴趣评价。
具体论文带读仍在研读模块。对话不布置任务、练习或交付物，也不因用户没说某项而判断能力弱。

【成绩单是可选信息来源】
对话支持直接识别用户粘贴的树洞成绩单，不要说只能到「我的」里导入。
开场已经介绍时（guidance.transcript_introduced=true），不要再次主动索要或重复操作说明。
若还未介绍且正好需要了解学习基础，可顺带说一次：在树洞成绩单页全选（Ctrl+A）、复制（Ctrl+C），
粘贴到这里，能帮助我更全面了解学习基础；不提供也能继续。用户主动询问时再解释操作即可。
已导入就用现有记录；拒绝后不催问、不要求说明原因。用户说以后再说，就把选择权留给他。

【本轮刚粘贴成绩单时】
环境里的 transcript_import 非空，说明代码已完成解析和保存/预览，系统会先显示准确的简短回执。
你只写回执后接着说的话，通常 1–3 句话。直接回应上下文或提出一个轻问题，不再用「收到、已保存、已存进画像」二次确认。
除非用户明确问某项统计，不重报门数、绩点、均分、学分或逐项列能力，不重复回执。
用户问统计时用 transcript_import.summary 或查询工具的真实结果；预览的 summary 只属于本次未保存内容，不能说成已保存记录。
先接用户粘贴前的上下文：如果他同时提问，先答；如果已说过年级、院系或兴趣，不要再采集。
首次只有成绩单、基本身份未知，且用户没有拒绝补背景时，先问清尚缺的院系、专业、年级。
例如「你现在在哪个院系、几年级？如果已经分专业也可以一起说。」不以研究主题或阅读目标作为导入后的默认问题。
不能只说「之后想聊什么随时说」「有问题来问」就结束，否则仍然是导入后的断话。
已有身份就顺着上下文继续交流；必要时可以问本学期正在上的课程（可跳过），不把未知兴趣自动转成追问研究主题。
guidance.entry_ready=true 就不再补背景；可简短表示已经可以开始研读，或顺着尚未回答的问题继续。
mode=preview 时不索要身份、兴趣或要求正式导入；只回应用户的问题，把是否继续留给用户。
预览仅不保存课程和派生能力，聊天原文仍照常保留；禁止承诺「不留痕」「没有任何记录」「什么都没存」。
guidance.transcript_history 是跨近期消息窗口的处理历史；其中 preview 表示曾经识别但没有保存，不能把“没有导入”说成“从未预览”。
不要猜年级、院系、专业、研究方向，也不要把课程成绩说成掌握能力。只在用户自己的引导语中有明确事实时写 memory_ops。

【记忆与回执】
不把假设、他人经历、示例或你自己的评价写成用户事实。
用户明确的交流偏好（如「不想分享成绩单」「别问课程」「先解释，不做练习」）应记住：
用 style:<主题>，value 直接使用能独立理解的用户原话短句，affects=question_next 或 pace。
不要往 value 里添加原因或「偏好隐私保护」等概括，以免超出引文、写入失败。
后续更正同一偏好时 replace，别让互相矛盾的偏好并存。
只有本轮 memory_write 显示真正写入，才可以说「已保存/已更新/已撤回」。
写入被拒只能说这次没保存成功；source=derived 的成绩单结论不能靠 memory_ops 撤回，
应说明要在「我的」修改或删除底稿，不能谎称已删。"""

_PROPOSAL_RULES = """【本轮要你输出什么】
一次输出（JSON），包含四件事：你观察到了什么、要不要改记忆、这一轮说什么、要不要调工具。

- understanding.signals：这段话告诉了你什么。可以为零条（寒暄、假设、引用别人都算零条）。
  注意：signals 只是你的观察，**不等于要写进记忆**——写不写看下一节。

【写不写进记忆：默认不写，要写就举证】
本阶段尚未提交记忆，dialogue.reply 不要宣称「已保存/已更新/已撤回」；
可以回应理解到的内容。真正的提交结果由系统回执或后续生成时的 memory_write 确认。
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
- next_action：始终为 null。对话不生成行动卡、不派任务，也不以定方向作为对话收口。
  offered_actions 仅是可选的用户回复短句，默认空；用户确实不知如何表达时才给 2–3 个中性选项，
  包含「先跳过」或「暂时不确定」，不要用它塞练习、催交材料或重复追问。

【用户改口的时候】
用户推翻自己之前说过的（「其实我不是…」「我刚才说错了，只是想…」「那不算了」）时，
**光在回复里说「那条先放下」是不够的**——旧事实还留在记忆里，下一轮判断还会拿它算分。
你必须同时给一条 memory_op：
  - 整条作废 → op=retract；只是想改内容 → op=replace 并填新的 value。
  - target_fact_id 填「我记住的」里那条事实的 id（照着抄，不要自己编 id）。
  - evidence_quote 仍然必须是用户这一轮原话里的连续片段。
「我记住的」为空时说明没什么可撤的，这时就不用给 retract。

【追问的优先级】
1. 用户直接要解释 → 先帮；背景不足时说明回答的适用范围。
2. 已明确更正的内容 → 直接更正；只有真正影响理解的歧义才问一句。
3. 用户正在了解自己的阅读起点 → 只补问最能帮助当前回答的一项。
4. 剩余非必要字段、对方不想答或已足以开始 → 停止采集，继续答疑即可。

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
  这些事实先用于规范身份名称、理解专业背景；用户只是自报专业时不必罗列培养方案、学分或必修课，
  也不要用「你们有这些课」代替倾听他的阅读兴趣。需要讲课程时再引用相关原文和来源。
  只报简称时通常一句「信管，也就是信息管理系」就足够；先保存标准名称并顺着话题聊，不展开分流名单。
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
    "move": "answer|clarify|correct|acknowledge_stop",
    "reason": "为什么选这个动作（不给用户看）",
    "reply": "回复正文；tool_intent 不为 null 时这里是 null",
    "offered_actions": [{{"label": "可点的下一步"}}]
  }},
  "next_action": null
}}
next_action 必须为 null。
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
        "引导状态与交流偏好": env.get("guidance"),
        "本轮实际记忆写入 memory_write": env.get("memory_write"),
        "transcript_import（代码已完成处理，不需要模型解析）": env.get("transcript_import"),
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
        '只输出 JSON：{"reply": "给用户的回复", "next_action": null}。'
        "不要生成任务、行动卡或考核步骤；先回答用户这轮的问题。"
    )
    data = llm.chat_json(_reply_system(), user, timeout=45, tag="dialogue.reply", uid=env.get("uid"))
    if not isinstance(data, dict):
        return None
    reply = str(data.get("reply") or "").strip()
    if not reply:
        return None
    return {"reply": reply[:4000], "next_action": _clean_next_action(data.get("next_action"))}


# ---------- 规整与校验 ----------


def _clean_next_action(raw: Any) -> None:
    """兼容旧响应形状，但任何模型输出都不能再生成对话行动。"""
    return None


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
    if env.get("transcript_import"):
        mode = env["transcript_import"]["mode"]
        known = {x["layer"]: x["known"] for x in env["ladder"].get("layers", [])}
        if mode == "preview":
            reply = "这次先保持预览。如果有想了解的地方，可以直接问我。"
        elif env["guidance"]["entry_ready"]:
            reply = "现有信息已经够开始使用，其他背景以后需要时再补。也可以继续在这里问问题。"
        elif not known.get("identity"):
            reply = "这些记录能帮我了解你接触过的课程，但不代表已经熟练掌握。你现在读什么专业？"
        else:
            reply = "这些课程会作为后续解释的背景。你最近想了解什么主题？暂时没想好也没关系。"
        return {"understanding": {"gist": "处理成绩单后继续交流"}, "memory_ops": [],
                "tool_intent": None, "dialogue": {"move": "answer", "reason": "导入后接话降级",
                "reply": reply, "offered_actions": []}, "next_action": None}

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
        move, reply = "clarify", "这一轮模型暂时不可用，无法可靠理解或保存新的背景信息。你可以先进入启研使用研读模块，稍后再来聊。"
        next_action = None
    elif not plan.get("pending_action") and dir_id:
        move, reply = "clarify", "这一轮模型暂时不可用。已有背景仍保留，你可以先使用研读模块，稍后再来问。"
        next_action = None
    else:
        move, reply = "clarify", "这一轮模型暂时不可用，无法可靠回答这个问题。已有信息仍保留，可以稍后重试。"
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


def _transcript_context(message: str) -> str:
    """保留粘贴前的用户原话，课程数据只用已提交的结构化结果。"""
    lead = re.split(r"\d{2}-\d{2}学年度[123]学期", message, maxsplit=1)[0]
    lead = re.sub(r"成绩查询|刷新|隐藏文字|显示文字", "", lead).strip()[:500]
    return (lead + "\n" if lead else "") + "（用户粘贴了成绩单，代码处理结果见 transcript_import。）"


def _transcript_turn(uid: str, cid: str, message: str) -> dict[str, Any]:
    """确定性解析与提交，不让模型决定课程/分数；返回回执，由 turn_steps 接回自然对话。"""
    header = re.search(r"\d{2}-\d{2}学年度[123]学期", message)
    # 操作说明不是成绩单的一行；不把「只检查/追加」误报成未识别的课程。
    parsed = transcript.parse_transcript(message[header.start():] if header else message)
    courses = parsed["courses"]
    warnings = parsed["warnings"]
    # 尊重粘贴前的明确意图：只预览不落库，补充不覆盖已有记录。
    lead = re.split(r"\d{2}-\d{2}学年度[123]学期", message, maxsplit=1)[0]
    preview = bool(re.search(r"不要(?:导入|保存|写入)|不(?:保存|写入)|先别(?:导入|保存|写入)|只(?:预览|检查|看看)", lead))
    append = bool(re.search(r"追加|补充|增补|不要替换|别替换", lead))

    written = 0
    if courses and not preview:
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
            # 追加模式（上游 w99 成绩单接话）按原样追加；替换模式按学期替换：贴进来的学期以新为准（重复粘贴不会重复），
            # 没贴的学期不动。原来整份替换：只贴这学期两门课，以前导入的所有学期都被删光（Codex 复现）。
            written = (store.add_enrollments(uid, clean) if append
                       else store.replace_terms(uid, clean))
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

    # 数字回执由代码保证准确，明细交给可展开回执，不再向气泡塞整份统计。
    if courses and preview:
        reply = f"识别出 {len(courses)} 门课。本次只预览，没有保存课程，也没有更新能力记录。"
    elif courses and append and not written:
        reply = "这些课程已经在记录里，本次没有新增或替换。"
    elif written:
        reply = (f"已保存 {written} 门课程记录。原有课程保留。" if append
                 else f"已识别并保存 {written} 门课程记录，替换当前画像的原有课程。")
        reply += "课程和成绩的明细可以在下方展开查看。"
    else:
        reply = "这看起来像成绩单，但没有识别到课程。可以检查学期标题是否完整，或到「我的」页核对。"
    if warnings:
        reply += f"另有 {len(warnings)} 处没看懂，没有替你补写；详情也在下方。"

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
        "pending_action": None,
        "offered_actions": [],
        "move": "answer",
        "onboard_done": onboard_done(uid),
        "degraded": False,
        "fabrication_risk": False,
        "transcript_import": {
            "written": written,
            "mode": "preview" if preview else ("append" if append else "replace"),
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

    # 数据仍由代码解析；完成后继续走同一对话内核，避免回执之后突然断话。
    imported = None
    if transcript.looks_like_transcript(message):
        yield ("stage", {"name": "memory"})
        imported = _transcript_turn(uid, cid, message)
        if stream_reply:
            yield ("delta", {"text": imported["reply"] + "\n\n"})

    yield ("stage", {"name": "observe"})
    env = build_env(uid, cid, message)
    if imported:
        imp = imported["transcript_import"]
        env["transcript_import"] = {
            "mode": imp["mode"], "written": imp["written"],
            "parsed_courses": imp["summary"].get("total", 0),
            "stored_courses": len(store.list_enrollments(uid)),
            "summary": imp["summary"],
            "warnings": imp["warnings"],
        }
        # 这次模型负责接话，不重读整份成绩单；明确的用户引导语仍可抽取身份/兴趣。
        env["current_message"] = _transcript_context(message)
        env["recent_context"] = [
            {**m, "text": _transcript_context(m["text"])}
            if m["role"] == "user" and transcript.looks_like_transcript(m["text"]) else m
            for m in env["recent_context"]
        ]
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

    # 第二次生成（含真实 SSE）看到实际提交结果，不能把被拒的记忆说成已保存。
    env["memory_write"] = {"added": added, "changed": changed, "rejected": rejected}
    previous_ready = env["guidance"]["entry_ready"]
    env["guidance"]["profile"] = guide.assess(uid)
    env["guidance"]["entry_ready"] = env["guidance"]["profile"]["ready"]
    env["guidance"]["entry_became_ready"] = not previous_ready and env["guidance"]["entry_ready"]
    reply = proposal["dialogue"]["reply"]
    next_action = proposal.get("next_action")
    intent = proposal.get("tool_intent")
    fabrication_risk = False

    # 达到推荐条件的一次性产品回顾由已提交事实生成，防止模型继续发起背景访谈。
    # 同轮有明确问题时仍先正常答疑，页面邀请负责提示，不覆盖用户的问题。
    guide_closing = (env["guidance"].get("entry_became_ready")
                     and env["guidance"]["profile"]["first_visit"]
                     and not re.search(r"[?？]|是什么|怎么|为什么|如何|能否|是不是|有什么区别|多少", message))
    if guide_closing:
        tool_results = []
        reply = _guide_closing(env["guidance"]["profile"])
        proposal["dialogue"]["offered_actions"] = []
        yield ("stage", {"name": "compose"})
        if stream_reply:
            yield ("delta", {"text": reply})
    elif intent:
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
    if imported:
        reply = imported["reply"] + "\n\n" + reply

    # 代码侧关掉派任务入口，不能仅依赖提示词或模型自觉。
    next_action = None
    pending_action = None

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
        "guide": guide.assess(uid),
        "guide_completed": bool(guide_closing),
        **({"transcript_import": imported["transcript_import"]} if imported else {}),
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


def onboard_done(uid: str) -> bool:
    """推荐开始的覆盖度；兼容字段名，不是阻止进入的权限判断。"""
    return guide.assess(uid)["ready"]


def _sync_onboard_state(uid: str, env: dict[str, Any]) -> None:
    """让旧向导（/api/onboard/*）与新内核看到同一份进度，避免两套状态打架。

    规则版五问（模型不可用时 _degraded_proposal 走 onboarding.advance）把当前轮次记在 phase 里：
    已经往后走的轮次和「问完」要原样保留，不然每一轮都被抹成 gathering，没模型的同学会被一遍遍问同一句
    （Codex 第四轮复现过，上游 PR #19 并入后又出现）。第一轮和空状态统一记成 gathering（advance 把它当第一轮）。"""
    import onboarding
    state = store.get_onboard_state(uid)
    phase = str(state.get("phase") or "")
    if onboard_done(uid):
        phase = "done"
    elif not (phase == "done" or onboarding._ROUND_INDEX.get(phase, 0) > 0):
        phase = "gathering"
    store.set_onboard_state(uid, {**state, "phase": phase})


# ---------- 行动事件（契约 §6）----------


def action_event(uid: str, action_id: str, event: str) -> dict[str, Any] | None:
    """旧客户端只可关闭历史卡片；不可再从对话创建或完成任务。"""
    if event not in ("accept", "complete", "decline"):
        raise ValueError(f"unknown event: {event}")
    a = store.get_action(uid, action_id)
    if not a:
        return None
    if event != "decline":
        raise ValueError("对话不再布置任务；历史任务请在原任务模块查看")
    store.update_action(uid, action_id, "abandoned")
    store.add_event(uid, "action_abandoned", source_id=action_id,
                    payload={"action": a["action"]})
    return _public_action(store.get_action(uid, action_id))


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
    return {
        "messages": [{"role": m["role"], "text": m["text"], "at": m["created_at"]}
                     for m in msgs if m["role"] in ("user", "assistant")],
        "conversations": store.list_conversations(uid),
        "pending_action": None,
        "last_action": None,
        "just_finished": None,
        "welcome_message": (onboarding.WELCOME_MESSAGE if guide.assess(uid)["first_visit"]
                            else "想继续聊什么？可以直接问问题，或补充最近的阅读情况。"),
        "onboard_done": onboard_done(uid),
        "guide": guide.assess(uid),
    }
