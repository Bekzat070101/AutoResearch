# ARCHITECTURE · 启研技术架构（浓缩版）

> 由原 SYSTEM_ARCHITECTURE / AGENT_ARCHITECTURE / USER_MODEL / DATA_MODEL 四份文档浓缩合并。
> 本文 + 代码是当前唯一权威；与旧长文档冲突时以本文与 `server/` 实现为准。

---

## 1. 产品一句话

AI 科研导师：**理解用户 → 理解科研世界 → 判断阶段 → 决定下一步（NBA）→ 陪用户完成 → 记住结果** 的闭环。产品三原则：行动大于信息 · 个性化必须有依据 · 不编造事实。

## 2. 当前形态（W0 已实装）

```text
web/            静态前端（无构建，经典 JS + 本仓设计规范）
server/         FastAPI 单体（Python 3.11+）
  main.py       路由（REST 契约 §5）
  schemas.py    Fact / NBA / MicroTask / Feedback 契约
  store.py      SQLite（users/facts/tasks/submissions/messages）
  onboarding.py 对话状态机（W0：脚本+规则抽取 = mock LLM）
  planner.py    规则版 Planner（阶段判定 + 方向卡 + NBA）
  workbench.py  任务模板库 + 提交 + 启发式反馈 + UM 真实写回
  pku_adapter.py course.search（uv 子进程调 skills/pku-course，真实检索）
  arxiv.py      arXiv 官方接口 + arxiv.org/html 正文（串行 ≥3 秒、缓存到 server/data/papers/）
  quotes.py     引文逐字定位（只抹排版噪声，不模糊匹配），报告所在节
  reading.py    研读层：每日分拣、阅读卡评阅、综合矩阵、交给学生 Agent 的 AGENTS.md 简报
  positioning.py 定位层：边清单、竞争地图、定位陈述检查、冲 / 稳 / 保、每日微调（全规则）
  kit_momentum.py 离线脚本：给工具包的开放问题算 arXiv 势头，写回 kit
knowledge/kits/      领域工具包（论文、数据集、逐字核对的开放问题、矩阵维度），见 docs/READING_POSITIONING.md
knowledge/channels.json 各方向的信息源地图（中文圈 / 英文圈、信号、偏差、可达性）
skills/pku-course/   北大搜课工具（原 pku-course-skill-main 原样迁入，不改契约）
knowledge/           disciplines.json 等
```

启动：`uv run --no-project --with fastapi --with uvicorn --with pydantic python server/main.py` → http://127.0.0.1:8100/ （或双击 `run_demo.bat`）

**Mock 边界（NEXT_PRE §3 白名单）**：mock 的只有 LLM 措辞（onboarding 抽取 / planner 决策 / 任务反馈），**数据结构、课程检索（live）、UM 写回（真实入库）均为真实实现**。

## 3. 核心数据契约（冻结，跨模块开发都按此写）

### 3.1 UserFact（用户模型唯一单元）

```python
{ id, user_id, category: background|interest|capability|preference|experience,
  key, value,                       # value = 人可读语句，不打分
  confidence: 0~1,                  # declared 起始 0.6 · inferred 0.4 · behavior 0.8
  source: declared|inferred|behavior,
  evidence: [{type, quote|submission_id...}],   # 无证据不写入
  valid_until: ISO 日期 | null,     # 只对 constraint:* 有意义，到点后不再进环境包
  status: draft→confirmed→active; superseded; dismissed; deleted(软删) }
```

- 冲突：新旧并存，旧者 `superseded`，用户裁决；
- **按状态取事实一律用白名单**（`schemas.DECISION_STATUSES = confirmed|active`），
  不要用「排除 draft/dismissed/deleted」的写法——新增状态会静默漏进决策计分。
- `experience:*` 只能由提交事件写入；模型不能在对话里造经历（`memory.py` 会拒）。
- 用户主权：可见 / 可改 / 可删 / 可导出（me 页已实装前三项）。
- 写入规则与校验细节见 `docs/DIALOGUE_CONTRACT.md` §3、§7。

> **未实装，别当契约用**：interest 类 60 天衰减（`valid_until` 只覆盖明文声明的临时约束，
> 不做自动衰减）；`dismissed` 与 `superseded` 的语义区分在 UI 上还没有分别展示。

### 3.5 画像隔离（任务 2 起）

