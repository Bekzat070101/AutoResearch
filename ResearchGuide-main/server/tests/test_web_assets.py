# -*- coding: utf-8 -*-
"""前端静态文件必须能被浏览器正确读出来。

起因：改一个缓存版本号，我用了
    (Get-Content x.html -Raw) -replace ... | Set-Content x.html
PowerShell 在中途按错误编码读了一次，把 4 个中文字连同它后面的 ASCII
一起压成了 U+FFFD。后果**不是显示瑕疵，是结构损坏**：

    定下来</small>   →   定下�?/small>      ← `</small>` 的 `<` 没了
    工作区">         →   工作�?              ← aria-label 的收尾引号没了
    下一步。" />     →   下一步�? />         ← content 的收尾引号没了

typecheck 过、单元测试全绿、git diff 里那几行看着也正常，
**只有把真正发出去的字节解出来才会发现**。所以这条测试直接读文件字节。

以后改这些文件一律走 write/edit 工具，不要用 PowerShell 的 Get-Content/Set-Content。
"""
from __future__ import annotations

import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent.parent / "web"

FILES = ["index.html", "css/styles.css", "js/app.js", "js/chat.js"]


@pytest.mark.parametrize("rel", FILES)
def test_web_file_is_valid_utf8_without_replacement_chars(rel):
    p = WEB / rel
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        pytest.fail(f"{rel} 不是合法 UTF-8：{e}。多半是被非 UTF-8 的工具写过。")
    bad = text.count("\ufffd")
    assert bad == 0, (
        f"{rel} 里有 {bad} 个 U+FFFD（编码损坏）。"
        "不要用 PowerShell 的 Get-Content/Set-Content 改这些文件，用 write/edit 工具。")


def test_index_html_tags_are_intact():
    """属性被吃掉收尾引号、闭合标签少了 `<`——这类损坏会让浏览器
    把后面的标签当属性吞掉（样式表就曾整个被吞进 meta 里）。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for tag in ("</title>", "</small>", "</p>", "</nav>", "</button>"):
        assert tag in html, f"index.html 少了 {tag}"
    # 每个属性都要有收尾引号；数引号是否成对是最省事的算法
    assert html.count('"') % 2 == 0, "index.html 里的引号不成对，有属性没闭合"
    assert '<link rel="stylesheet"' in html, "样式表链接不在（可能被坏属性吞了）"
    assert html.count("<script") >= 2, "脚本标签不齐"


def test_web_files_have_no_utf8_bom():
    """BOM 会让 `#!` 之类的开头失效，而且不同工具对它处理不一致。"""
    for rel in FILES:
        raw = (WEB / rel).read_bytes()
        assert raw[:3] != b"\xef\xbb\xbf", f"{rel} 带了 UTF-8 BOM"


def test_no_questionnaire_view_is_left():
    """「问卷」两个字本身可以出现（解释为什么删掉它的注释、
    课程搜索的占位提示），但它**不能还是一个视图**。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert '"问卷"' not in js, "「问卷」又被当成标签名了"
    assert 'case "onboarding"' not in js, "onboarding 视图分支又回来了"
    assert "function portraitTabs" not in js
    assert "function renderConfirm" not in js
    assert "function confirmDrafts" in js


def test_build_stamp_matches_every_cache_buster():
    """版本戳上写的号，必须和**每一个** ?v= 完全一致。

    这条是一个真事故换来的：上次把 JS 从 w27 升到 w28 时，
    `styles.css?v=w27` 忘了改，于是用户拿到「新 JS + 旧 CSS」——
    项目文件夹、任务面板那些样式全没加载，但版本戳理直气壮地说 w28。
    版本戳本来就是为了让「我改了没有」一眼可确认；
    它自己跟缓存参数不一致的时候，比没有版本戳更坏——它会让人以为已经刷新过了。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    stamp = re.search(r'class="build-stamp">\s*界面版本\s*<b>(w\d+)</b>', html)
    assert stamp, "找不到版本戳"
    version = stamp.group(1)

    busters = re.findall(r'\?v=(w\d+)', html)
    assert len(busters) >= 3, f"应该有三个缓存参数（css/chat/app），实际 {busters}"
    bad = [b for b in busters if b != version]
    assert not bad, (
        f"版本戳是 {version}，但这些缓存参数不是：{bad}。"
        "改了前端就要把版本戳和**全部** ?v= 一起 +1，"
        "否则用户会拿到新旧混着的文件。"
    )


def test_every_local_asset_is_cache_busted():
    """本地 js/css 都要带 ?v=，否则改了不生效。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for m in re.finditer(r'(?:src|href)="((?:/)?static/(?:js|css)/[^"]+)"', html):
        assert "?v=" in m.group(1), f"{m.group(1)} 没有缓存参数，改了不会生效"


