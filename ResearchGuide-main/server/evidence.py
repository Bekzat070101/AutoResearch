# -*- coding: utf-8 -*-
"""记忆写入的引文核对：模型按字段给结构化的值，代码只拿固定的表和规则去学生原话里对。

分工（docs/DIALOGUE_CONTRACT.md §7）：
- 模型负责抽：年级给（阶段, 第几年），数量给 {value, unit, per}，学校/院系/专业给名字，兴趣/目标抄原话；
- 代码负责对：原话里得有一处说法按这里的表解析出同一个值，而且这一处不在否定、过去、假设、改口的范围里。
表里没有的说法就算没有着落：漏记一条下次再问，记错一条会悄悄带偏之后每个决策。
不再给每种新说法补一条正则（十七轮审查一直在补）；新说法进表，范围规则只有一套。
所有扫描都是一遍过的、量词有上限：学生可能贴进来几万字。
"""
from __future__ import annotations

import bisect
import re
import unicodedata
from functools import lru_cache
from typing import Any, NamedTuple

# ---------- 预处理 ----------

_VULGAR = re.compile(r"(?:(\d{1,6})[ \t]?)?([¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞])")
# 分数两头都得是完整的整数：「3.7/4.0」里的「7/4」不是分数
_SLASH = re.compile(r"(?<![\d.])(\d{1,3})[ \t]?[/⁄∕][ \t]?(\d{1,3})(?![\d.])")
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def dec(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".")


_PCT = re.compile(r"百分之([零〇一二两三四五六七八九十百]{1,6}|\d{1,3}(?:\.\d{1,2})?)")
_FRAC_CN = re.compile(r"([一二两三四五六七八九十]{1,3}|\d{1,3})分之([一二两三四五六七八九十]{1,3}|\d{1,3})")


def _frac_cn(m: re.Match) -> str:
    a, b = (_tok_value(x) for x in (m[1], m[2]))
    return dec(b[0] / a[0]) if a and b and a[0] else m[0]


@lru_cache(maxsize=256)
def prep(text: str) -> str:
    """「1½」先整体算成 1.5（NFKC 会把它拼成「11⁄2」），再全角转半角、分数算成小数（「三分之一」也算，Codex 复现）、
    「百分之八十」写成「八十%」、英文转小写。之后所有位置都按这份文本算；对 prep 过的文本再 prep 不变。"""
    t = _VULGAR.sub(lambda m: dec(int(m[1] or 0) + unicodedata.numeric(m[2])), str(text or ""))
    t = unicodedata.normalize("NFKC", t)
    t = _SLASH.sub(lambda m: dec(int(m[1]) / int(m[2])) if int(m[2]) else m[0], t)
    t = _PCT.sub(lambda m: m[1] + "%", t)
    t = _FRAC_CN.sub(_frac_cn, t)
    return t.translate(_ASCII_LOWER)


# ---------- 分句和范围 ----------

# 否定、过去、假设只管到这一分句为止；转折词也算断开（「不确定，但对 NLP 感兴趣」）。冒号不断：「我的专业是：经济学」是一句
_BOUNDARY = re.compile(r"[,，。;；!！?？\n\r]|\.(?!\d)|(?<![a-z])(?:but|however|though|although|whereas|instead)(?![a-z])"
                       r"|但是|但|可是|不过|然而|而是|只是|却")
# 「现在」只截断过去（「以前是大一现在大二」）；原来它对否定也算断开，「我并不是现在大三」的否定被洗掉（Codex 复现）
_NOW = re.compile(r"现在|目前|如今|(?<![a-z])now(?![a-z])")
# 下一分句以并列词开头，否定接着管：「我不喜欢数据库、网络，或者操作系统」（Codex 复现）
_CONT = re.compile(r"[ \t]*(?:或者|或是|还有|以及|也不|和|与|及|跟|或|(?<![a-z])(?:or|and|nor)(?![a-z]))")

NEG, PAST, IRREALIS, COND, CORRECTED, OTHER, QUESTION, MENTION, ASIDE = "neg", "past", "irrealis", "cond", "corrected", "other", "question", "mention", "aside"
# 说的是不是他「现在的状态」：年级、学校院系、年龄和每周时间六样都不沾；
# 兴趣和目标本来就是「想」，打算可以，但不能是否定、过去、假设（「假如我喜欢…」）、改口、别人的事
# 只是提到（「是讲座的题目」「这个词」）不算自述；顺带提到（「看开发大模型应用的教程」「班长给我发了通知」）也不是他现在的状态，
# 但兴趣可以从看教程来，所以兴趣目标不查顺带提到
STATE = frozenset({NEG, PAST, IRREALIS, COND, CORRECTED, OTHER, QUESTION, MENTION, ASIDE})
WISH = frozenset({NEG, PAST, COND, CORRECTED, OTHER, QUESTION, MENTION})
KINDS = tuple(sorted(STATE))

_EN_NEG = r"(?<![a-z])(?:not|no|never|neither|nor|without|except|cannot)(?![a-z])|n['’]t(?![a-z])"
_NEG_FWD = re.compile(r"不再是|并不是|也不是|并非|不是|不算|算不上|谈不上|称不上|从来没有|从来没|从没有|从没|从未|未曾|毫无|很难|难以|没有|没|不|除了|除去|而非|"
                      + _EN_NEG)
# 长得像否定、其实不是：程度和比较（不错、不到五小时）、关联词（不仅、不过）、客套、A不A 问句在下面单独判
_NEG_PSEUDO = re.compile(r"不错|不少|不同|不断|不仅|不只|不止|不但|不过|不管|不论|不久|不然|不如|不得不|不禁|不免|不到|不超过|不足|不低于|不少于|"
                         r"不多于|不高于|不对|不好意思|不客气|没错|没关系|没事|没准|毫无疑问|除了.{0,8}?(?:还|也)|"
                         r"not only|not just|no matter|no doubt")
# 双重否定：「不是不喜欢」「没有不想学的」——两个都不算
_NEG_DOUBLE = re.compile(r"(?:不是|并非|并不是|没有|不能|不会|不可能|不可)[ \t]?(?:不|没)")
# 外层「不是」管着的范围里又有一个否定，而且不是「也不是」这种并列：「我不是对人工智能没有兴趣」
_NEG_COPULA = ("不是", "并不是", "并非", "不再是")
_COORD = re.compile(r"也|又|还|、|和|或|及")
# 后置的否定：「对机器学习没有兴趣」「Python 我没学过😂」——在分句末尾（后面只剩语气词、表情）时往前管，管到这一分句开头或最近的「对」
_NEG_BACK = re.compile(r"(?:没有?|毫无|不太?|不是很|不怎么|不算|谈不上|算不上|并不|一点都不|一点也不|都不|也不)(?:什么|太大|多大|啥|太多|很大|这个|那个)?"
                       r"(?:有?兴趣|感兴趣|喜欢|想学|想做|想碰|想搞|考虑|打算|会|懂|熟|熟悉|了解|学过|碰过|接触过|用过|写过|做过|好|行|擅长|在行|熟练|扎实|精通|"
                       r"在上|上过|修过|选上|在修|在做)"
                       r"|(?:兴趣|好感)(?:都|也)?(?:没有?|不大)|无感|没感觉|不感冒|算了|一窍不通|(?:拿|挤|抽|腾)不出(?:来)?|做不到|达不到|不够|没(?:决定|想好|确定)(?:要不要[^，。,.!?！？\s]{0,2})?")
# 这几种不用在分句末尾：「金融学并不是我的专业，我只是选过一门课」（Codex 复现）
_NEG_BACK_ANY = re.compile(r"(?:并不是|不是|并非)(?:我的)?(?:专业|学校|方向|兴趣|目标|菜|院系|学院|领域)")
_TAIL = re.compile(r"(?:[了啊吧呢的呀哈过吗嘛啦哦耶~～…]|[^\w])*")
_PAST = re.compile(r"以前|之前|原来|原本|本来|去年|前年|曾经|当时|那时|那会儿?|上学期|上个学期|上周|上个星期|上个月|小时候|"
                   r"(?<![a-z])(?:used to|last year|previously|formerly|back then|was|were)(?![a-z])")
