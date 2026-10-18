"""成绩单解析 + 绩点计算。

格式和公式来自北大同学写的开源计算器
（https://github.com/PKUMuZi/pku-gpa-calculator ，MIT），
这里做的是把它的解析规则**照原样**移植进后端，另外加两件事：
  1. 分数校验（我们对「不编造」的要求比一个本地计算器高）；
  2. 失败时给出人能看懂的原因，而不是静默丢数据。

为什么要单独一个模块而不是塞进 memory.py：这里是**纯函数、无副作用**，
可以拿真实成绩单逐条对着算，不需要起服务、不需要模型。

——粘贴格式（树洞/教务导出，一行一项，顺序固定）——

    25-26学年度2学期      ← 学期标题，决定下面这些课属于哪个学期
    3                      ← 学分
    学分                   ← 字面量，用来锚定一条记录的起点
    金融会计               ← 课程名
    任选                   ← 课程性质（可以没有，也可以有多行）
    W                      ← 成绩

一条记录 = 学分 / "学分" / 课程名 / (课程性质)* / 成绩。
学期标题是**倒序**出现的（最新的在前），所以解析结果需要自己按学期排序。
"""

from __future__ import annotations

import re
from typing import Any, NamedTuple

# 学期标题：25-26学年度1学期 / 24-25学年度3学期
_SEMESTER_RE = re.compile(r"^(\d{2}-\d{2})学年度([123])学期$")

# 成绩可能长这样：分数、W（退课）、I（缓考）、P/F、合格/不合格、缓考、退课、
# 字母等级（B+，北大部分课程尤其通选课用）、IP（在修，成绩未出）、
# 五级制（优秀/良好/中等/及格/不及格）。
#
# 这几类都是实际成绩单上会出现的（拿真实格式的成绩单跑过）：
# 字母等级（B+，部分课程尤其通选课用）、IP（在修，成绩未出）。
# 原来的正则一个都不认，于是那几门课连同学分被整条丢掉。
_GRADE_RE = re.compile(
    r"^(?:W|I|P|F|IP|NP|合格|不合格|缓考|退课|在修|进行中|"
    r"优秀|良好|中等|及格|不及格|"  # 原来漏了「不及格」：挂的课连同学分整条消失
    r"[A-Fa-f][+-]?|"
    r"\d+(?:\.\d+)?)$")

# 课程性质。允许出现多行（例如「专业必修」+「全校必修」），取最后一行。
_KIND_RE = re.compile(
    r"^(?:专业必修|专业选修|专业限选|限选|任选|全校任选|全校必修|全校选修|"
    r"通选课|公共必修|公共选修|公选|必修|选修)$")

# 教务导出里夹的噪声行，见到就跳过
_NOISE_RE = re.compile(
    r"^(?:刷新|隐藏文字|显示文字|总学分|总绩点|该GPA系根据|具体各场景|"
    r"学分|成绩|课程名称)$")

# 同一类噪声，但可能夹在别的字里（例如整行是「刷新 隐藏文字」）。
# 用 search 而不是 match，免得一个空格就让它漏过去、又被报成「没看懂」。
_NOISE_ANY_RE = re.compile(r"刷新|隐藏文字|显示文字|该GPA系根据|具体各场景")

# 形如 3.700 的裸绩点数字，容易和学分/成绩混淆，按噪声处理
_BARE_GPA_RE = re.compile(r"^-?\d+\.\d{3,}$")

_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")

# 非百分制的成绩状态 → 统一写法
_STATUS_ALIASES = {
    "W": "W", "WITHDRAW": "W", "退课": "W", "退": "W",
    "I": "I", "缓考": "I",
    "IP": "IP", "在修": "IP", "进行中": "IP", "IN PROGRESS": "IP",
    "P": "合格", "PASS": "合格", "合格": "合格", "通过": "合格",
    "F": "不合格", "FAIL": "不合格", "不合格": "不合格", "不通过": "不合格",
    "NP": "不合格",
}