# ---------- 对话 ↔ 任务区这条链路的前端半边 ----------
#
# 这两处都是「看起来像小事、坏了就整条链路断掉」的地方：
#   1. 任务区必须能显示对话给的任务（它和知识树无关）
#   2. 「标记完成」不能回来（点一下就当作做完，没有交付物也不写回记忆）
# 注释里出现这些字是允许的，所以这里剥掉注释再判断。


def _strip_js_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"//[^\n]*", "", js)


def test_task_area_can_show_dialogue_tasks():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function dialogueTaskSection" in js, "对话任务区没了——任务区又会是空的"
    code = _strip_js_comments(js)
    # 关键：在没有知识树方向时，不能整页 return 掉对话任务
    assert "task.origin === \"dialogue\"" in code or "tk.origin === \"dialogue\"" in code, \
        "没有按 origin 分出「对话给的任务」"


def test_a_task_answers_the_three_questions():
    """做什么 / 交什么 / 怎样算做到。缺「交什么」就是用户说的
    「我在对话里出现的任务是要我怎么完成」。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    for label in ("① 做什么", "② 交什么", "③ 怎样算做到"):
        assert label in js, f"任务里少了「{label}」这一段"
    assert "task.deliverable" in js, "没有渲染交付物（deliverable）"
    assert "怎样算做到" in js


def test_task_panel_hides_rubric_behind_toggle():
    """评分标准 / 反馈默认收起，点开才展开。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function taskFolds" in js
    assert "task-fold" in js
    assert "怎样算做到" in js
    # 未完成路径不应默认把评阅摊开
    assert "renderFeedbackInto" in js


def test_matrix_copy_no_longer_asks_for_three_cards():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "再交 3" not in js
    assert "先交一张过线的阅读卡" in js
    assert "格子只来自你自己的过线卡" in js


def test_the_mark_done_button_is_gone():
    """「标记完成」只能通过提交交付物来做到，不能是一个点一下就完的按钮。"""
    js = _strip_js_comments((WEB / "js" / "chat.js").read_text(encoding="utf-8"))
    assert "标记完成" not in js, "「标记完成」按钮又回来了"
    assert '"complete"' not in js, "前端又在直接发 complete 事件（应该走任务提交）"
    assert "去做这一步" not in js, "对话不再承接任务跳转"
    assert "renderAction" not in js and "finishedNote" not in js


