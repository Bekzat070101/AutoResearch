# 03 · 按兴趣检索每日论文 + 今日改成每日新论文推荐

- 需求原文：「6. 同步优化研读的搜索算法，按照用户兴趣检索」「7. 今日变为：主要负责推荐每日新论文，并且按照用户兴趣来做」。
- 目标状态：每日候选论文不再只按 llm-eval 工具包的分类×关键词取，而是按学生的兴趣话题取（纯规则，不调模型）；今日页主体改成这份推荐（≤6 篇、留/过分拣、每天目标 3 篇），原来的 NBA 主卡降为底部一行规则文案；非 arXiv 方向如实给空状态并指向信息源。
- 事实澄清：每日分拣现在就在今日页（`dailyBlock` 只被 renderToday 调，web/js/app.js:4183-4190、4631-4700），研读页没有每日区，不存在「迁移」；要改的是显示门槛（4185 只给 AI 方向或开过研读的人）和候选来源。
- 和 02 的对接：记忆的读法不变（`memory.active_facts`）；「没聊完」的判定用 02 的 `S.entered`/`onboard_done`。
- 基线：分支 `fix/review-r1` @ 0825020，971 条测试全绿；行号以这个提交为准。

## 决策（已定，不要另起方案）

- 「研读的搜索」= 每日候选检索（server/reading.py:54-67 → server/arxiv.py:142-147）；页面没有搜索框，本轮也不加。
- 中文兴趣→arXiv 英文检索词只走策展词表 `knowledge/arxiv_topics.json`（新增，结构见下）；不调模型翻译；记忆里的原话、`goal:`、`career:`、`style:`、`direction:` 的值、开场抽象标签（server/onboarding.py:92-101）一律不进查询。原因：`abs:机器学习` 命中 0；`direction:` 的值含学生原话（server/planner.py:354）；关键词原样拼进引号可改写查询（探针：`foo" OR cat:*`）。
- 每个话题自带 `categories × en`，各查各的，候选并集后本地排序；不做「分类并集 × 关键词并集」的大查询（会串味：心理学生被 cs.CL 评测论文占满）。
- 缓存键 = 话题 id（几十个，与人数无关），不是「(分类集合, 关键词集合)」（键随人数线性涨，200 人时 arxiv 池 32 个名额直接 503）。
- 候选池落 SQLite 新表 `paper_pool(key, day, data, fetched_at)`，按 arXiv 日失效（`arxiv_day = (utcnow − 5h).date()`，API 在美东午夜更新），保留最近 3 天；进程内 TTLCache 只做前置。
- 每日取数用独立的 `AsyncFlight(workpool.BoundedPool(1, 16, "daily"), max_pending=16)`，不再和论文全文共用 `_FLIGHTS["arxiv"]`（4 线程、32 名额）；每次请求最多现取 1 个冷话题，其余先给旧池并标 `stale`。
- 每人话题上限：≤3 个兴趣话题 + 1 个方向话题 + 1 个工具包；每个话题 ≤4 个分类、≤6 个英文短语；`max_results` 25→50（跨若干天，窄话题一天可能 0 篇）。
- 话题来源顺序：兴趣/领域事实（`interest:*`、`field:*` 的值和 slug 按别名匹配）→ 方向树精确节点（trail 里的 `dir`，必须在白名单）→ `direction:<code>` 经 legacy 映射 → `prefs.kit_id`；都没有就不出论文，不拿 llm-eval 填版面。
- arXiv 稀薄方向（心理、基础医学、经济等）：`empty_reason="thin"` + 信息源出口（knowledge/channels.json 已有 psyarxiv:935-952、nber:1200 起），不回落到 cs.* 论文；接 PsyArXiv/NBER 另立任务。
- 分拣记录：兴趣模式新记录写 `kit_id="*daily"`（`*` 前缀同 budget.GUESTS="*guest" 先例）；已分拣、今日计数、留下的论文都跨全部 kit 读。不能用 `"*today"` 当 kit：`daily/triage/daily_tweak` 都先 `reading.kit()`，直接 400（探针已复现）。
- 「读成一张卡」只给命中某个 kit 话题的论文（`card_kit` 非空）；其余只给「在 arXiv 读」。原因：阅读卡绑在 kit 上，数学/心理论文会被要求填 ≥5 个评测维度（server/reading.py:142-144），交上还进 llm-eval 的矩阵和竞争地图人群。
- 今日主卡：有话题就是论文区；没聊完/没方向且没话题时主卡仍是原来的「先聊聊 / 去核对 / 去选方向」规则链（app.js:4074-4116），不出论文。
- 删 `/api/nba` 与 `planner.next_best_action/_speak`（全仓只有今日页一处调用、没有测试碰它）；`NBAReq` 类和 `direction_cards` 保留（onboard/start、directions/* 还在用）。每次打开今日省 1 次模型调用，S0 省 2 次。
- 排序纯规则：标题命中 ×2 + 摘要命中，按词边界、缩写大小写敏感（`ml` 不命中 html/xml，`rl` 不命中 world）；已分拣的是过滤不是排序；话题轮流取，防一个话题占满 6 篇；同分按 published 新、arxiv_id 定序。
- 查询仍只查 `abs:`（server/arxiv.py 的 docstring 写「标题或摘要」不准，本轮不改成 `ti: OR abs:`，避免缓存全冷）。
- 词表由开发先写（53 个节点各 ≥1 个话题），文件标 `status: 草稿`，再按 kit 的 `reviewed_by` 规则请学长学姐核对。
- 研读页加一行「这个工具包的新论文在今日」链接，不放分拣本体。

## 现状要点（file:line）

- 取数：`daily_source` 固定 `max_results=25`，查询 `(cat:A OR cat:B) AND (abs:"k1" OR abs:k2)`：reading.py:54-67、arxiv.py:142-147；元数据缓存是进程内 `TTLCache(6h, 512)`、键是完整 URL、实例回收即丢（arxiv.py:31-34、100-120）；落盘的只有全文 server/data/papers/（arxiv.py:30、243-266）。
- 限速：全局 `_LOCK` 串行、间隔 ≥3 秒、单次 20 秒（全文 30 秒）；每日候选与 `/api/papers` 全文共用这把锁（arxiv.py:58-75、350）。singleflight 两层：arxiv 内 `("query", url)`；main 层 `("daily", kit)` 走 `_FLIGHTS["arxiv"]`（main.py:1211-1223）；失败共享 `_DAILY_MISS` 30 秒按 kit（main.py:1248-1271）。
- 分拣：`daily` 每人取未分拣的前 6 篇、seen 只看同一 kit（reading.py:70-82）；`triage` 理由 4–80 字、先校验 kit（85-95）；表主键 `(user_id, kit_id, arxiv_id)`（store.py:116-125、1308-1319）；只有 reading.py 读 triage，矩阵/竞争地图/combo_rarity 读的是 cards（reading.py:199-229、positioning.py:135-139、244-276）。
- `_daily_payload` 每次算一次 `positioning.daily_tweak(uid, kit)`，内部 `reading.kit()`（main.py:1285-1288、positioning.py:404-423）。
- 记忆：兴趣层 key `goal:/interest:/field:`（multi）、`direction:`（exclusive）、`career:/style:`（memory.py:121-133）；值规定抄原话（dialogue.py:252、memory.py:435-440）；key slug 由模型起（`interest:ml/nlp/rl/robotics`，也可能是中文）；`canon_key` 不做同义归并（memory.py:24-46）。
- 方向：方向事实 key 只有 7 个后端码 ai/math/stat/psy/econ/se/med（planner.py:28-98）；树代码（如 520.30）只在前端 web/js/directions.js:11-80（53 个节点），节点→后端码 `backend`（138-144，未写的回落 "ai"：688），后端码→默认节点 `legacyDir`（146-148，se→520.40，所以 520.30 系统结构的学生按 legacy 会落到软件）；学生点的精确节点存 trail（前端 `PUT /api/projects/{portraitId|default}/trail` app.js:2694-2716，服务端 store.py:630-649，data 未校验）；后端没有树、没有英文名、没有中英词表（projects.py:48-55 的 `DIR_TERMS` 只有中文）。
- 今日页：renderToday app.js:4041-4191，取 `/api/tasks`、`/api/onboard/result`、`/api/projects/mine`（4049-4053），规则链出主卡（4074-4116），loopStrip（4118-4136），`.nba-card`（4137-4152），`/api/nba` 只在 `nbaOk` 时调来改写文案（4157-4182），`NBA_ACTION_VIEW`（4001-4008）；`/api/nba` 的模型成本 S1/S2 一次、S0 两次（planner.py:174、234、314-338）。
- 分拣行：摘要按 `/(?<=\.)\s/` 切前两句（4649，遇 "e.g. " 会切错）；空状态一律「周末 arXiv 不更新」（4646）；显示门槛 `S.kitId || saved.code === "ai"` 且顺手 `useKit(DEFAULT_KIT)` PUT prefs（4185-4186、4214-4218）。
- 阅读卡：renderCard 固定用 `S.kitId || DEFAULT_KIT`，返回链接写死「← 研读」（app.js:4408-4426）；交卡写 `card:{kit}:{aid}` 证据并进 kit 的矩阵与竞争地图人群（reading.py:180-189）。
- arXiv 官方：新论文在处理完后的美东午夜进 API；同一查询一天调一次；建议间隔 3 秒；无查询长度硬上限（6 分类 × 12 短语约 605 字符）。

## 改动清单

### knowledge/arxiv_topics.json（新增）

- `topics: {id: {name, zh: [中文别名], slugs: [key slug 别名], en: [英文短语 ≤6], categories: [≤4]}}`；`nodes: {"520.30": ["cs-arch"], …}` 覆盖 directions.js 全部 53 个节点（可按父节点继承）；`legacy: {ai: "520.20", se: "520.40", …}`（directions.js:146-148 的服务端副本）；`thin: ["190.25", "190.30", "190.42", …]`；`status/checked_at/reviewed_by`。
- 把 llm-eval 收进来当一个话题（分类/关键词沿用 kit.daily），kit 模式下 `recent()` 入参与改前逐字相同。
- 英文短语只含 `[A-Za-z0-9 .-]`；单个英文单词的短语必须是缩写或在白名单里（"learning"、"model" 这类宽词会吞掉其它话题）。

### server/interests.py（新增）

- `user_topics(uid) -> (topic_ids, unmatched_terms)`：`memory.active_facts` 里 `interest:*`/`field:*` 的值与 slug 按别名匹配；方向取 `store.get_trail(uid, store.active_portrait_id(uid) or "default")["dir"]`，必须在 `nodes` 白名单里，否则用 `direction:<code>` 的 key 经 `legacy`；再加 `store.get_prefs(uid)["kit_id"]`；按上限截断。
- 排除：`goal:`、`career:`、`style:`、`pace`、`interest:unknown`、onboarding.py:92-100 的抽象标签；`direction:` 只用 key 不用值。
- 没认出的词只记词频（不记 uid）供补表；原话不出服务器、不进 URL。

### server/arxiv.py

- `recent()` 入口清洗：分类必须匹配 `^[a-z\-]+(\.[A-Za-z\-]+)?$`，关键词只允许 `[A-Za-z0-9 .\-]`，不合格的丢弃（142-147）。

### server/reading.py

- 新 `topic_source(topic_id, cached_only)`：TTLCache 前置 + `paper_pool` 落盘，键 `(topic_id, arxiv_day)`，取不到回旧池并标 `stale`。
- 新 `daily_for_user(uid)`：各话题池并集去重 → 去掉跨 kit 的已分拣 → 打分 → 话题轮流取 → ≤6 篇；每篇回 `hits: [{term_zh, term_en}]`、`topic`、`card_kit`；回包加 `topics`、`unmatched`、`empty_reason`（`no_topics | thin | exhausted | upstream`）、`stale`。
- `daily()/triage()` 的 kit 允许为空：空 = 兴趣模式、写 `kit_id="*daily"`；seen/done_today/kept_total/recent_keeps 改读全部 kit（70-95）；`daily(uid, kit, source)` 位置参数签名保持兼容（test_model_output_review.py:118-124 调它）。
- `card_kit_for(paper)`：论文分类与某 kit 话题分类相交且命中该 kit 关键词才返回 kit_id，否则 None。

### server/store.py

- 新 `list_triage_all(uid)`（1316-1319 旁）。
- 新表 `paper_pool(key TEXT, day TEXT, data TEXT, fetched_at TEXT, PRIMARY KEY(key, day))`，不带 user_id（不进删号/导出，`_user_tables` 按列识别：816-819）；写入时删 3 天前的行。

### server/main.py

- `GET /api/daily` 的 `kit` 改 `kit: str = ""`，空走 `daily_for_user`（1291-1295）；`TriageReq.kit` 默认 `""`（239-246）；POST triage 在 SERIAL 锁内只读缓存池，不打 arXiv（1298-1302，否则同一人 409）。
- 新 `_FLIGHTS["daily"] = AsyncFlight(workpool.BoundedPool(1, 16, "daily"), max_pending=16)`，每日取数指过去（1211-1216）；`_DAILY_MISS` 改按话题键（1248-1271）；每次请求最多现取 1 个冷话题（1256-1260）。
- `_daily_payload` 的 tweak 只在 prefs 有 kit 时调 `positioning.daily_tweak(uid, prefs_kit, kept_today)`（1285-1288）。
- 删 `/api/nba`（693-697）；planner.py 删 `next_best_action`、`_speak`（221-266、314-338）；`NBAReq`（177、335、709）保留。

### web/js/app.js

- renderToday：主体 = 论文区；底部一行「继续：…」只用规则链结果（4074-4116）；删 `/api/nba` 调用（4157-4182）与 `NBA_ACTION_VIEW`（4001-4008）；去掉 4185 的门槛与 `useKit(DEFAULT_KIT)` 副作用；没话题时主卡沿用「先聊聊 / 去核对 / 去选方向」。
- dailyBlock（4631-4700）：`GET /api/daily?uid=…`（prefs 有 kit 才带）；POST triage 不带 kit；命中词高亮用文本节点切分，不在 innerHTML 上做替换；空状态按 `empty_reason` 分三种文案（「这个话题最近的都分拣完了」「这个方向 arXiv 上很少，去看信息源」「连不上 arXiv，这是 X 日的候选」），不再统一写「周末 arXiv 不更新」（4646）；文案写「最近的新论文」不写「今天的」；摘要切句避开 "e.g./i.e./et al."（4649）。
- 「读成一张卡」只在 `card_kit` 非空时出现：点了设 `S.kitId = card_kit; S.cardFrom = "today"` 再 `openCard`；renderCard 的返回链接按 `S.cardFrom` 回今日（4414-4416）。
- renderRead（4220-4307）加一行「这个工具包的新论文在今日」→ `setView("today")`。
- 新类名（`.today-papers`、`.triage-hit`、`.today-next` 等）都要有 CSS 规则；`.nba-card` 不再用则连 styles.css:885-891 一起删。

### web/index.html

- 版本戳与三个 `?v=` 一起 +1。

### 文档

- ARCHITECTURE.md:170-171（/api/daily 参数与回包）、READING_POSITIONING.md:15、40（候选来源）、DESIGN_SPEC.md:31、67（今日唯一重点改为论文区）、DESIGN_PROPOSAL.md:259（「模型重排」改为 v1 纯规则）、CHANGELOG 登记。

## 不做（越界先问）

- 不调模型翻译兴趣词、不做模型重排（DESIGN_PROPOSAL §5.3 的「模型重排 + 一句理由」留到以后，且只在规则选出的 6 篇内做）。
- 不接 PsyArXiv/NBER/bioRxiv（另立任务）。
- 不做「无维度通用阅读卡」；非 kit 论文只给 arXiv 链接。
- 不记曝光（看过没分拣的论文第二天可再出现，按新旧排序自然靠后）。
- 不改查询为 `ti: OR abs:`；不改 `done_today` 的 UTC 日口径（早上 8–12 点会出现「计数清零但候选还是昨天那批」，文案已回避）。
- 不加页面搜索框。

## 测试

- 不改但必须仍绿：test_perf.py:399-425 `test_daily_failure_is_shared_not_retried_per_request`（`arxiv.recent` 仍是唯一上游入口、kit 键不变）；test_reading.py:129-137；test_model_output_review.py:118-124；test_positioning.py:112-121；test_public_load.py:113-129（每日取数移到 `_FLIGHTS["daily"]` 后不受影响）；test_web_assets.py:324-339（五栏不变，以 01 的结论为准）；test_auth.py 的全量路由扫描会自动覆盖新参数。
- 新增：`test_arxiv_topics_cover_every_tree_node`（解析 directions.js 的 `n("…")` 节点与 legacyDir，断言全在词表里且一致、分类在 arXiv 白名单、短语只含合法字符、单词短语在白名单）；`test_interest_terms_never_reach_arxiv_raw`（中文值、带引号的值不进 URL）；`test_goal_and_direction_values_are_not_keywords`；`test_trail_dir_outside_whitelist_is_ignored`；`test_520_30_maps_to_arch_not_se`；`test_daily_pool_once_per_arxiv_day`（冻结时间，同日第二次不调 `recent`，换天调一次）；`test_cold_topics_fetch_at_most_one_per_request`（3 个冷话题只触发 1 次，回包 `stale`）；`test_seen_across_kits_and_counts_merge`（先在 llm-eval 分拣一篇，兴趣模式不再出现、done_today 合计）；`test_topics_round_robin_and_word_boundary`（`html` 不命中 `ml`）；`test_card_button_only_for_kit_papers`（math.PR 论文 `card_kit` 为 None）；`test_daily_pool_keeps_three_days`；`test_daily_pool_full_does_not_block_papers`（60 个不同话题键并发时 `/api/papers` 不 503）；兴趣模式同话题 40 并发只 1 次上游。
- 契约：`GET /api/daily` 回包新增 `topics/unmatched/empty_reason/stale`，`items[]` 新增 `hits/topic/card_kit`，兴趣模式下 `kit` 为 null；`POST /api/daily/triage` 的 kit 可省。

## 验收

- 四种账号手测：新用户（没聊完）主卡「先聊聊」、无论文；AI 方向（兴趣「大模型评测」）出 ≤6 篇且命中词高亮、有「读成一张卡」；系统结构 520.30 学生拿到 cs.NI/cs.DC 的论文而不是软件工程；心理方向显示「arXiv 上很少」+ 信息源链接，不出 cs.* 论文。
- Network 里今日页不再有 `/api/nba`；每日取数的上游请求数 ≤ 在用话题数/天（看服务日志的 arxiv 行）。
- 留/过后计数合计跨 kit；刷新后已分拣的不再出现；断网时显示「这是 X 日的候选」。
- 冷启动预热：上线前跑一遍离线脚本把全部话题取进 `paper_pool`（像 kit_momentum.py 那样），否则第一天开卡全文要排在 T×≈4 秒后面。
- 971 条 + 新增全绿；`node --check`；版本戳一致。

## 风险

- arXiv 全站一把锁（arxiv.py:32、60）：即使分了线程池，全冷当天开卡全文仍会多等几秒到几十秒；预热脚本是硬要求。
- 词表写宽了会让一个话题吞掉其它话题；测试里限制单词短语。
- 窄话题常为空；按话题取最近 50 篇跨天解决大部分，剩下的靠 `exhausted` 文案。
- triage 不分画像、兴趣分画像：切画像后已分拣的仍过滤，属预期。
- 高亮若在 innerHTML 上做替换会切坏实体或引入 XSS；必须文本节点。