_PAST_PSEUDO = re.compile(r"本来就|原来如此|原来是这样")
# 说一样东西是别人的意思：「毕业读博是别人给的建议」（Codex 复现）
_PEOPLE = r"(?:室友|舍友|同学|朋友|学姐|学长|学弟|学妹|师兄|师姐|师弟|师妹|姐姐|哥哥|弟弟|妹妹|别人|老师|爸妈|父母|家里)"
_OTHER_BACK = re.compile(r"是?(?:别人|家里|父母|爸妈|老师|导师|学长|学姐|室友|同学)(?:给|提|说|定|要求)?的(?:建议|想法|意见|要求|安排|期望|主意)"
                         r"|的?是(?:我的?)?" + _PEOPLE + r"|属于(?:我的?)?" + _PEOPLE + r"|替" + _PEOPLE + r"问的?"
                         r"|" + _PEOPLE + r"(?:替我|帮我|给我)(?:填|定|选|报|安排|规划)的?"
                         # 修饰别人的：「大四的学长」「23岁的师兄」「元培的同学」「18岁人群」（Codex 复现）
                         r"|的(?:学长|学姐|学弟|学妹|师兄|师姐|师弟|师妹|同学|朋友|室友|老师|侄子|小侄子|孩子)|的?(?:人群|群体)")
# 说一样东西「是过去的」：往前管到分句开头（「人工智能是我去年的兴趣」「大二已经是过去的事了」，Codex 复现）
_PAST_BACK = re.compile(r"(?:去年|以前|之前|过去|曾经|小时候|高中)的(?:兴趣|事|爱好|专业|学校|方向|想法|目标|梦想)|过去的事|过去式|已经结课|结课了|全忘了|都忘了|"
                        r"已经交了|提前交了")
_IRREALIS = re.compile(r"(?<![思理感联回猜幻梦构妄])想(?!法)|打算|准备|计划|希望|将来|以后|未来|毕业后|等我|等到|明年|下学期|下个学期|争取|考虑|申请|报考|"
                       r"考研|保研|(?<![a-z])(?:want|wants|plan|plans|hope|hopes|going to|will|would|apply|applying)(?![a-z])")
_COND = re.compile(r"如果|假如|要是|万一|假设|若是|倘若|假使|(?<![a-z])(?:if|suppose|supposing)(?![a-z])")
# 只是理想、得等以后：「每周五小时只是理想状态」「这个额度得等期中考完才有」（Codex 复现）
_COND_BACK = re.compile(r"(?:只是|仅仅是)(?:理想|想象|设想|假设|计划|愿望)(?:状态|情况)?|(?:得|要)等[^，。,]{0,10}才(?:有|行|能)")
# 只是提到：「是今天讲座的题目」「是群公告里的要求」「这个词」「看…的教程」「班长给我发了通知」（Codex 复现）
_MENTION_BACK = re.compile(r"(?:只)?是[^，。,；;]{0,10}的?(?:题目|标题|栏目|要求|名字|通知|公告)|这个词")
_ASIDE_BACK = re.compile(r"的(?:教程|视频|入门指南|指南|海报|课件|简介)|(?:给我|让我|叫我|帮我|找我|发给我)")
_MENTION_FWD = re.compile(r"(?:标题|题目)(?:是|写着)|论坛里见到|目录里有")
# 条件句的后件也是假设：「如果拿到奖学金，我才会考虑出国留学」（Codex 复现）
_CONSEQ = re.compile(r"[ \t]*(?:那么|那就|那我|那|我才|我就|我再|我也|就|才|则)")
# 「想先说明一下」「想问」是在说话，不是打算（Codex 复现）
_IRREALIS_PSEUDO = re.compile(r"想(?:先)?(?:说明|说|问|确认|告诉|补充|提一下|了解一下|请教)")
# 他自己又出来当主语，「别人的事」就到这儿：「室友大二而我大三」（Codex 复现）。「希望我」「让我」里的「我」是宾语，不算
_SELF = re.compile(r"(?<![望让叫要帮给说得议荐诉劝替跟和比像带对])(?:而|但|可|不过)?(?:我|本人)(?=自己|本人|则|就|才|也|呢|其实|确实|真的|的确|的话|这边|现在|今年|是|在|读|念|想|对|喜欢|"
                   r"大[一二三四五六]|研[一二三]|博[一二三四五六]|已经|属于|学|修|做|准备|打算|决定|还|刚|每周|[0-9一二三四五六七八九十]+岁)")
_SENT_END = re.compile(r"[。！？!?\n；;]|\.(?!\d)")
# 问句里自我介绍的那一段照样是自述：「我这个大四还能…吗」「心院这边的我能…吗」「作为大二学生…？」（Codex 复现）
_SELF_NP = re.compile(r"(?:我这个|我一个|作为|身为|像我这样的?|我们这种)[^，。,？?]{0,12}?(?=的人|的我|还|能|可以|适合|该|要|想|，|,|$)|[^，。,？?]{1,10}这边的我|[^，。,？?]{1,10}的我")
# 说的是别人：「我室友大二」「我妈妈希望我读博」「同组的学长会写 Python」（Codex 复现）。
# 「跟室友一起」「在张老师组里」「跟着导师」是他自己的事，不算
_OTHER = re.compile(r"室友|舍友|同学|同桌|朋友|闺蜜|学长|学姐|学弟|学妹|师兄|师姐|师弟|师妹|老师|导师|教授|助教|辅导员|爸爸|妈妈|我爸|我妈|父母|家长|"
                    r"哥哥|姐姐|弟弟|妹妹|我哥|我姐|我弟|我妹|那位|这位|那个人|同宿舍的?|(?<![其吉])他们?|她们?|别人|大家|有人|"
                    r"(?<![a-z])(?:my (?:friend|roommate|classmate|sister|brother|mom|dad|parents|advisor)|he|she|they)(?![a-z])")
_OTHER_NOT = re.compile(r"组|课题组|实验室|的组|那边|那里|门下|一起")
# 改口：「大二，哦打错了我是大三」「我大二。等下。说错了，我大三」。「打错」这类在哪都算；「不对」「错了」只在分句开头算（「作业错了三题」不是改口）。
# 改口词前面只有语气词、「刚才」「年龄」这类，就往回越过「等下」「抱歉」这种插话，管到上一句实话（Codex 复现）
_CORR_ANY = re.compile(r"打错|说错|写错|输错|口误|笔误|更正|纠正一下|那句(?:话)?(?:是|说的是)|(?<![a-z])(?:i meant|typo|correction)(?![a-z])")
# 往后管的改口：「蛋糕上误写了20岁」（Codex 复现）
_CORR_FWD = re.compile(r"误写了?|误报了?|错写成|写错成|错报成|报错成")
_CORR_HEAD = re.compile(r"(?:不对|错了|说反了|sorry)(?=$|[我是应其啊吧呢哦嗯 \t])")
_CORR_PREFIX = re.compile(r"(?:[哦噢啊呃嗯额哎诶唉 \t]|我|刚才|刚刚|刚|之前|前面|上面|抱歉|不好意思|sorry|年龄|年级|学校|专业|院系|时间|数字|名字|那个|这个)*")
_FILLER = re.compile(r"(?:[哦噢啊呃嗯额哎诶唉 \t!！]|等下|等一下|等会儿?|等等|停一下|稍等|抱歉|不好意思|sorry|wait|hmm)*")


class Scope(NamedTuple):
    start: int
    end: int
    kind: str
    cue_start: int
    cue_end: int


class Scan(NamedTuple):
    text: str
    clauses: list[tuple[int, int]]
    scopes: list[Scope]
    by_cue: list[int]          # scopes 按 cue_start 排好的下标，找「提示词在这一处里面」用
    cue_starts: list[int]      # 和 by_cue 一一对应的 cue_start，二分用
    cover: dict[str, list[int]]  # 每种范围覆盖到每个位置的个数（差分数组求和）
    starts: list[int]          # 每个分句的起点，二分找分句用

    def clause(self, pos: int) -> tuple[int, int]:
        return self.clauses[_clause_at(self.clauses, self.starts, pos)]


def _clauses(t: str) -> list[tuple[int, int]]:
    out, prev = [], 0
    for m in _BOUNDARY.finditer(t):
        out.append((prev, m.start()))
        prev = m.end()
    out.append((prev, len(t)))
    return out


def _clause_at(clauses: list[tuple[int, int]], starts: list[int], pos: int) -> int:
    return max(0, bisect.bisect_right(starts, pos) - 1)