# 字母等级。**我们不把它们换算成绩点**，原因见 gpa_of 的注释。
LETTER_GRADES: tuple[str, ...] = (
    "A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "F")

# 五级制。同样不换算成绩点，只判断通过与否。
LEVEL_GRADES: tuple[str, ...] = ("优秀", "良好", "中等", "及格", "不及格")

# 成绩还没出的状态：课要先记下来（它就是「在修」），但不参与任何统计
IN_PROGRESS = ("IP",)

# 明确没通过
_FAILED_GRADES = ("不合格", "不及格", "F", "W", "I", "")

# 不计入 GPA 的成绩形态
NOT_GRADED = ("W", "I", "合格", "不合格", "缓考", "退课", "", "IP") + LETTER_GRADES + LEVEL_GRADES

# 低于这个分数，绩点按 0 计。
#
# 注意这里和参考实现**故意不同**：参考的 gpaFromScore 不设下限，
# 于是 50 分会算出 4-3*(50)^2/1600 = -0.6875 的**负绩点**，
# 累加进平均绩点会把别的课拖下去。负绩点没有意义，
# 北大教务的口径也是不及格记 0。所以这里截到 0。
# 如果你确认要照参考实现原样（不截），把 GPA_FLOOR_AT_ZERO 改成 False 即可。
GPA_FLOOR_AT_ZERO = True
_PASS_MARK = 60.0


def gpa_of(grade: Any) -> float | None:
    """单门课绩点。百分制之外的状态返回 None（不计入 GPA）。

    GPA(x) = 4 - 3(100-x)^2 / 1600
    """
    n = _to_float(grade)
    if n is None or n < 0 or n > 100:
        return None
    g = 4 - 3 * (100 - n) ** 2 / 1600
    if GPA_FLOOR_AT_ZERO and n < _PASS_MARK:
        return 0.0
    return g


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("W", "I", "合格", "不合格", "缓考", "退课"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def normalize_grade(value: Any) -> str:
    """把各种写法收敛成统一状态；百分制分数原样保留成字符串。

    保留成字符串而不是 float，是因为 W/I/合格 这些不是数字，
    而且 88 和 88.0 要显示成用户原本看到的样子。
    """
    s = str(value if value is not None else "").strip()
    if not s:
        return ""
    up = s.upper()
    if up in _STATUS_ALIASES:
        return _STATUS_ALIASES[up]
    if s in _STATUS_ALIASES:
        return _STATUS_ALIASES[s]
    # 字母等级统一成大写：成绩单上可能写 b+ 也可能写 B+
    if up in LETTER_GRADES:
        return up
    f = _to_float(s)
    if f is not None:
        # 整数就显示整数，别把 88 变成 88.0
        return str(int(f)) if f == int(f) else str(f)
    return s


def is_passed(grade: Any) -> bool:
    """这门课算不算「通过学分」。

    不通过的：W（退课）/I（缓考）/不合格/不及格/F/IP（成绩还没出）/空。
    字母等级按「不是 F 就算过」判断——B+ 显然是通过的，
    原来的实现因为它不是数字就判成没过，那门课连学分都不算。
    """
    s = normalize_grade(grade)
    if s in _FAILED_GRADES:
        return False
    if s == "合格":
        return True
    if s in LETTER_GRADES:
        return s != "F"
    if s in LEVEL_GRADES:
        return s != "不及格"
    f = _to_float(s)
    return f is not None and f >= _PASS_MARK


def term_label(year: str, term: str) -> str:
    return f"{year}学年度{term}学期"


def term_sort_key(term: str) -> str:
    """学期做字符串排序用的键。25-26学年度1学期 → 25261。

    不这么做的话按字面排序，「9-10学年度」会排在「25-26学年度」后面。
    统一成 4 位年（25xx）+ 学期，字典序就等于时间序。
    """
    m = re.match(r"^(\d{2})-(\d{2})学年度([123])学期$", str(term or ""))
    if not m:
        return str(term or "")
    return f"{m[1]}{m[2]}{m[3]}"


def looks_like_transcript(text: str) -> bool:
    """这段文本像不像粘贴进来的成绩单。

    用户的原话：「为什么我直接在对话里输入成绩单，不能直接智能识别？
    只能在核对页面手动输入才能识别」

    判据刻意保守，两条同时满足才算：
      1. 出现学期标题行（`25-26学年度1学期`）——这是教务导出的固定形状
      2. 真的解析出 ≥2 门课

    为什么不按「数字多不多」判断：用户随口说「高数 85、线代 90」也全是数字，
    但那是**自述**，该走记忆写入（可改、可否认），不是成绩单底稿。
    把它当成绩单会让一条随口的话变成「硬证据」，这正是最该避免的事。

    宁可漏判（让他去核对页粘，那里一定会认）也不要错判。
    """
    t = str(text or "")
    if not any(_SEMESTER_RE.match(line.strip())
               for line in t.replace("\r", "\n").split("\n")):
        return False
    try:
        return len(parse_transcript(t)["courses"]) >= 2
    except Exception:
        # 解析器自己出问题不该让整轮对话挂掉
        return False


def parse_transcript(text: str) -> dict[str, Any]:
    """解析粘贴的成绩单。

    返回 {"courses": [...], "warnings": [...], "terms": [...]}。
    **解析不出来就明确说为什么**，不猜、不补。
    """
    lines = [s.strip() for s in str(text or "").replace("\r", "\n").split("\n")]
    lines = [s for s in lines if s]

    courses: list[dict[str, Any]] = []
    warnings: list[str] = []
    current_term = ""
    unseen: list[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        m = _SEMESTER_RE.match(line)
        if m:
            current_term = line
            i += 1
            continue

        if (_NOISE_RE.match(line) or _NOISE_ANY_RE.search(line)
                or _BARE_GPA_RE.match(line)):
            i += 1
            continue

        # 记录起点：一个数字，紧跟字面量「学分」
        if _NUM_RE.match(line) and i + 1 < len(lines) and lines[i + 1] == "学分":
            credits = float(line)
            name = lines[i + 2] if i + 2 < len(lines) else ""
            j = i + 3
            kind = ""
            while j < len(lines) and _KIND_RE.match(lines[j]):
                kind = lines[j]
                j += 1
            grade = lines[j] if j < len(lines) else ""

            if not current_term:
                warnings.append("有课程出现在任何学期标题之前，已跳过。"
                                "请确认文本里有类似「25-26学年度1学期」的行。")
                i += 1
                continue
            if not name or not _GRADE_RE.match(grade):
                # 结构对不上就不要硬猜。把**整条**跳过（i = j + 1），
                # 不要只前进一行：那样这条记录剩下的每一行都会再被当成
                # 新记录的开头，于是一处格式不认就炸出一串「没看懂」。
                # 报出来的是成绩那一行（有问题的是它），不是学分那一行。
                unseen.append(f"{name} {grade}".strip() if name else line)
                i = max(j + 1, i + 1)
                continue

            courses.append({
                "term": current_term,
                "course": name,
                "credits": credits,
                "kind": kind,
                "grade": normalize_grade(grade),
                # IP = 成绩还没出，也就是「在修」。这类课要记下来
                # （用户的信息分类里「在修课程」是单独一类），
                # 但不能混进已修课程的统计里。
                "status": "current" if normalize_grade(grade) in IN_PROGRESS else "completed",
            })
            i = j + 1
            continue

        # 孤立的数字：不是记录开头就不是课程数据（例如「总学分」下面那个 43）。
        # 这种不该报成「没看懂」——一份完全正常的成绩单末尾都有它，
        # 每次都弹一条警告只会让人以为格式错了。
        if _NUM_RE.match(line):
            i += 1
            continue

        unseen.append(line)
        i += 1

    if not courses:
        if not _SEMESTER_RE.search("\n".join(lines)):
            warnings.append("没识别到学期标题。格式应类似「25-26学年度1学期」。")
        warnings.append("没识别到课程。一条记录长这样：学分 / 学分 / 课程名 / 课程性质 / 成绩。")
    elif unseen:
        warnings.append(f"有 {len(unseen)} 行没看懂，已跳过（例如：{unseen[0][:24]}）。")

    terms = sorted({c["term"] for c in courses}, key=term_sort_key)
    return {"courses": courses, "warnings": warnings, "terms": terms}


def summarize(courses: list[dict[str, Any]]) -> dict[str, Any]:
    """按学分加权算总绩点 / 均分。

    口径和参考实现一致：
      - 通过学分：所有通过（含「合格」）的课，不要求有分数
      - 计 GPA 学分：**只有百分制分数**的课。W/I/合格/不合格不进分母
    这条区分很重要：把「合格」当成 60 分算进去，会把绩点算低。

    **字母等级（B+ 等）和五级制（优秀 等）计入通过学分，但不计入 GPA**，
    并且单独报出来（`ungraded`），让界面能说清「这个绩点少算了哪几门」。
    不换算的理由见 gpa_of。
    """
    passed_credits = 0.0
    gpa_credits = 0.0
    weighted_gpa = 0.0
    weighted_score = 0.0
    counted = 0
    ungraded: list[dict[str, Any]] = []
    in_progress: list[dict[str, Any]] = []

    for c in courses:
        credits = max(0.0, float(c.get("credits") or 0))  # 不取整：0.5 学分的实验课原来被 round 成 0，绩点和学分都算错
        grade = normalize_grade(c.get("grade"))
        if grade in IN_PROGRESS:
            # 成绩还没出：既不算通过学分，也不算绩点，但要单独报出来
            in_progress.append({"course": c.get("course"), "credits": credits})
            continue
        if is_passed(grade):
            passed_credits += credits
        g = gpa_of(grade)
        if g is not None:
            gpa_credits += credits
            weighted_gpa += credits * g
            weighted_score += credits * float(grade)
            counted += 1
        elif is_passed(grade) and grade not in ("合格",):
            # 通过了、但换算不了绩点：字母等级 / 五级制
            ungraded.append({"course": c.get("course"), "grade": grade, "credits": credits})

    return {
        "total": len(courses),
        "passed_credits": passed_credits,
        "gpa_credits": gpa_credits,
        "gpa": (weighted_gpa / gpa_credits) if gpa_credits > 0 else None,
        "avg_score": (weighted_score / gpa_credits) if gpa_credits > 0 else None,
        # 计 GPA 的门数，和 total 不同——差在 W/I/合格 这些
        "gpa_courses": counted,
        "ungraded": ungraded,
        "ungraded_credits": sum(u["credits"] for u in ungraded),
        "in_progress": in_progress,
        "in_progress_credits": sum(u["credits"] for u in in_progress),
    }


def summarize_by_term(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按学期分组的汇总，学期按时间正序。"""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for c in courses:
        buckets.setdefault(str(c.get("term") or "未标注学期"), []).append(c)
    out = []
    for term in sorted(buckets, key=term_sort_key):
        s = summarize(buckets[term])
        s["term"] = term
        out.append(s)
    return out


# ---------- 从成绩单推导能力 ----------
#
# 这是「能力」这一层的正确来源。以前能力是**模型在对话里凭一段话写出来的**，
# 而 base:code 那起事故（把「Vibecoding」写成「依赖 AI 生成」、并加上
# 「课程基础扎实」）之所以发生，根子就在这里：没有底稿，模型只能编判断。
#
# 两条设计红线：
#   1. **不写判断，只写事实。** value 里出现的只能是「修过哪几门、考了多少、
#      加权平均多少」。绝不出现「基础扎实」「能力偏弱」这类词——
#      那是我们的推断，一旦写进「事实」就再也分不清了。
#      难度判断交给读到这条事实的模型自己下。
#   2. **只统计通过的课。** 挂了的高等数学不能算「修过数学类课程」，
#      那句话会误导人。
#
# 关键词是**匹配用的模式**，不是编造的课程名——它们只在用户真实录入的
# 课程名上做子串匹配，匹配不上就不产出任何东西。


class CapabilityRule(NamedTuple):
    area: str
    label: str
    patterns: tuple[str, ...]
    min_courses: int
    # 这条结论改变哪个决策（闸门③）。不写「能力」这种笼统的——要落到具体决策上。
    affects: str = "task_difficulty"


CAPABILITY_RULES: tuple[CapabilityRule, ...] = (
    CapabilityRule("math", "数学类课程", (
        "高等数学", "数学分析", "线性代数", "高等代数", "概率统计", "数理统计",
        "离散数学", "常微分", "偏微分", "抽象代数", "实变函数", "复变函数",
        "数学物理", "拓扑", "数论",
    ), 2),
    CapabilityRule("code", "编程类课程", (
        "计算概论", "程序设计", "数据结构", "算法", "面向对象", "Python",
        "python", "C语言", "C++", "Java", "数值计算", "编译原理", "操作系统",
        "数据库", "计算机网络",
    ), 1),
    CapabilityRule("data", "统计与数据类课程", (
        "概率统计", "数理统计", "数据分析", "数据科学", "数据可视化", "数据挖掘",
        "计量经济", "统计学习", "机器学习", "回归分析", "时间序列", "抽样调查",
    ), 1),
    # 经济金融类课多了，指向的是「他可能想做经济/金融方向」，
    # 改变的是方向建议而不是任务难度。
    CapabilityRule("econ", "经济金融类课程", (
        "经济学原理", "微观经济", "宏观经济", "政治经济", "计量经济", "金融",
        "会计", "投资", "货币", "财政", "国际贸易", "保险",
    ), 2, affects="direction_choice"),
    CapabilityRule("english", "英语类课程", (
        "英语", "英文", "学术英语",
    ), 1),
)


def match_areas(course: str) -> list[str]:
    """一门课落在哪些领域。可以同时属于多个（例如「概率统计」既是数学也是统计）。"""
    name = str(course or "")
    return [r.area for r in CAPABILITY_RULES
            if any(p in name for p in r.patterns)]


def _rollup(rule: CapabilityRule, hits: list[dict[str, Any]]) -> dict[str, Any]:
    """把命中的课拼成一句**可核对的事实**。

    没有百分制分数的课（合格/W/缓考）照实说，不折算成一个假分数。
    """
    graded = [(c, _to_float(c.get("grade"))) for c in hits]
    graded = [(c, g) for c, g in graded if g is not None]

    if graded:
        total_c = sum(max(0.0, float(c.get("credits") or 0)) for c, _ in graded)
        if total_c > 0:
            avg = sum(max(0.0, float(c.get("credits") or 0)) * g for c, g in graded) / total_c
            basis = "学分加权平均"
        else:
            avg = sum(g for _, g in graded) / len(graded)
            basis = "平均"
        shown = "、".join(f"{c['course']} {normalize_grade(c['grade'])}" for c, _ in graded[:4])
        more = f" 等 {len(graded)} 门" if len(graded) > 4 else ""
        value = (f"修过 {len(hits)} 门{rule.label}，{basis} {avg:.1f}：{shown}{more}")
    else:
        shown = "、".join(c["course"] for c in hits[:4])
        more = f" 等 {len(hits)} 门" if len(hits) > 4 else ""
        avg = None
        value = f"修过 {len(hits)} 门{rule.label}（{shown}{more}），成绩为合格/未出分"

    return {
        "key": f"transcript:{rule.area}",
        "area": rule.area,
        "label": rule.label,
        "value": value[:200],
        "affects": rule.affects,
        "count": len(hits),
        "avg": avg,
        "enrollments": [
            {"course": c["course"], "grade": normalize_grade(c["grade"]),
             "credits": float(c.get("credits") or 0), "term": c.get("term") or ""}
            for c in hits
        ],
    }


def derive_capabilities(courses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从成绩单推导出「修过哪几类课」。

    **幂等且完整**：同样的输入永远产出同样的结果，不依赖历史。
    调用方负责把结果与库里已有的对齐（增/改/撤）。
    """
    passed = [c for c in courses if is_passed(c.get("grade"))]
    out: list[dict[str, Any]] = []
    for rule in CAPABILITY_RULES:
        hits = [c for c in passed if any(p in str(c.get("course") or "")
                                         for p in rule.patterns)]
        if len(hits) < rule.min_courses:
            continue
        hits.sort(key=lambda c: (term_sort_key(str(c.get("term") or "")), c["course"]))
        out.append(_rollup(rule, hits))
    return out


# 推导出的事实挂在哪个决策点上（闸门③）。能力直接改变任务难度。
DERIVED_AFFECTS = "task_difficulty"