def test_memory_panel_renders_project_folders():
    """一个项目一张卡——用户要的「大文件夹」。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function projectFolders" in js, "项目文件夹的渲染没了"
    assert "r.projects" in js or "mem.projects" in js, "没有把 /api/memory 的 projects 用起来"


def test_chat_does_not_resume_old_task_workflows():
    """退出派任务后，回到对话也不能重新带回旧任务卡和完成通知。"""
    js = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    assert "function finishedNote" not in js
    assert "history.just_finished" not in js
    assert "history.last_action" not in js


# ---------- CSS 的机械检查 ----------
#
# 这一组全部是「跑一遍就能确定」的事，不靠眼睛看。
# 起因是一次前端审计：截图看不出来的问题（变量名写错、同一条规则写两遍、
# 删掉的功能留下的钩子）恰好都是这类。


def test_css_variables_are_all_defined():
    """用了 var(--x) 就必须有 --x: 的定义（或自带兜底）。

    没有兜底又没定义的 var() 会让整条声明在计算值阶段失效：
    `background: var(--teal-wash)` 直接变透明，`color: var(--ink-1)` 变成继承。
    **不报错、不警告**，只是颜色悄悄不对——最费时间的那一类 bug。
    实际踩过：--ink-1 / --wash / --teal-wash 三个名字，8 处引用。
    """
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    defined = set(re.findall(r"(--[a-zA-Z0-9_-]+)\s*:", css))
    used = re.findall(r"var\(\s*(--[a-zA-Z0-9_-]+)\s*(,)?", css)
    missing = sorted({name for name, fallback in used
                      if name not in defined and not fallback})
    assert not missing, (
        f"这些变量没有定义也没有兜底：{missing}。"
        "写错名字不会报错，只会让颜色静默失效。"
    )


def test_no_class_has_two_disagreeing_rule_blocks():
    """同一个类名不要在两处各写一条规则。

    同特异性、没有媒体查询时，后面那条会静默覆盖前面的。
    实际踩过：`.finished-note` 在文件中间写了 `margin-top: 16px`，
    末尾又写 `margin: 0 0 10px`——结果是 0，而且没人看得出来。
    这里只查我们确实踩过的那几个，不做通用检测（动态类名会误报）。
    """
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    for cls in ("finished-note", "deliverable", "task-origin", "proj"):
        blocks = re.findall(rf"^\.{re.escape(cls)}\s*\{{", css, re.M)
        assert len(blocks) <= 1, (
            f".{cls} 有 {len(blocks)} 条顶层规则，后面的会静默覆盖前面的"
        )


@pytest.mark.parametrize("cls", ["sr-only", "divider", "typing", "chat-hint", "chat-options"])
def test_dead_css_hooks_stay_deleted(cls):
    """删掉的功能不要在样式表里留钩子。

    重做视觉的人看到 `.typing` 会以为「AI 正在输入…」还在用，
    于是花时间去调一个根本不存在的指示器。
    """
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    assert not re.search(rf"^\.{re.escape(cls)}\s*[,{{]", css, re.M), \
        f".{cls} 又回来了（四个前端文件里都没人用它）"
    for name in ("index.html", "js/app.js", "js/chat.js"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert cls not in text.replace(f"chat-{cls}", ""), \
            f"{name} 里又出现了 {cls}"


def test_completed_task_does_not_render_two_headers():
    """已完成的任务只画一个表头。

    `taskPanel` 先无脑 append 了一个表头（带「约 N 分钟」），
    然后在 done 分支直接交给 `taskPanelDone`——而后者**不清空面板**，
    自己又 append 一个表头（带「已完成」）。
    结果是标题、简介各出现两遍，还同时挂着两个互相矛盾的标签。
    提交那条路径一直是先 `p.innerHTML = ""` 的，只有这条漏了。
    """
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    m = re.search(r'if \(task\.status === "done"\) \{(.*?)\n  \}', js, re.S)
    assert m, "找不到 task.status === 'done' 的分支"
    branch = m.group(1)
    # 必须**紧挨着**：先清空、再交给 taskPanelDone。
    # 只断言「分支里出现过 p.innerHTML = ""」是不够的——
    # 提交那条路径的清空离得很近，会落进同一个窗口里，
    # 于是把修复回滚掉测试照样绿。（这个弱点是我自己验出来的。）
    assert re.search(r'p\.innerHTML\s*=\s*""\s*;\s*taskPanelDone\(', branch), (
        "done 分支里 `p.innerHTML = \"\"` 没有紧接在 taskPanelDone 前面，"
        "会画出两个表头（标题两遍 + 「约 N 分钟」和「已完成」并存）"
    )
    # 而且这两个表头确实都在——说明不清空就真的会重复
    assert re.search(r'head\.appendChild\(el\("span", "task-meta"', js), \
        "taskPanel 里应该还有一个无条件追加的表头"


def test_chat_page_has_conversation_and_composer_without_middle_scroll_strip():
    """回执跟随回复，建议归输入区，不再形成第三个小滚动窗口。"""
    js = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    m = re.search(r"chat\.append\(([^)]*)\)", js)
    assert m, "找不到把三个区装进对话面板的那一行"
    zones = [z.strip() for z in m.group(1).split(",")]
    assert zones == ["scroll", "inputZone"]
    assert 'scroll.appendChild(actions)' in js
    assert 'suggestions.appendChild(row)' in js
    for name, cls in (("scroll", "chat-scroll"),
                      ("inputZone", "chat-input")):
        # 只查名字在不在：actions 的类名是双类 "action-slot chat-next"，
        # 用精确引号匹配会误报。
        assert cls in js, f"{name} 没有用约定的类名 {cls}"
    # 旧的单一 footer 不能回来
    assert ".chat-foot {" not in (WEB / "css" / "styles.css").read_text(encoding="utf-8"), \
        "`.chat-foot` 又回来了；三个区不该再挤回一个 footer"


def test_chat_panel_height_matches_side_panel_height():
    """对话面板和记忆侧栏用同一套高度算法，底边才对得上。

    两列高度不一致时，短的那列看起来像没加载完。
    """
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    panel = re.search(r"\.chat-panel \{(.*?)\}", css, re.S)
    side = re.search(r"\.two-col-chat > \.side-panel \{(.*?)\}", css, re.S)
    assert panel and side, "找不到对话面板或侧栏的规则"
    p_h = re.search(r"height:\s*([^;]+);", panel.group(1))
    s_h = re.search(r"max-height:\s*([^;]+);", side.group(1))
    assert p_h and s_h, "两边都要限制高度"
    assert p_h.group(1).strip() == s_h.group(1).strip(), (
        f"高度算法不一致：面板 {p_h.group(1).strip()} vs 侧栏 {s_h.group(1).strip()}"
    )


def test_new_class_names_all_have_rules():
    """本轮新起的类名必须真的在样式表里有规则。

    我刚批评过 `.chat-wide` 这种「起了名字没兑现」的空钩子，
    就不能自己再留一个（`.two-col-chat` 差点就是）。
    """
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    for cls in ("two-col-chat", "chat-next", "chat-input", "ws-head-bar",
                "ws-bar-main", "read-toolbar", "paper-tools", "ws-lead",
                "app-top", "dir-chip", "user-menu-btn", "user-menu",
                "snap-inner", "land-skip", "land-steps", "land-hint",
                "land-closing", "snap-no", "room-guide",
                "task-stem", "task-fold", "task-ask",
                "paper-table", "sec-jump", "pos-lead", "map-card", "pos-tab",
                "login-tabs", "login-tab", "login-body",
                "nav-soon", "tex", "paper-front",
                "enter-bar", "enter-card",
                "rank-row", "rank-why", "rank-acts", "my-dirs", "dir-menu",
                "today-papers", "triage-hit", "today-next", "triage-zh", "triage-kw",
                "tutor-panel", "tutor-head", "tutor-voice", "tutor-prog",
                "tutor-notice", "tutor-q", "tutor-hint", "tutor-acts",
                "tutor-hit", "tutor-alt", "tutor-miss", "tutor-launch", "tutor-minor",
                "bench-modes", "tutor-answer", "tutor-verdict", "tutor-pending",
                "tutor-ok", "tutor-bad",
                "read-grid", "read-side", "read-pane", "read-pane-head", "read-pane-body",
                "kp-zh", "kp-kw", "kp-lib", "read-search",
                "paper-zh", "paper-kw", "card-head", "nav-toggle", "nav-expand"):
        # 用 (?![\w-]) 而不是 \b：类名里允许 `-`，所以 `\b` 会把
        # `.chat-next-nonexistent` 也当成 `.chat-next` 存在——那样把规则改名
        # 测试照样绿。（这个弱点是我回滚验证时发现的。）
        assert re.search(rf"\.{re.escape(cls)}(?![\w-])", css), \
            f".{cls} 在 JS 里用了，样式表里却没有规则（空钩子）"


def test_sidebar_is_five_rooms_and_user_owns_portrait():
    """学生侧栏是今日 / 对话 / 研读 / 定位 / 项目五个房间；作业不在栏目里。
    另外还有一个默认隐藏的「监测」（w104 起）：只对管理员显示，不占学生的栏目。"""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    nav = re.search(r'<nav class="side-links".*?</nav>', html, re.S).group(0)
    views = re.findall(r'data-view="([^"]+)"', nav)
    assert views[:5] == ["today", "dialogue", "read", "position", "projects"], views
    assert views[5:] == ["monitor"], views
    mbtn = re.search(r'<button[^>]*data-view="monitor"[^>]*>', nav)
    assert mbtn and "hidden" in mbtn.group(0)  # 管理员入口默认隐藏
    assert "作业" not in nav
    assert "待开发" in nav
    assert 'id="dirChip"' in html
    assert 'id="userMenuBtn"' in html
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function openUserMenu" in js
    assert "function paintChrome" in js
    assert "八个工作区" not in js
    assert "先聊五个问题" not in js
    assert "问答画像" not in js
    assert "此刻最值得做的一件事" not in (WEB / "css" / "styles.css").read_text(encoding="utf-8")


def test_monitor_panel_does_not_feed_elements_to_el():
    """el() 的第三参数是 innerHTML 字符串：把元素传进去，页面上会渲染出
    "[object HTMLDivElement]"（监测面板第一版真出过）。元素必须用 append/appendChild 塞。"""
    code = _strip_js_comments((WEB / "js" / "app.js").read_text(encoding="utf-8"))
    assert 'el("div", "monitor-grid",' not in code, "卡片元素不能当 el 的第三参数（innerHTML）传"
    assert '", el(' not in code, "不要把 el(...) 调用当 el(...) 的参数，会被 innerHTML 变成 [object HTMLDivElement]"


def test_read_does_not_blame_kit_when_user_is_gone():
    """研读曾经把工具包和「按 uid 拉阅读卡」绑在一次请求里。
    云函数 /tmp 库一丢，旧 uid 变成 user not found，整页写成「工具包加载失败」。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function recoverUser" in js
    assert "function signedOut" in js
    assert "rg_token" in js
    assert "Authorization" in js
    # 工具包失败文案只在单独取 kit 失败时用，不能再和 cards 绑死
    assert '工具包加载失败' in js