def _neg_cues(t: str) -> tuple[list[tuple[int, int]], set[int]]:
    """真的否定词（去掉看着像的、A不A 问句、双重否定），和双重否定里被抵掉的那个否定词的起点。"""
    cues, cancelled = [], set()
    for m in _NEG_FWD.finditer(t):
        s, e = m.span()
        if s in cancelled or _NEG_PSEUDO.match(t, s) or (t[s] in "不没" and 0 < s and t[s - 1:s] == t[s + 1:s + 2]) \
                or (t[s] == "不" and t[s - 1:s] in ("差", "了", "要")):
            continue
        d = _NEG_DOUBLE.match(t, s)
        if d:
            cancelled.add(d.end() - 1)
            continue
        cues.append((s, e))
    return cues, cancelled


@lru_cache(maxsize=32)  # 同一轮几条 op 共用一句原话；每条要存几个和原话一样长的数组，别多存
def scan(text: str) -> Scan:
    """把原话分句，标出否定、过去、打算、假设、改口、别人的事各自管到哪里。text 必须已经 prep 过。每样都一遍扫完。"""
    t = text
    clauses = _clauses(t)
    starts = [a for a, _ in clauses]
    n = len(clauses)
    scopes: list[Scope] = []

    def clause_idx(pos: int) -> int:
        return _clause_at(clauses, starts, pos)

    # 「的」后面跟着数就不截（「挤不出完整的五小时」），否则否定管不到「的」后面被修饰的词（「不需要太多数学的机器学习」）
    des = [i for i, c in enumerate(t) if c == "的" and not re.match(r"[0-9零〇一二两三四五六七八九十半]", t[i + 1:i + 2])]
    nows = [m.start() for m in _NOW.finditer(t)]
    cont_to = list(range(n))  # 从第 k 句起，并列的分句一直接到第几句
    for k in range(n - 2, -1, -1):
        if _CONT.match(t, clauses[k + 1][0]) and clauses[k + 1][0] < clauses[k + 1][1]:
            cont_to[k] = cont_to[k + 1]

    def fwd_end(e: int, kind: str) -> int:
        k = clause_idx(e)
        end = clauses[k][1]
        if kind == NEG:
            i = bisect.bisect_left(des, e)
            if i < len(des) and des[i] < end:
                return des[i]
            return clauses[cont_to[k]][1]
        if kind == PAST:
            i = bisect.bisect_left(nows, e)
            if i < len(nows) and nows[i] < end:
                end = nows[i]
        return end

    def topic(k: int, cs: int, ce: int) -> None:
        # 「读博？我完全没这个打算」「清华大学？不是我的学校啊」：前一句是个问出来的话题，后一句的否定管到它（Codex 复现）
        if k > 0 and clauses[k - 1][1] - clauses[k - 1][0] <= 15 and re.search(r"[?？]", t[clauses[k - 1][1]:clauses[k][0]]):
            scopes.append(Scope(clauses[k - 1][0], clauses[k - 1][1], NEG, cs, ce))

    neg, cancelled = _neg_cues(t)
    neg_starts = [s for s, _ in neg]
    dead = set()
    for i, (s, e) in enumerate(neg):  # 外层「不是」里面又有否定、中间不是并列 → 两个都不算
        if t[s:e] not in _NEG_COPULA:
            continue
        end = fwd_end(e, NEG)
        j = bisect.bisect_left(neg_starts, e)
        if j < len(neg) and neg[j][0] < end and not _COORD.search(t, e, neg[j][0]):
            dead.update((i, j))
    # 抵掉的否定词也不能被后置否定再认一遍：「我对机器学习不是不感兴趣」里的「不感兴趣」（Codex 复现）
    dead_at = {p for i in dead for p in range(*neg[i])} | cancelled
    for i, (s, e) in enumerate(neg):
        if i not in dead:
            scopes.append(Scope(e, fwd_end(e, NEG), NEG, s, e))
            topic(clause_idx(s), s, e)
    for rx, final in ((_NEG_BACK, True), (_NEG_BACK_ANY, False)):
        for m in rx.finditer(t):
            s, e = m.span()
            k = clause_idx(s)
            a, b = clauses[k]
            if s in dead_at or (final and not _TAIL.fullmatch(t, e, b)):
                continue
            dui = t.rfind("对", a, s)
            scopes.append(Scope(dui if dui != -1 else a, s, NEG, s, e))
            topic(k, s, e)
            if not final and k > 0 and not t[a:s].strip():  # 「…是别人给的建议，不是我的目标」：没主语的这句说的是上一句
                scopes.append(Scope(clauses[k - 1][0], clauses[k - 1][1], NEG, s, e))
    for m in _PAST.finditer(t):
        if not _PAST_PSEUDO.match(t, m.start()):
            scopes.append(Scope(m.end(), fwd_end(m.end(), PAST), PAST, m.start(), m.end()))
    for m in _OTHER_BACK.finditer(t):
        scopes.append(Scope(clauses[clause_idx(m.start())][0], m.start(), OTHER, m.start(), m.end()))
    for m in _PAST_BACK.finditer(t):
        scopes.append(Scope(clauses[clause_idx(m.start())][0], m.start(), PAST, m.start(), m.end()))
    for m in _IRREALIS.finditer(t):
        if not _IRREALIS_PSEUDO.match(t, m.start()):
            scopes.append(Scope(m.end(), fwd_end(m.end(), IRREALIS), IRREALIS, m.start(), m.end()))
    for m in _COND.finditer(t):
        k = clause_idx(m.end())
        end = clauses[k + 1][1] if k + 1 < n and _CONSEQ.match(t, clauses[k + 1][0]) else clauses[k][1]
        scopes.append(Scope(m.end(), end, COND, m.start(), m.end()))
    # 别人的事管到句末或他自己又当主语的地方，中间隔着逗号也接着管（「我室友很闲，每周能抽四小时」，Codex 复现）；
    # 「我和同学都喜欢」「我跟隔壁的朋友同属物院」是一起，不算（Codex 复现）
    sent_ends = [m.start() for m in _SENT_END.finditer(t)]
    selves = [m.start() for m in _SELF.finditer(t)]
    for m in _OTHER.finditer(t):
        s, e = m.span()
        a = clauses[clause_idx(s)][0]
        if t[s - 1:s] in ("和", "跟", "与", "同", "在", "给") or t[s - 2:s] == "跟着" or _OTHER_NOT.match(t, e) \
                or re.search(r"我[ \t]?(?:和|跟|与|同)[^，。！？,!?]{0,8}$", t[max(a, s - 12):s]):
            continue
        i, j = bisect.bisect_left(sent_ends, e), bisect.bisect_left(selves, e)
        scopes.append(Scope(e, min(sent_ends[i] if i < len(sent_ends) else len(t), selves[j] if j < len(selves) else len(t)), OTHER, s, e))
    for m in _COND_BACK.finditer(t):
        scopes.append(Scope(clauses[clause_idx(m.start())][0], m.start(), COND, m.start(), m.end()))
    for rx, kind in ((_MENTION_BACK, MENTION), (_ASIDE_BACK, ASIDE)):
        for m in rx.finditer(t):
            scopes.append(Scope(clauses[clause_idx(m.start())][0], m.start(), kind, m.start(), m.end()))
    for m in _MENTION_FWD.finditer(t):
        scopes.append(Scope(m.end(), clauses[clause_idx(m.end())][1], MENTION, m.start(), m.end()))
    for m in _CORR_FWD.finditer(t):
        scopes.append(Scope(m.end(), clauses[clause_idx(m.end())][1], CORRECTED, m.start(), m.end()))
    # 问句里的说法不是自述：「大二可以去听研究生的讨论课吗？」「复旦大学能申请旁听吗？」（Codex 复现）
    for k, (a, b) in enumerate(clauses):
        tail = t[b:clauses[k + 1][0]] if k + 1 < n else ""
        if re.search(r"[?？]", tail) or re.search(r"(?:吗|呢|什么|怎么样?)[ \t]*$", t[max(a, b - 4):b]):
            p = a
            for m in _SELF_NP.finditer(t, a, b):
                if m.start() > p:
                    scopes.append(Scope(p, m.start(), QUESTION, b, b))
                p = max(p, m.end())
            if p < b:
                scopes.append(Scope(p, b, QUESTION, b, b))

    filler = [bool(_FILLER.fullmatch(t, a, b)) for a, b in clauses]
    prefix_end = [_CORR_PREFIX.match(t, a, b).end() for a, b in clauses]  # 每句只算一次：原来每个改口词都从句首重扫一遍，平方级（Codex 复现）

    def back_to(k: int) -> int:
        j = k - 1
        while j >= 0 and filler[j]:
            j -= 1
        return clauses[j][0] if j >= 0 else clauses[k][0]

    for k, (a, b) in enumerate(clauses):
        h = _CORR_HEAD.match(t, prefix_end[k], b)
        if h and k > 0:
            scopes.append(Scope(back_to(k), a, CORRECTED, h.start(), h.end()))
    for m in _CORR_ANY.finditer(t):
        k = clause_idx(m.start())
        a = back_to(k) if m.start() <= prefix_end[k] else clauses[k][0]
        scopes.append(Scope(a, m.start(), CORRECTED, m.start(), m.end()))

    cover: dict[str, list[int]] = {}
    for kind in KINDS:
        diff = [0] * (len(t) + 1)
        for sc in scopes:
            if sc.kind == kind and sc.end > sc.start:
                diff[sc.start] += 1
                diff[sc.end] -= 1
        run, acc = 0, []
        for x in diff:
            run += x
            acc.append(run)
        cover[kind] = acc
    by_cue = sorted(range(len(scopes)), key=lambda i: scopes[i].cue_start)
    return Scan(t, clauses, scopes, by_cue, [scopes[i].cue_start for i in by_cue], cover, starts)