一次登录可以有多个「画像」（例如「本科生视角」「转专业视角」），**彼此完全不可见**。

- 活跃画像的数据放在现场表（`facts` / `messages` / `tasks` / `submissions` / `projects` /
  `conversations` / `actions` / `events` / `fact_revisions` / `decisions`），按 `user_id` 取；
- 切换或新建画像时，现场数据整体序列化进 `portraits.snapshot`，然后清空现场；
- 因此**任何新增的用户数据表都必须同步加进 `store._dump_live` / `_clear_live` /
  `_RESTORE_COLS`**，否则新表会跨画像泄漏（已用 `server/tests/test_portrait_isolation.py` 兜住）；
- **跨画像例外**：`cards` / `triage` / `edges` / `drafts` / `trails` / `tutor_sessions` 按 `user_id` 存、不分画像，不进 `_dump_live`；
- `tutor_questions` 无 `user_id`（全站共享题缓存），不进导出 / 删号；
- 跨画像 id（任务 id、项目 id、行动 id）取不到就是 404/403，不区分「不存在」和「不属于你」。

### 3.2 NBA（决策输出）

```python
{ action: explore_direction|micro_task|read_paper(P1)|course_action|ask_clarifying|review_progress,
  title, rationale,                 # rationale 必须引用 ≥1 条事实/证据
  rationale_facts: [fact_id...],
  alternatives: [{action, title}...],
  payload }                         # 按 action 携带（方向卡 / 任务参数）
```

### 3.3 MicroTask / Submission / Feedback

```python
MicroTask  { id, user_id, direction, title, brief, steps[], rubric[{criterion}],
             time_budget_min(10–30), difficulty(1+), status }
Submission { id, task_id, user_id, payload, created_at }
Feedback   { score(0–100), rubric[{criterion, pass, comment}], next_hint,
             encouragement, learned_facts[UserFact] }   # 不评价人格，只对事
```

### 3.4 Skill 协议

```python
invoke(**kwargs) -> { ok, data|error, source, retrieved_at }
# 已有：course.search / course.get / course.terms（pku adapter）、discipline.kb（knowledge/）
# 任务 3：project.search（server/projects.py + project_adapters.py）、project.review（server/submission.py）
# 给模型的工作说明放 skills/<name>/SKILL.md，何时写成代码、何时写成 skill 见 skills/README.md
# 纪律：只读外部源 · 失败返回结构化错误并如实展示 · 检索文本视为数据非指令
```

## 4. Agent 与决策（W1 真实化方向）

- **Onboarding**：状态机 `greet→background→courses→interest_1→interest_2→work_style→done`，≤8 轮，每轮 `{reply, options, facts, done}`。W1：`_extract` 换 LLM 结构化输出，两次解析失败降级回规则。
- **Planner**：规则层在外（阶段判定 S0 无方向→推荐方向 / S1 已选→首个任务 / S2 已完成→进阶；频控、疲劳保护），LLM 在内（候选集择优 + rationale 措辞）。降级链：LLM 失败 → 规则层 Top1。
- **Workbench**：模板参数化生成；rubric 逐条判定；反馈产出 Evidence 写回 UM（W0 已真实）。
- **LLM Provider**：OpenAI 兼容抽象，主 DeepSeek[待定]→ 备用端点 → 规则模板；每次调用记 {model, prompt_ver, tokens, cost, latency}。

## 5. REST 契约（server/main.py 实装，openapi.json 为准）