def test_hero_swap_is_queued_and_fast():
    """首页换屏必须短于旧的 4.2s，且滚轮去抖、忙时排队。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "/ 4200)" not in js
    m = re.search(r"const SWAP_MS\s*=\s*(\d+)", js)
    assert m, "缺少 SWAP_MS"
    assert int(m.group(1)) <= 1600, m.group(1)
    assert "e.timeStamp - lastWheelT" in js
    assert "queued = Math.sign" in js


def test_projects_view_is_placeholder():
    """render() 的 projects/project 必须走占位页，不直接打开旧项目页。"""
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function renderProjectsSoon" in js
    block = re.search(r"switch \(S\.view\) \{(.*?)default:", js, re.S)
    assert block, "找不到 render() 的 switch"
    assert "renderProjectsSoon()" in block.group(1)
    assert re.search(r'case "projects":\s*case "project":\s*renderProjectsSoon\(\)', block.group(1))


def test_nav_soon_rule_exists():
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"\.nav-soon(?![\w-])", css)


def test_position_view_is_placeholder():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "function renderPositionSoon" in js
    block = re.search(r"switch \(S\.view\) \{(.*?)default:", js, re.S)
    assert block, "找不到 render() 的 switch"
    assert "renderPositionSoon()" in block.group(1)
    assert re.search(r'case "position":\s*renderPositionSoon\(\)', block.group(1))
    assert "阅读顺序" not in js
    assert "定位微调" not in js
    html = (WEB / "index.html").read_text(encoding="utf-8")
    pos = re.search(r'data-view="position".*?</button>', html, re.S)
    assert pos and "待开发" in pos.group(0)
    assert "async function renderPosition()" not in js
    assert "async function renderProjects()" not in js


def test_request_1016_frontend_copy():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    chat = (WEB / "js" / "chat.js").read_text(encoding="utf-8")
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    assert "继续对话" in js
    assert "先核对" in chat
    assert "留下／跳过的理由（4–80 字）" in js
    assert "kit-paper::before" not in css
    adopt = re.search(r"function adoptDirection\(facts\) \{.*?\n\}", js, re.S)
    assert adopt and "saveTrail" not in adopt.group(0)
    assert "记录" not in js.split("function firstView")[1][:800] or "我的" in js
    assert "refreshFacts();" in chat


def test_frontend_loss_guards():
    import subprocess
    script = WEB.parent / "tools" / "verify" / "verify_frontend_loss.js"
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "async function ensureMyDirs" in js
    assert "llm_key_invalid" in js or "llmKeyInvalid" in js


def test_privacy_export_and_sidebar_copy():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "function privacyBox" in js
    assert "consent.checked" in js or "consent.ok()" in js  # 本分支登录页用 consentRow().ok() 把关
    assert js.count("PRIVACY_HTML") >= 2
    assert "/api/me/export" in js
    assert 'id="llmPill"' in html and "我的 DeepSeek" in html
    assert 'id="llmPill"' in html and "连接模型" not in html.split('id="llmPill"')[1][:80]


def test_card_page_cta_and_tools():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "带读答题" in js
    assert "paper-lede" in js
    assert "paper-tools" in js
    assert 'el("nav", "ws-tabs bench-modes")' not in js


def test_phone_reading_pane_not_sticky():
    css = (WEB / "css" / "styles.css").read_text(encoding="utf-8")
    start = css.index("@media (max-width: 920px)")
    depth = 0
    end = start
    for i in range(css.index("{", start), len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    block = css[start:end]
    rule = re.search(r"[^{}]*\.bench-paper[^{}]*\{([^}]*)\}", block)
    assert rule and "position: static" in rule.group(1)


def test_no_empty_direction_copy():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "还没有方向" not in js
    assert "还没选方向" in js


def test_soon_rooms_hidden_and_pass_copy():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    for v in ("position", "projects"):
        btn = re.search(rf'<button[^>]*data-view="{v}"[^>]*>', html).group(0)
        assert "hidden" in btn
    assert "过线 ${passed}" not in js


def test_read_list_zh_first_and_search_placeholder():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert "kp-en" in js
    assert "搜论文（arXiv 只认英文关键词）" in js


def test_tutor_draft_key_and_resume_gate_present():
    js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
    assert 'rg_tutor_${S.uid || "anon"}_${kitId}_${aid}' in js
    assert "tutor-draft-note" in js
    assert "if (!canGrade && !hasSession) {" in js
    assert "  if (!canGrade) {\n    tabTutor.disabled = true;" not in js