def kinds_at(sc: Scan, start: int, end: int, kinds: frozenset[str]) -> set[str]:
    """这一处（start..end）落在哪几种范围里。提示词本身在这一处里面的不算（「不确定性量化」里的「不」）。"""
    end = min(max(end, start + 1), len(sc.text))
    if end <= start:
        return set()
    lo, hi = bisect.bisect_left(sc.cue_starts, start), bisect.bisect_left(sc.cue_starts, end)
    # 提示词在这一处里面的范围按种类记成差分，逐位减掉：原来每个位置都把这些范围数一遍，整句引文里提示词多时是平方级（Codex 复现）
    inner: dict[str, list[int]] = {}
    for c in (sc.scopes[i] for i in sc.by_cue[lo:hi]):
        if c.cue_end <= end and c.kind in kinds and c.end > start and c.start < end:
            d = inner.setdefault(c.kind, [0] * (end - start + 1))
            d[max(c.start, start) - start] += 1
            d[min(c.end, end) - start] -= 1
    out = set()
    for k in kinds:
        cov, d, run = sc.cover[k], inner.get(k), 0
        for p in range(start, end):
            if d is not None:
                run += d[p - start]
            if cov[p] > run:
                out.add(k)
                break
    return out


def affirmed(sc: Scan, start: int, end: int, kinds: frozenset[str]) -> bool:
    return not kinds_at(sc, start, end, kinds)


_POST_PAST = re.compile(r"的?(?:时候|时|那会儿?|那年|期间|毕业)")


def is_current(sc: Scan, start: int, end: int, kinds: frozenset[str] = STATE) -> bool:
    """说的是现在：不在否定/过去/假设/改口里，后面也没跟「的时候」「毕业」。"""
    if PAST in kinds and _POST_PAST.match(sc.text, end):
        return False
    return affirmed(sc, start, end, kinds)


def has_negation(text: str) -> bool:
    t = prep(text)
    return bool(_neg_cues(t)[0] or _NEG_BACK.search(t))


# ---------- 年级：固定的表 ----------

STAGES = ("本科", "硕士", "博士", "高中")
_MAX_YEAR = {"本科": 6, "硕士": 3, "博士": 6, "高中": 3, None: 6}
_STAGE_OF = {"本科": "本科", "本科生": "本科", "本": "本科", "大学": "本科", "大学生": "本科", "大": "本科",
             "undergrad": "本科", "undergraduate": "本科", "bachelor": "本科", "college": "本科", "university": "本科",
             "硕士": "硕士", "硕士生": "硕士", "硕士研究生": "硕士", "硕": "硕士", "研究生": "硕士", "研": "硕士", "读研": "硕士",
             "master": "硕士", "masters": "硕士", "master's": "硕士", "graduate": "硕士", "grad": "硕士",
             "博士": "博士", "博士生": "博士", "博士研究生": "博士", "博": "博士", "读博": "博士", "在读博": "博士",
             "phd": "博士", "ph.d": "博士", "ph.d.": "博士", "doctoral": "博士", "doctorate": "博士",
             "高中": "高中", "高中生": "高中", "高": "高中"}
_YEAR_OF = {c: i + 1 for i, c in enumerate("一二三四五六")} | {str(i): i for i in range(1, 7)} | \
           {w: i + 1 for i, w in enumerate(("first", "second", "third", "fourth", "fifth", "sixth"))} | \
           {w: i + 1 for i, w in enumerate(("1st", "2nd", "3rd", "4th", "5th", "6th"))}
_Y = "[一二三四五六1-6]"
_NOT_COUNT = r"(?![0-9]|\.[0-9]|[个门项次篇倍月天周小分万千百十点些统])"  # 「大2个」「大一点」「大2.5」不是年级；句号照常（「我是大二.」，Codex 复现）
_EN_STAGE = r"undergrad(?:uate)?|ph\.?d\.?|doctoral|master'?s|masters|graduate|grad|college|university"
# （正则, 阶段：固定值或取自第几组, 年份所在组）
_GRADE_FORMS: tuple[tuple[re.Pattern, Any, int | None], ...] = (
    (re.compile(r"(?<![最很较更太超扩放增加伟强远广宏盛重巨庞北])大[ \t]?(" + _Y + ")" + _NOT_COUNT), "本科", 1),
    (re.compile(r"(?<![钻科考保攻])研[ \t]?(" + _Y + ")(?!年)" + _NOT_COUNT), "硕士", 1),  # 「读研二」是年级（Codex 复现），「读研一年」不是
    (re.compile(r"硕[ \t]?(" + _Y + ")" + _NOT_COUNT), "硕士", 1),
    (re.compile(r"(?<![赌])博[ \t]?(" + _Y + ")" + _NOT_COUNT), "博士", 1),
    (re.compile(r"(?<![提升身最很较更太增拔])高[ \t]?(" + _Y + ")" + _NOT_COUNT), "高中", 1),
    # 「本科二年级」「本科第2年」「本科二年」；「本科四年里」是学制不是年级
    (re.compile(r"(本科|大学|研究生|硕士|博士|高中)[ \t]?(?:第[ \t]?)?(" + _Y + r")[ \t]?(?:年级|年(?![代份级里内中制间期来半多前后左]))"), 1, 2),
    # 「二年级」「二年级的研究生」
    (re.compile(r"(?<![0-9一二三四五六七八九十])(" + _Y + r")[ \t]?年级(?:[ \t]?的?[ \t]?(本科生|研究生|硕士生|博士生|本科|硕士|博士))?"), 2, 1),
    (re.compile(r"第[ \t]?(" + _Y + r")[ \t]?年(?![代份度级])"), None, 1),
    (re.compile(r"本科生|本科|大学生|硕士研究生|硕士生|硕士|研究生|博士研究生|博士生|博士(?!后)|在读博(?!士后)|读博(?!士后)|读研|高中生|高中"), 0, None),
    (re.compile(r"(?<![a-z])(freshman|sophomore|junior|senior)(?![a-z])"), "本科", 1),
    # 英文：只看紧跟在 year 后面的阶段词（「not first-year, I am a sophomore」不吞后面的）；PhD 优先于 graduate
    (re.compile(r"(?<![a-z])(first|second|third|fourth|fifth|sixth|1st|2nd|3rd|4th|5th|6th)[ \t-]?year(?![a-z])"
                r"((?:[ \t-]{1,3}(?:" + _EN_STAGE + r")(?![a-z])){0,3})(?:[ \t]{1,3}(?:student|candidate))?"), 2, 1),
    (re.compile(r"(?<![a-z])year[ \t]?([1-6])(?![0-9])((?:[ \t-]{1,3}(?:" + _EN_STAGE + r")(?![a-z])){0,3})"), 2, 1),
    (re.compile(r"(?<![a-z])(undergrad(?:uate)?|ph\.?d\.?|doctoral|master'?s|masters|graduate|grad)(?:[ \t]{1,3}(?:student|candidate))(?![a-z])"), 1, None),
    (re.compile(r"(?<![a-z])(undergrad(?:uate)?|ph\.?d\.?)(?![a-z])"), 1, None),
)
_EN_RANK = {"freshman": 1, "sophomore": 2, "junior": 3, "senior": 4}


class Grade(NamedTuple):
    stage: str | None
    year: int | None
    start: int
    end: int