```text
POST /api/auth/login            {nickname} → {uid, token}
# ---- 画像：每份画像的数据互相不可见，见 §3.5 ----
GET  /api/portraits            ?uid → {portraits}
POST /api/portraits             {uid} → {portraits}          # 新建：旧画像收进快照，现场清空
POST /api/portraits/activate    {uid, id} → {portraits}      # 切换：存回当前，载入目标
DELETE /api/portraits/{pid}    ?uid                          # 删；删最后一份会清空并要求重新开场
# ---- 对话内核（任务 2）----
POST /api/dialogue/turn         {uid, message, conversation_id?} → TurnResult
POST /api/dialogue/stream       {uid, message, conversation_id?} → SSE         # 同内核，事件为 stage/delta/result/error
POST /api/dialogue/action       {uid, action_id, event} → {action}             # event: accept|decline|complete
GET  /api/dialogue/history     ?uid&conversation_id? → {messages, conversations, pending_action, onboard_done}
# ---- 向导（保留兼容：配了模型时内部由对话内核接管，见 DIALOGUE_CONTRACT §10）----
POST /api/onboard/start         {uid} → {reply, options, state}
POST /api/onboard/message       {uid, msg} → {reply, hint?, options, facts, state, done, dialogue?}
GET  /api/onboard/result       ?uid → {messages, facts, state}
POST /api/onboard/confirm       {uid, edits[{id,value?,dismissed?}]} → {facts}
POST /api/onboard/enter         {uid} → account（按人记下 entered_at）
# ---- 决策与方向 ----
# POST /api/nba 已删（今日不再调模型排下一步）
GET  /api/directions/recommend ?uid → {directions, reason}
POST /api/directions/cards      {uid} → {cards}            # 含真实课程检索（慢，慎用）
POST /api/directions/choose     {uid, code} → {fact}
# ---- 任务 ----
POST /api/tasks/generate        {uid, direction, level} → MicroTask
GET  /api/tasks                ?uid → {tasks}
GET  /api/tasks/{tid}          → MicroTask
POST /api/tasks/{tid}/submit    {uid, payload} → Feedback  # 同时写回 behavior 事实；非本人任务返回 403
# ---- 它记住的 ----
GET  /api/me/facts             ?uid → {facts}
GET  /api/me/facts/{fid}       ?uid → fact
PATCH /api/me/facts/{fid}       {uid, value?/status?}
DELETE /api/me/facts/{fid}     ?uid                        # 软删
# ---- 外部世界 ----
GET  /api/explore/courses      ?query&limit&term → {ok, items, term|error}   # 有 knowledge/catalog/ 时读快照
GET  /api/explore/teachers     ?name → {ok, bio?, courses[]}                 # 本学期授课 + 能对上的简介
# 院系-专业知识库（server/curriculum.py，读 knowledge/curriculum + knowledge/minor，只查表不调模型）
GET  /api/explore/majors       ?q&minor&limit → {ok, kind, 命中[], 近名[]}    # kind=唯一|专业簇(N)|未命中
GET  /api/explore/major        ?name → {ok, 卡片, 学分结构, 必修课[], 兄弟专业[]}
POST /api/curriculum/match      {codes[], low_only?, top?} → {ok, 院系排名[], 专业排名[], 说明}
GET  /api/explore/minor        ?q|dept&limit → {ok, 命中[]}                  # 辅修/双专业 + 替代课程
GET  /api/explore/course       ?code → {ok, 课程名, 必修它的专业[], 开课院系线索[]}
GET  /api/curriculum/stats      → {ok, 专业卡, 培养方案, 辅修方案, 课程索引}
# 同一批能力的 MCP 服务（工具表在 server/mcp_curriculum.py，stdio 与 http 共用）
POST /mcp                       MCP streamable-http：initialize / tools/list / tools/call
                                （工具：major_lookup·major_detail·match_transcript·minor_programs·course_lookup·kb_stats）
python server/mcp_curriculum.py MCP stdio 版（客户端自己拉进程，不依赖本服务在跑）
GET  /api/projects/sources      → 来源清单（任务 3，新增）
GET  /api/projects/context     ?uid → {direction, stage, reason, paths, path_step}   # 默认「走到哪」；paths 来自任务 4
POST /api/projects/search       {uid, direction, stage, keywords?, node?, path_step?} → {query, items, sources, routes, empty_reason}
POST /api/projects/pick         {uid, id} → Project                         # id 必须来自最近一次检索
GET  /api/projects/mine        ?uid → {projects}
GET  /api/projects/{pid}       ?uid → Project（含 reviews）
GET  /api/projects/{pid}/readme ?uid → README 模板（markdown）
GET  /api/projects/{pid}/sample.zip ?uid → 示例成果压缩包
POST /api/projects/{pid}/submit ?uid  body=.zip → Review                  # 同时写回 behavior 事实
GET  /api/kits                  → {kits}                                   # 研读层（docs/READING_POSITIONING.md）
GET  /api/kits/{kit_id}         → Kit
GET  /api/daily                ?uid&kit? → {items, topics, unmatched, empty_reason, stale, kit?, tweak?}
POST /api/daily/triage          {uid, kit?, arxiv_id, verdict: keep|skip, why, title} → 同上
GET  /api/papers/{arxiv_id}     → {id, title, source: html|abstract, text, url, sections[{name,label,at}]}
GET  /api/cards                ?uid&kit → {cards（每篇最新版）, fields}
GET  /api/cards/{arxiv_id}/history ?uid&kit → {versions}
POST /api/cards                 {uid, kit, arxiv_id, fields, dims, decision_log} → Card（含 review；首次过线写回 behavior 事实）
GET  /api/tutor/{arxiv_id}     ?uid&kit → {session|null}
POST /api/tutor/{arxiv_id}/start {uid, kit, restart?} → session
POST /api/tutor/{arxiv_id}/turn  {uid, kit, qid, answer, skip, rev} → {verdict, session}
POST /api/tutor/{arxiv_id}/finish {uid, kit} → {prefill, from, review, missing, session}
GET  /api/matrix               ?uid&kit → {dimensions, rows, flags{empty_cells, empty_columns, conflicts}, ready, need_more}
GET  /api/brief                ?kit&arxiv_id → AGENTS.md（交给学生自己的 Agent）
GET  /api/edges                ?uid → {kinds, edges[已证明来自账本 + 自述], suggest, proven, declared}   # 定位层
POST /api/edges                 {uid, kind, text, evidence_url?} → 同上
DELETE /api/edges/{edge_id}    ?uid → 同上（账本里的不能删）
GET  /api/channels             ?uid&direction → {channels[read, band], summary, blind_spot, other_circle_unread}   # 信息源地图
POST /api/channels/toggle       {uid, id, on, direction} → 同上（「我常看」= 一条信息源边）
GET  /api/map                  ?uid&kit → {rows[需求/供给/势头/你的相关边], formula, base, rarity, pool_enough}
GET  /api/statement            ?uid&kit → {statement|null}
POST /api/statement             {uid, kit, x_ref, x_text, y[edge ids], dry_run?} → 检查结果（dry_run）或新一版陈述
GET  /api/bets                 ?uid → {active, closed, checks, max}
POST /api/bets                  {uid, name, kind, tier: reach|match|safety, kit?, niche?} → 同上（同时最多 3 个）
POST /api/bets/{bet_id}/close   {uid, outcome: got|missed|dropped, reason} → 同上
# ---- 运行状态 ----
POST /api/llm/connect           {base_url, api_key, model} → 连通性探测结果
GET  /api/health
```