def _stage_word(words: str) -> str | None:
    if re.search(r"ph\.?d|doctoral", words):
        return "博士"
    if re.search(r"undergrad|college|university", words):
        return "本科"
    if re.search(r"master|graduate|grad", words):
        return "硕士"
    return None


def grade_mentions(t: str) -> list[Grade]:
    """原话（已 prep）里按表认得出的每一处年级说法；互相包含的只留最长那处。"""
    found: list[Grade] = []
    for rx, stage, ygroup in _GRADE_FORMS:
        for m in rx.finditer(t):
            word = m.group(ygroup) if ygroup else None
            year = _EN_RANK.get(word or "") or _YEAR_OF.get(word or "")
            if stage == 0:
                st = _STAGE_OF.get(m.group(0))
            elif isinstance(stage, int):
                g = m.group(stage) or ""
                st = _STAGE_OF.get(g) or _stage_word(g)
            else:
                st = stage
            if year and year > _MAX_YEAR[st]:
                continue
            found.append(Grade(st, year, m.start(), m.end()))
    found.sort(key=lambda g: (g.start, -(g.end - g.start)))
    out: list[Grade] = []
    for g in found:
        if not any(o.start <= g.start and g.end <= o.end and (o.end - o.start) > (g.end - g.start) for o in out[-4:]):
            out.append(g)
    return out


class Windows(NamedTuple):
    """同一句引文在整条消息里的几处（从前往后、一样长）。"""
    starts: tuple[int, ...]
    length: int

    def contain(self, s: int, e: int) -> bool:
        # 起点在 [e - 长度, s] 之间的那几处才可能包住 [s, e)：二分，长消息里引文重复出现也不逐个比
        return bisect.bisect_right(self.starts, s) > bisect.bisect_left(self.starts, e - self.length)


def current_grades(text: str, windows: Windows | None = None) -> list[Grade]:
    """说的是现在的年级；给了 windows 只要落在其中某一段里的（位置按 prep 过的 text 算）。"""
    t = prep(text)
    sc = scan(t)
    return [g for g in grade_mentions(t) if (windows is None or windows.contain(g.start, g.end)) and is_current(sc, g.start, g.end)]


BAD = object()  # 结构化的值形状不对


def _year(v: Any) -> int | None | object:
    if v is None:
        return None
    if isinstance(v, bool):
        return BAD
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip() in _YEAR_OF:
        return _YEAR_OF[v.strip()]
    return BAD


def parse_grade(value: Any) -> tuple[str | None, int | None] | None | object:
    """{"stage", "year"} 或者一个整句就是年级的字符串（「大二」「本科二年级」「sophomore」）→（阶段, 第几年）。
    字符串认不出、或者年级后面还夹着别的（「本科二年级，GPA4.0」）→ None；字典形状不对 → BAD。"""
    if isinstance(value, dict):
        if not value or set(value) - {"stage", "year"}:
            return BAD
        st, yr = value.get("stage"), _year(value.get("year"))
        if st is not None:
            if not isinstance(st, str):
                return BAD
            st = _STAGE_OF.get(prep(st).strip())
            if st is None:
                return BAD
        if yr is BAD or (st is None and yr is None) or (yr is not None and not 1 <= yr <= _MAX_YEAR[st]):
            return BAD
        return st, yr
    t = re.sub(r"(?:在读|学生|在读生)$", "", prep(str(value or "")).strip()).strip()
    ms = grade_mentions(t)
    if len(ms) == 1 and ms[0].start == 0 and ms[0].end == len(t):
        return ms[0].stage, ms[0].year
    if len(ms) > 1 and all(m.start == 0 or t[ms[0].end:m.start].strip() == "" for m in ms) and ms[-1].end == len(t) \
            and len({(m.stage, m.year) for m in ms}) == 1:
        return ms[0].stage, ms[0].year
    return None


def render_grade(stage: str | None, year: int | None) -> str:
    cn = "一二三四五六"
    if stage and year:
        return {"本科": "大", "硕士": "研", "博士": "博", "高中": "高"}[stage] + cn[year - 1]
    return stage or f"{cn[(year or 1) - 1]}年级"


def grade_supported(stage: str | None, year: int | None, quote: str, windows: Windows | None = None) -> bool:
    """原话里有一处说的是现在的年级，阶段和第几年都对得上。值写了第几年，原话就得说了同一年；
    原话没说阶段（「二年级」「second-year」）只撑得住本科（本产品默认本科生）或不写阶段的值。"""
    for g in current_grades(quote, windows):
        if year is not None and g.year != year:
            continue
        if stage is None or g.stage == stage or (g.stage is None and stage == "本科"):
            return True
    return False


# ---------- 数量：数 + 单位 ----------

# 单位 → (量纲, 换成该量纲基本单位的倍数)。钟点按分钟、日历按天、月和年按月：一天不等于 24 小时的投入，一个月也不是 30 天
_UNIT: dict[str, tuple[str, float]] = {
    "秒": ("clock", 1 / 60), "秒钟": ("clock", 1 / 60), "分钟": ("clock", 1), "分": ("clock", 1), "min": ("clock", 1),
    "mins": ("clock", 1), "minute": ("clock", 1), "minutes": ("clock", 1), "小时": ("clock", 60), "钟头": ("clock", 60),
    "钟": ("clock", 60), "h": ("clock", 60), "hr": ("clock", 60), "hrs": ("clock", 60), "hour": ("clock", 60), "hours": ("clock", 60),
    "天": ("day", 1), "日": ("day", 1), "day": ("day", 1), "days": ("day", 1), "周": ("day", 7), "星期": ("day", 7),
    "礼拜": ("day", 7), "week": ("day", 7), "weeks": ("day", 7),
    "月": ("month", 1), "month": ("month", 1), "months": ("month", 1), "年": ("month", 12), "year": ("month", 12), "years": ("month", 12),
    "学期": ("term", 1), "semester": ("term", 1), "semesters": ("term", 1),
    "岁": ("age", 1), "周岁": ("age", 1), "years old": ("age", 1), "year old": ("age", 1), "yrs old": ("age", 1),
    "学分": ("credit", 1), "credits": ("credit", 1), "credit": ("credit", 1), "%": ("pct", 1), "percent": ("pct", 1), "倍": ("times", 1),
    "级": ("level", 1),
}
COUNTERS = ("个", "门", "项", "篇", "次", "期", "章", "节", "页", "题", "人", "本", "道", "场", "份")
_UNIT.update({c: ("count", 1) for c in COUNTERS})
_UNIT_MAX = max(map(len, _UNIT))
_PER_WORD = {"周": "w", "星期": "w", "礼拜": "w", "week": "w", "天": "d", "日": "d", "day": "d", "月": "mo", "month": "mo",
             "年": "y", "year": "y", "学期": "term", "semester": "term"}
_EN_NUM = {w: i for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                                        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
                                        "eighteen", "nineteen", "twenty"))} | {"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60}
_EN_TENS = r"(?:twenty|thirty|forty|fifty|sixty)[ \t-](?:one|two|three|four|five|six|seven|eight|nine)"
_TOKEN = re.compile(r"半(?=[ \t]?(?:个[ \t]?)?(?:小时|钟头|天|年|月|学期|周|星期))|\d{1,9}(?:\.\d{1,4})?|[零〇一二两三四五六七八九十百千万几]{1,12}(?:点[零〇一二三四五六七八九]{1,4})?"
                    r"|(?<![a-z])(?:" + _EN_TENS + "|" + "|".join(_EN_NUM) + r")(?![a-z])")
# 上下限不是定值：「每周不到五小时」撑不住「每周5小时」（Codex 复现）
_BOUND_BEFORE = re.compile(r"(?:不到|不足|少于|低于|最多|至多|顶多|不超过|小于|最少|至少|超过|多于|大于|不少于|上限|下限)[ \t]?$")
_BOUND_AFTER = re.compile(r"[ \t]?(?:以内|以下|之内|以上|出头|多一点|开外)")
_ENROLL_AFTER = re.compile(r"[ \t]?(?:秋季|9月|九月)?[ \t]?我?(?:是)?(?:入学|入校|进校|考入|考进|入读|进的|进了|来的|报到|来北大|进北大|上大学|毕业|出生|级|届)")
_RANGE = re.compile(r"[ \t]?(?:到|至|~|～|-|—|–|或)[ \t]?")
_MOD = re.compile(r"[ \t]?(多|来|余)?(个)?(多)?(半)?[ \t]?")
_JOIN = re.compile(r"[ \t]?(?:零|又)?[ \t]?")
_PER_BEFORE = re.compile(r"每[ \t]?(?:个)?(周|星期|礼拜|天|日|月|年|学期)|[一1][ \t]?(?:个)?(周|星期|礼拜|天|月)(?:里|内|中|下来)?|(周|日|月)均"
                         r"|(?<![a-z])(?:per|every|a|each)[ \t](week|day|month|semester)(?![a-z])|(?<![a-z])(week|dai)ly(?![a-z])")
_PER_AFTER = re.compile(r"[ \t]?(?:/|每|一|per |a |an |each )(?:个)?(周|星期|礼拜|天|日|月|学期|week|day|month|semester)")
_AGE_CTX = re.compile(r"(?:今年|年龄|年纪|虚岁|周岁|age|aged|i'm|i am)[ \t:]?(?:是|有|才|刚|已经)?[ \t]?$")
_CN_D = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_MUL = {"十": 10, "百": 100, "千": 1000}


class Qty(NamedTuple):
    lo: float          # 原样的数（换算前）
    hi: float
    dim: str           # "" = 没带单位
    base_lo: float     # 换成量纲基本单位后的数
    base_hi: float
    label: str         # 单位原样（计数单位门/篇/个之间要分开）
    per: str           # 「每周」= w；没说 = ""
    start: int
    end: int
    ctx: str = ""      # 没带单位、但跟在「今年」「年龄」后面 = "age"；四位的年份 = "calyear"
    bound: str = ""    # 「不到」「最多」「以上」这类上下限 = "<" / ">"


def _cn_value(run: str) -> tuple[float, float] | None:
    """中文数：「十九」「二十一」「两千零二十四」「二〇二四」；「三四」「两三」是范围、「十几」是 11 到 19。认不出返回 None。"""
    if "点" in run:  # 「一点五」= 1.5
        whole, frac = run.split("点", 1)
        w = _cn_value(whole) if whole else (0.0, 0.0)
        if not w or w[0] != w[1] or not all(c in _CN_D for c in frac):
            return None
        n = w[0] + float("0." + "".join(str(_CN_D[c]) for c in frac))
        return n, n
    if "几" in run:
        m = re.fullmatch(r"([一二两三四五六七八九]?)十几", run)
        if not m:
            return None
        d = _CN_D[m[1]] if m[1] else 1
        return d * 10 + 1, d * 10 + 9
    if all(c in _CN_D for c in run):
        if len(run) == 1:
            return float(_CN_D[run]), float(_CN_D[run])
        a, b = _CN_D[run[0]], _CN_D[run[1]]
        if len(run) == 2 and b == a + 1 and a > 0:
            return float(a), float(b)
        n = int("".join(str(_CN_D[c]) for c in run))
        return float(n), float(n)
    m = re.fullmatch(r"([一二两三四五六七八九])([一二两三四五六七八九])十", run)
    if m and _CN_D[m[2]] == _CN_D[m[1]] + 1:
        return _CN_D[m[1]] * 10.0, _CN_D[m[2]] * 10.0
    # 省略末位：「两千六」= 2600、「一百二」= 120、「一万二」= 12000（「两千零六」不省略）（原来算成 2006，Codex 复现）
    if len(run) >= 3 and run[-1] in _CN_D and run[-2] in "百千万" and "零" not in run and "〇" not in run:
        head = _cn_value(run[:-1])
        if head:
            n = head[0] + _CN_D[run[-1]] * {"百": 10, "千": 100, "万": 1000}[run[-2]]
            return n, n
    total = section = cur = 0
    for c in run:
        if c in _CN_D:
            cur = _CN_D[c]
        elif c in _CN_MUL:
            section += (cur or 1) * _CN_MUL[c]
            cur = 0
        elif c == "万":
            total += (section + cur or 1) * 10000
            section = cur = 0
        else:
            return None
    n = float(total + section + cur)
    return n, n


def _tok_value(tok: str) -> tuple[float, float] | None:
    if tok == "半":  # 「半小时」「半个月」
        return 0.5, 0.5
    if tok[0].isdigit():
        return float(tok), float(tok)
    if tok in _EN_NUM:
        return float(_EN_NUM[tok]), float(_EN_NUM[tok])
    en = re.fullmatch(r"(\w+)[ \t-](\w+)", tok)
    if en and en[1] in _EN_NUM and en[2] in _EN_NUM:  # 「twenty-one」
        return float(_EN_NUM[en[1]] + _EN_NUM[en[2]]), float(_EN_NUM[en[1]] + _EN_NUM[en[2]])
    return _cn_value(tok)


def _unit_at(t: str, pos: int) -> str:
    for n in range(min(_UNIT_MAX, len(t) - pos), 0, -1):
        u = t[pos:pos + n]
        if u in _UNIT and not (u[-1].isascii() and u[-1].isalpha() and t[pos + n:pos + n + 1].isascii()
                               and t[pos + n:pos + n + 1].isalpha()):
            if u == "年" and t[pos + 1:pos + 2] in ("级", "代", "份", "度"):
                return ""
            return u
    return ""


def _per_before(t: str, start: int, clause_start: int) -> str:
    """数前面不远处的「每周」「一天」「weekly」；中间隔着别的数就不算这个数的（「每周2小时，总共20周」）。"""
    last = None
    for m in _PER_BEFORE.finditer(t, max(clause_start, start - 16), start):
        last = m
    if last is None or _TOKEN.search(t, last.end(), start):
        return ""
    word = next(g for g in last.groups() if g)
    return "d" if word == "dai" else _PER_WORD.get(word, "")


@lru_cache(maxsize=64)
def quantities(text: str) -> tuple[Qty, ...]:
    """原话里的每个「数 + 单位」。复合时长（「两小时十分钟三十秒」「一小时又半小时」）合成一个总数；范围留两头；没带单位的阿拉伯数字也算，
    中文数要带单位、在括号里、有十百千或跟在「今年」后面才算（「一些」不是 1）。逐个数往后看，不回溯。同一句原话只解析一次。"""
    t = prep(text)
    clauses = _clauses(t)
    starts = [a for a, _ in clauses]
    out: list[Qty] = []
    skip_to = 0
    for m in _TOKEN.finditer(t):
        s, e = m.span()
        if s < skip_to:
            continue
        tok = m.group()
        val = _tok_value(tok)
        if val is None or (tok == "十" and t[e:e + 1] == "分" and t[e + 1:e + 2] != "钟"):  # 「十分喜欢」
            continue
        lo, hi = val
        pos = e
        r = _RANGE.match(t, pos)
        if r:
            m2 = _TOKEN.match(t, r.end())
            v2 = _tok_value(m2.group()) if m2 else None
            if v2 is not None:
                hi, pos = v2[1], m2.end()
        mod = _MOD.match(t, pos)
        half = 0.5 if mod.group(4) else 0.0
        upos = mod.end()
        unit = _unit_at(t, upos)
        if not unit and mod.group(4) is None:
            upos, unit = pos, _unit_at(t, pos)
        if half and not unit:
            half, unit = 0.0, ""
        lo, hi = lo + half, hi + half
        k = _clause_at(clauses, starts, s)
        cl_a, cl_b = clauses[k]
        if not unit:
            bracketed = t[s - 1:s] in ("(", "（") and t[pos:pos + 1] in (")", "）")
            age = bool(_AGE_CTX.search(t, max(cl_a, s - 8), s))
            spelled = len(tok) >= 2 and any(c in "十百千万" for c in tok)  # 「两百」「二十」没单位也是数；「一些」不是
            if tok[0].isdigit() or bracketed or age or spelled:
                year = tok.isdigit() and len(tok) == 4 and 1900 <= lo <= 2100  # 「2024入学」
                out.append(Qty(lo, hi, "", lo, hi, "", "", s, pos, "age" if age else "calyear" if year else ""))
            skip_to = pos
            continue
        dim, factor = _UNIT[unit]
        end = upos + len(unit)
        if t[s - 3:s] == "百分之":
            dim, factor = "pct", 1
        if unit == "年" and lo == hi and lo.is_integer() and 1900 <= lo <= 2100:
            dim, factor = "calyear", 1
        elif unit == "年" and lo < 100 and _ENROLL_AFTER.match(t, upos + 1):  # 「二三年入学」「24年入学」
            v = int("".join(str(_CN_D[c]) for c in tok)) if all(c in _CN_D for c in tok) else lo  # 「二三」这里是 23，不是两三
            dim, factor, lo, hi = "calyear", 1, 2000.0 + v, 2000.0 + v
        elif unit == "级" and lo >= 10:
            dim, factor = "calyear", 1
            lo, hi = (lo + 2000 if lo < 100 else lo), (hi + 2000 if hi < 100 else hi)
        if dim in ("clock", "day", "month") and t[end:end + 1] == "半" and not half:  # 「一年半」「两天半」
            lo, hi, end = lo + 0.5, hi + 0.5, end + 1
        base_lo, base_hi = lo * factor, hi * factor
        # 复合时长：后面一段接一段同一量纲、更小的单位，或者「又」连着同一单位，全部合成一个总数（原来只合两段，Codex 复现）
        last = factor
        while lo == hi and dim in ("clock", "day", "month"):
            j = _JOIN.match(t, end)
            m3 = _TOKEN.match(t, j.end())
            if not m3:
                break
            v3 = _tok_value(m3.group())
            mod3 = _MOD.match(t, m3.end())
            u3 = _unit_at(t, mod3.end())
            if not (v3 and u3 and v3[0] == v3[1] and _UNIT[u3][0] == dim
                    and (_UNIT[u3][1] < last or ("又" in j.group() and _UNIT[u3][1] == last))):
                break
            add = v3[0] + (0.5 if mod3.group(4) else 0.0)
            end = mod3.end() + len(u3)
            if t[end:end + 1] == "半":  # 「四十五分半」
                add, end = add + 0.5, end + 1
            base_lo = base_hi = base_lo + add * _UNIT[u3][1]
            last = _UNIT[u3][1]
        lo, hi = base_lo / factor, base_hi / factor
        per = _per_before(t, s, cl_a)
        pa = _PER_AFTER.match(t, end)
        if not per and pa:
            per = _PER_WORD.get(pa.group(1), "")
        bb = _BOUND_BEFORE.search(t, max(cl_a, s - 5), s)
        ba = _BOUND_AFTER.match(t, end)
        bound = ("<" if any(w in (bb or ba).group() for w in ("不到", "不足", "少于", "低于", "最多", "至多", "顶多", "不超过", "小于", "以内", "以下", "之内"))
                 else ">") if (bb or ba) else ""
        out.append(Qty(lo, hi, dim, base_lo, base_hi, unit, per, s, end, "", bound))
        skip_to = end
    return tuple(out)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 0.005  # 只容下四位小数的舍入（⅓ 小时 = 19.998 分钟）；按比例容差会让 2023 等于 2024


def qty_match(want: Qty, have: Qty, strict: bool = False) -> bool:
    """want（值里的）和 have（原话里的）是不是同一个量。定值只对定值、范围只对同一个范围：「每周四到六小时」「十几小时」撑不住「6小时」「19小时」（Codex 复现）。
    strict：结构化的数量字段（年龄、入学年份、每周时间），量纲必须一样（原话光一个数要有「今年」这类上下文），
    值写了「每周」原话也得说了同一个「每…」，原话是上下限（「不到五小时」）不算。"""
    if (want.lo == want.hi) != (have.lo == have.hi):
        return False
    if strict and (have.bound or (want.per and want.per != have.per)):
        return False
    if not want.dim or not have.dim:
        if want.dim and strict and have.ctx != want.dim:
            return False
        return _close(want.lo, have.lo) and _close(want.hi, have.hi)
    if want.dim != have.dim:
        return False
    if want.dim == "count" and want.label != have.label and "个" not in (want.label, have.label):
        return False  # 「个」和别的量词互通，「门」和「篇」不通
    if want.per and have.per and want.per != have.per:
        return False
    return _close(want.base_lo, have.base_lo) and _close(want.base_hi, have.base_hi)


def numbers_supported(value: str, quote: str) -> bool:
    """值里写的每个数，原话里都得有同一个数、单位对得上（值没写单位就只比数）。"""
    have = quantities(quote)
    return all(any(qty_match(w, h) for h in have) for w in quantities(value))


# ---------- 实体：学校、院系、专业 ----------

# 学校名 → 常见叫法。只收确定的简称；「交大」「中大」「南大」一叫多校，不收
SCHOOLS: dict[str, tuple[str, ...]] = {
    "北京大学": ("北大", "pku", "peking university"), "清华大学": ("清华", "thu", "tsinghua"),
    "复旦大学": ("复旦", "fudan"), "上海交通大学": ("上交", "上海交大", "sjtu"), "浙江大学": ("浙大", "zju"),
    "中国科学技术大学": ("中科大", "ustc"), "南京大学": ("nju",), "中国人民大学": ("人民大学", "人大", "ruc"),
    "北京航空航天大学": ("北航", "buaa"), "北京师范大学": ("北师大", "bnu"), "北京理工大学": ("北理工", "北理"),
    "武汉大学": ("武大", "whu"), "华中科技大学": ("华科", "华中科大", "hust"), "南开大学": ("南开",), "同济大学": ("同济",),
    "西安交通大学": ("西交", "西安交大", "xjtu"), "哈尔滨工业大学": ("哈工大",), "中山大学": ("sysu",), "厦门大学": ("厦大",),
    "四川大学": ("川大",), "山东大学": ("山大",), "吉林大学": ("吉大",), "东南大学": (), "天津大学": ("天大",),
    "中国科学院大学": ("国科大", "ucas"), "香港大学": ("港大", "hku"), "香港中文大学": ("港中文", "cuhk"),
    "香港科技大学": ("港科大", "hkust"), "国立台湾大学": ("台大",), "新加坡国立大学": ("新国立", "nus"),
    "麻省理工学院": ("mit",), "斯坦福大学": ("斯坦福", "stanford"), "哈佛大学": ("哈佛", "harvard"),
    "加州大学伯克利分校": ("伯克利", "berkeley"), "卡内基梅隆大学": ("cmu",), "牛津大学": ("牛津", "oxford"),
    "剑桥大学": ("剑桥", "cambridge"),
}
# 简称藏在别的校名里：「东北大学」里有「北大」
_CONFUSABLE = {"北大": ("东北大学", "西北大学", "湖北大学", "河北大学", "华北大学", "北大荒"), "人大": ("人大代表",),
               "山大": ("中山大学", "山大学"), "天大": ("天大学",), "华科": (), "同济": ()}
# 学科词本身不是院系：「我喜欢数学」不能撑「数学科学学院」，得是「数学系」「数学学院」「数学专业」
_SUBJECTS = {"数学", "物理", "化学", "生物", "经济", "法学", "心理", "社会", "社会学", "历史", "历史学", "哲学", "艺术", "新闻",
             "中文", "外国语", "信息管理", "信息科学技术", "政府管理", "工学", "考古"}
_DEPT_SUFFIX = ("系", "学院", "院", "专业", "的学生")


def _school_of(v: str) -> str | None:
    for name, short in SCHOOLS.items():
        if v == name or v in short:
            return name
    return None


def _depts() -> set[str]:
    import curriculum
    out = set()
    for c in curriculum.cards():
        out.update(x for x in str(c.get("院系") or "").split("/") if x)
    return out


def _dept_of(v: str) -> str | None:
    import curriculum
    depts = _depts()
    if v in depts:
        return v
    if curriculum.DEPT_ALIAS.get(v) in depts:
        return curriculum.DEPT_ALIAS[v]
    hit = [d for d in depts if len(v) >= 2 and v in d]
    return hit[0] if len(hit) == 1 else None


def _variants(name: str) -> set[str]:
    plain = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
    out = {name.strip(), plain}
    return {x.strip() for x in out | {x.replace("专业", "") for x in out} if len(x.strip()) >= 2}


def _major_names(v: str) -> set[str]:
    """同一个专业的叫法。卡片名里空格隔开的是几个不同的专业（「国际政治专业 … 外交学专业」），各算各的：
    原来整张卡的检索别名都当同一个，「我是外交学专业的」撑住了「国际政治」（Codex 复现）。"""
    import curriculum
    names: set[str] = set()
    for c in curriculum.cards():
        for n in (c.get("专业", ""), c.get("目录专业名", ""), *c.get("检索别名", [])):
            for piece in str(n or "").split():
                forms = _variants(piece)
                if v in forms or v + "专业" in forms:
                    names |= forms
    return names


def entity_forms(key: str, value: str) -> tuple[str | None, set[str]]:
    """学校/院系/专业的值 →（库里的规范名，原话里可以出现的叫法）。库里没有的，只认原样。"""
    v = re.sub(r"\s+", "", prep(value))
    if key == "school":
        name = _school_of(v)
        return name, ({prep(name), *SCHOOLS[name]} if name else {v})
    if key == "department":
        import curriculum
        name = _dept_of(v)
        if not name:
            return None, {v}
        return name, {name, *(k for k, d in curriculum.DEPT_ALIAS.items() if d == name and k not in _AMBIGUOUS)}
    names = _major_names(v)
    return (v, names) if names else (None, {v})


def _find(t: str, form: str) -> list[tuple[int, int]]:
    """form 在 t 里出现的每一处：中文字之间可以夹一两个空格，英文要有词界。"""
    form = form.strip()
    if not form:
        return []
    body = "[ \t]{0,2}".join(re.escape(c) for c in form if not c.isspace())
    if form.isascii():
        body = r"(?<![a-z0-9])" + body + r"(?![a-z0-9])"
    return [m.span() for m in re.finditer(body, t)]


# 简称本身另有常见意思：「我去法院旁听」不是法学院（Codex 复现）
_AMBIGUOUS = {"法院"}
# 学校的附属机构不是学籍：「北大附中」「清华大学出版社」（Codex 复现）
_SCHOOL_NOT = re.compile(r"附中|附小|附属|出版社|医院|校友")
# 学校院系专业要作为学籍说出来（Codex 复现：「借了本经济学教材」「物院门口」「北大邮箱」「信科招助理吗」都只是提到）：
# 名字前面紧挨着「是、在、读、属于、同属…」，或者后面紧跟「大二、学生、在读、专业、的。」，或者整句就是这个名字（回答「哪个院的」）
_MEMBER_PRE = re.compile(r"(?:是|在|读|念|就读|属于|同属|隶属|来自|主修|辅修|学|考上|考进|考入|进了?|转到|转入|转去|就在)[了的]?[ \t:：]*$"
                         r"|(?:专业|学校|院系|学院|本科专业)(?:是|定了|写|填|就是|叫|名)?[ \t:：]*$|我这个|作为|身为"
                         r"|(?<![a-z])(?:i'm at|i am at|i study at|study at|studying at|from|major in|majoring in)[ \t]*$")
_MEMBER_POST = re.compile(r"是我(?:的|们)?(?:现在的|本科的?|目前的)?(?:专业|学校|院系|学院|系)|这边的我|的我|(?:专业|系)?(?:的?(?:学生|本科生|研究生|博士生|人)|大[一二三四五六]|研[一二三]|博[一二三四五六]|[0-9]{2,4}级|级|届|在读|读书|念书|上学|本科|硕士|博士)"
                          r"|(?:专业|系)?的?[ \t~～…!！。.]*$")
_MEMBER_NOT = re.compile(r"[ \t]?(?:附近|门口|旁边|对面|周边|门外|楼下|出版|发的|发了|招|附中|附小|附属|医院|校友|选过|上过|旁听|蹭|(?:专业)?的(?:作业|同学|书|课程)|教材|邮箱|课程)")
_BARE = re.compile(r"[ \t~～…!！。.啊呀吧呢哈嗯哦]*")


@lru_cache(maxsize=1)
def _known_names() -> tuple[str, ...]:
    """库里所有学校、院系、专业的叫法，长的在前。用来认「化学生物学」整个是一个专业、里面的「化学」不是另一个。"""
    import curriculum
    names = {prep(n) for name, short in SCHOOLS.items() for n in (name, *short)} | {prep(n) for n in curriculum.known_names()}
    names |= _depts() | {v for c in curriculum.cards() for n in (c.get("专业", ""), c.get("目录专业名", "")) for p in str(n or "").split()
                         for v in _variants(p)}
    return tuple(sorted((n for n in names if len(n) >= 2), key=len, reverse=True))


def _covered(t: str, s: int, e: int, longer: list[str]) -> bool:
    """[s, e) 被原话里某个更长的名字整个包住。"""
    for n in longer:
        i = t.find(n, max(0, e - len(n)), s + len(n))
        while i != -1:
            if i <= s and e <= i + len(n):
                return True
            i = t.find(n, i + 1, s + len(n))
    return False


def entity_occurrences(key: str, value: str, quote: str, windows: Windows | None = None) -> list[tuple[int, int]]:
    """这个学校/院系/专业在原话里作为学籍出现的每一处（给了 windows 只看落在里面的）：被更长的另一个名字包着的不算（「化学生物学」里的「化学」，Codex 复现）。"""
    t = prep(quote)
    sc = scan(t)
    _, forms = entity_forms(key, value)
    out = []
    for f in forms:
        longer = [n for n in _known_names() if len(n) > len(f) and f in n and n not in forms]
        for s, e in _find(t, f):
            if windows is not None and not windows.contain(s, e):
                continue
            if key == "school" and (any(t.find(c, max(0, s - len(c)), e + len(c)) != -1 for c in _CONFUSABLE.get(f, ()))
                                    or _SCHOOL_NOT.match(t, e) or (t.startswith("大学", e) and _SCHOOL_NOT.match(t, e + 2))):
                continue
            if key == "department" and f in _SUBJECTS and not t.startswith(_DEPT_SUFFIX, e):
                continue
            # 「法学院」里的「法学」只按「法学院」算（学科词后面跟的是院名，不是学籍说法）
            if _covered(t, s, e, longer) or (f in _SUBJECTS and _covered(t, s, e, [x for x in forms if len(x) > len(f) and f in x])):
                continue
            a, b = sc.clause(s)
            if _MEMBER_NOT.match(t, e) or (t.startswith("大学", e) and _MEMBER_NOT.match(t, e + 2)):
                continue
            bare = _BARE.fullmatch(t, a, s) and _MEMBER_POST.fullmatch(t, e, b)
            if bare or _MEMBER_PRE.search(t, max(a, s - 8), s) or (_MEMBER_POST.match(t, e, b) and _MEMBER_POST.match(t, e, b).end() > e):
                out.append((s, e))
    return out


# ---------- 原话片段：兴趣、目标 ----------

# 同一样东西的几种叫法。值抄原话最好；写成这里的另一种叫法也认
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("ai", "人工智能"), ("ml", "机器学习"), ("nlp", "自然语言处理"), ("cv", "计算机视觉"), ("rl", "强化学习"),
    ("dl", "深度学习"), ("llm", "llms", "大模型", "大语言模型"), ("hci", "人机交互"), ("econ", "经济学"),
    ("cs", "计算机科学", "计算机"), ("读研", "读研究生", "读硕士", "读硕"), ("读博", "读博士", "攻读博士", "phd"),
    ("出国", "留学", "出国留学"), ("就业", "工作", "找工作"),
)