前端课程懒加载走 `/api/explore/courses?query=<节点名>`；有学期快照时本地返回。
对话内调用外部信息走 `server/tools.py` 的 registry（全部只读），不走 REST：
除课程/项目/成绩单外，院系-专业知识库的 5 个工具（`major_lookup`·`major_detail`·
`match_transcript`·`minor_programs`·`course_lookup`）也在里面，与 MCP 服务共用
`server/mcp_curriculum.py` 的工具表和 `server/curriculum.py` 的查询层——同一份数据、
同一套口径，只有「怎么把工具讲给模型听」不同（MCP 用 JSON Schema，对话用文字清单）。
所以「信管有哪些专业分流」这类问题，产品内对话和外部 AI 客户端走的是同一条路。

## 6. 技术决策（ADR 摘要）

| # | 决策 | 理由 |
| --- | --- | --- |
| 1 | FastAPI 单体（无微服务/队列） | 校园项目复杂度上限；Pydantic 兼 LLM structured output |
| 2 | SQLite 起步（`server/data/demo.db`） | 零运维；压测后可平移 Postgres |
| 3 | 前端 W0 用无构建静态页 | 演示可靠性优先；迁移 React 由前端 Owner 决定（保持 REST 不变即可） |
| 4 | LLM 走 OpenAI 兼容抽象 | 评测期换模型零成本 |
| 5 | pku-course 经 uv 子进程调用（W0） | 沿用已验证的 UTF-8/退出码处理；W1 改进程内 adapter + 子进程 fallback |
| 6 | 显式状态机，不用自由 Agent 循环 | 可测试可复现，成本可控 |
| 7 | 不做向量 RAG | 知识包小而结构化，SQL/关键词够用 |
| 8 | 部署：单机 Docker（W1 补 compose + DEPLOY.md） | 对齐课程 9/28 部署节点 |