def _has(text: str, form: str) -> bool:
    return bool(_find(text, form))


def span_forms(value: str) -> set[str]:
    """值本身，加上把里面的缩写换成同组别的叫法（「AI安全」也找「人工智能安全」）。"""
    v = re.sub(r"\s+", " ", prep(value)).strip()
    forms = {v}
    for group in ALIAS_GROUPS:
        for a in group:
            if not _has(v, a):
                continue
            for f in list(forms)[:8]:
                for b in group:
                    if b != a:
                        forms.add(re.sub((r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])") if a.isascii() else re.escape(a), b, f))
    return forms


def span_occurrences(value: str, quote: str) -> list[tuple[int, int]]:
    t = prep(quote)
    return sorted({o for f in span_forms(value) for o in _find(t, f)})


def runs(value: str) -> list[str]:
    """值里能拿去原话里找的片段：两个字以上的连续中文、两个字母以上的英文词。"""
    return [r for r in re.findall(r"[一-鿿]{2,}|[a-z][a-z0-9+#.]{1,}", prep(value)) if r]


def anchors(value: str, quote: str, min_len: int = 4) -> list[str]:
    """值和原话共有的片段：从左往右每次取原话里也有的最长一段中文（至少 min_len 个字），加上原话里也有的英文词。"""
    t = prep(quote)
    out = []
    for r in runs(value):
        if r.isascii():
            if _has(t, r):
                out.append(r)
            continue
        i = 0
        while i <= len(r) - min_len:
            j = i + min_len
            if r[i:j] not in t:
                i += 1
                continue
            while j < len(r) and r[i:j + 1] in t:
                j += 1
            out.append(r[i:j])
            i = j
    return out
