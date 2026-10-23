# 启研 · AI Research Mentor

> 它认识你，它懂科研世界，它决定你此刻最值得做的一件事，陪你做完，记住结果，再给下一步。

面向大一大二、不知道自己适不适合科研、该研究什么、从哪开始的学生。区别于「又一个 AI 问答工具」的是一个闭环：

```text
理解用户 → 理解科研世界 → 判断阶段 → 决定下一步(NBA)
    ↑                                      ↓
更新认知  ←  记录行为与结果  ←  陪用户完成行动
```

产品三原则：**行动大于信息 · 个性化必须有依据 · 不编造事实**。

## 当前状态

**Week2 进行中。** 侧栏「对话」已换成自然聊天内核（任务 2）：一轮最多两次模型调用，
先观察环境、再由模型决策、需要时调只读工具、然后把结论落到记忆和下一步。
契约见 [docs/DIALOGUE_CONTRACT.md](docs/DIALOGUE_CONTRACT.md)。

> ⚠️ 下面「W0 Thin Slice」一节描述的是 Week1 的 9/28 演示版，**部分已过期**
> （①–⑨ 链路已被「工作区」改版取代，原「画像」向导降级为对话页里的「问卷」标签页）。
> 开工前先看 [docs/README.md](docs/README.md) 的「文档状态」一节。

### 对话内核（任务 2）

- **模型决策、代码裁决**：模型出结构化提案（观察 / 记忆写入意图 / 工具意图 / 回复 / 下一步），
  代码负责校验能不能做。记忆写入走白名单 + 引文逐字校验，说不出来源的不写。
- **画像完全隔离**：一份账号可以有多份画像，对话、任务、项目、行动互不可见。
- **不编造**：外部检索失败就如实说失败，也不拿「我知道」顶上；每个回答里关于你的事实都能追到你的原话。
- **降级**：没配 key 时对话仍可用（规则版），会明确告诉你这一轮是规则回复。

离线测试（**417 个**）：`python tools/verify/run_existing_tests.py`
（Windows 上不要直接 `pytest server/tests`——会因环境变量长度上限报 2 个 `ValueError`，
那是既有问题，与功能无关；这个入口会跳过那两个文件）。
真实模型验收：先起服务，再 `python eval/run_eval.py`（多轮用例见 `eval/dialogue_cases.json`）。

### 今日与研读的画像联动（w112）

账号拥有多份独立画像；对话校验后保存的事实、课程、阅读和任务反馈属于当前画像。
同一画像可以保留多个兴趣和收藏方向，每次选择一个当前方向；右上角切换后，今日与研读
结合同一份背景重新选文，各方向教程进度保留。选方向不会另建画像，也不代表已掌握该领域。

推荐先读取当前画像的有效记录，再由模型规划学术检索词、在真实候选中筛选排序；代码核对
论文ID、画像依据和标题/摘要引文。公开摘要和检索元数据可以缓存共享，用户阅读经历不共享。
检索使用 DataCite 登记的 arXiv 元数据，原有 arXiv 搜索与取文也保留备用元数据路径；
需正常的 HTTPS 出站连接与证书验证，无需新增检索密钥。搜索超时可以使用已有公开摘要候选，
明确显示降级状态；没有全文时仅作摘要练习，不把摘要练习或「留」计为已读完成。

今日仍按原教程进度选择节点，题目在该节点目标内参考用户基础、困难与目标调整。
任务提交与评分进入后续学习输入，但不等于掌握。今日候选可直接打开现有研读页。
离线回归可运行 `python tools/verify/run_existing_tests.py server/tests/test_learning_personalization.py server/tests/test_recommendations.py server/tests/test_tutor.py`。

界面 w113 将今日的两条路线分开说明：论文筛选（留／过）→ 打开研读 → 带读与阅读卡；
方向基础练习则是可选的教程路线，不是推荐论文的配套题，也不是阅读的前置关卡。
练习页显示方向／节点来源并提供返回今日，右下角双气泡 SVG「启研助手」可随时补充背景或讨论阅读。

### W0 Thin Slice（9/28 演示版，部分过期）

①–⑨ 演示链路已全部实装：昵称登录 → 初始对话（5 轮，选项卡+「不知道」）→ 初始认知确认（可改可删）→ 3 张方向推荐卡（引用你的原话 + **北大真实课程实时检索** + 入门读物）→ 20 分钟微任务 → 提交 → rubric 逐条反馈 → **行为证据真实写回用户模型** → 进阶 NBA。

LLM 环节（对话抽取 / 决策措辞 / 任务反馈）**已接入真实模型**（`server/llm.py`，OpenAI 兼容，未配置 key 时回退规则版），数据结构与闭环全程真实。

## 快速开始

```bash
# 需 Python 3.11+ 与 uv（搜课功能）；前端无构建步骤
# 对话 / 下一步 / 方向理由 / 任务反馈要接模型时：
# 复制 .env.example 为 .env 并填 LLM_API_KEY，或在网页首页点「连接模型 API」。
run_demo.bat                # Windows 双击即跑
# 或：
uv run --no-project --with fastapi --with uvicorn --with pydantic python server/main.py
# 打开 http://127.0.0.1:8100/
```

## 部署（发版前必读；给人，也给 Codex / Grok 这类 agent）

**现网 2026-10-20 的 `/api/health` 仍是 `db.ephemeral: true`、`account_store.enabled: false`：库在临时盘上，每次发版或实例回收都会清掉所有账号和记录。这不是代码 bug，是函数上没配持久盘。** 下面四条是硬规矩，缺一条就会再丢一次人。

1. **生产库必须在 NAS**：函数 `qiyan` 配 VPC + NAS 挂到 `/mnt/nas`，环境变量 `QIYAN_DB=/mnt/nas/qiyan.db`。不设 `QIYAN_DB`（库落在代码包里）或设成 `/tmp/...` 都等于「每个新版本从零开始」。从本版起服务端会守门：函数计算上库不在持久盘就不建库、`/api/*` 回 503 `db_not_ready`，`/api/health` 的 `db.blocked` 写明原因；放行只靠 `QIYAN_ALLOW_EPHEMERAL=1`，只给 `qiyan-test` 用。
2. **只用 `tools/deploy/deploy_fc.py` 发版**（命令见 [docs/DEPLOY.md](docs/DEPLOY.md)「发版」）。它会保留函数上已有的环境变量，库不在持久盘时会拒绝发生产；不要在控制台「部署代码」，不要给生产加 `--allow-ephemeral`。
3. **换库路径必须迁移**：`deploy_fc.py --db <新路径> --migrate-from-backup <本地备份文件>`，脚本会备份 → 发版 → 恢复 → 核对用户数；远端有用户而不带这个参数时脚本拒绝。10-18 那次丢人就是换了路径没迁移。
4. **从 `main` 发版**，发完对照 `/api/health` 的 `checks.build` 和 `git log -1 origin/main`；现网 `4db39dd w102` 不在 `main` 上，所以 `main` 里已合并的登录可靠性修复（#21）现网并没有跑。

发版后的验收（两条都要过，第二条发两次版再看）：

```bash
curl -fsS $URL/api/health | python3 -c "import json,sys; d=json.load(sys.stdin)['db']; assert d['ephemeral'] is False and d['writable'] is True, d"
curl -fsS -H "X-Admin-Token: $ADMIN_TOKEN" $URL/api/admin/stats     # 用户数不少于发版前
```

函数上的环境变量（发版脚本不会删已有的，只覆盖自己管的几项）：

| 变量 | 必须？ | 作用 |
| --- | --- | --- |
| `QIYAN_DB` | 必须 | SQLite 路径，必须在 NAS |
| `QIYAN_SECRET` | 必须 | 封装学生各自的 DeepSeek 密钥；换它要先 `POST /api/admin/rewrap` |
| `ADMIN_TOKEN` | 必须（≥32 字节） | `/api/admin/*`、发版后预热论文 |
| `LLM_API_KEY`（`LLM_MODEL`、`LLM_BASE_URL`） | 必须 | 站点兜底模型 |
| `QIYAN_ACCOUNT_STORE` 或 `BACKUP_OSS_BUCKET`+`BACKUP_KEY` | 强烈建议 | 账号副本：库被清空时灌回用户名和密码哈希（只是兜底，不是迁移） |
| `TRUST_PROXY=1` | 建议 | 在网关后面才能按来源限频 |
| `METRICS_TOKEN` | 可选 | 本机 Prometheus 抓生产 `/metrics`（#23 合并后） |
| `AGENT_TOKENS`、`QIYAN_SELF_URL`、`LLM_DAILY_AGENT` | 可选 | 让别人的 AI agent 当学生跑统计（#24 合并后；不设 = 关闭） |
| `WECHAT_*` | 可选 | 公众号登录，见 DEPLOY.md |
| `QIYAN_ALLOW_EPHEMERAL=1` | 只给 `qiyan-test` / 试运行 | 放行服务端守门；`deploy_fc.py` 给测试函数和 `--allow-ephemeral` 自动带上，正常发生产时自动去掉，生产函数上不该有 |

前端：`python tools/deploy/publish_gh_pages.py`，和接口一起发（旧前端打新接口会 401）。回滚、恢复、事故手册都在 [docs/DEPLOY.md](docs/DEPLOY.md)。

## 边学边练（任务 3）

侧栏「项目」：选方向和「走到哪」，从公开来源实时检索可以做的练手项目（每条带来源链接和检索时间，查不到就空着）；选一个，按要求打成 `.zip` 交上来，按五条标准逐条评阅并写回画像。说明见 [docs/TASK3_PROJECTS.md](docs/TASK3_PROJECTS.md)；离线测试：`uv run --no-project --with pytest --with fastapi --with pydantic --with httpx pytest server/tests`（不联网、不调模型；PR 上由 GitHub Actions 自动跑，见仓库根目录 `.github/workflows/tests.yml`）。

## 目录结构

```text
web/                前端（无构建静态页；设计规范 docs/DESIGN_SPEC.md）
server/             FastAPI 后端（路由/契约/状态机/Planner/Workbench/搜课适配）
  dialogue.py       对话内核：环境包 → 决策 → 工具 → 回复（任务 2）
  memory.py         记忆治理：写入白名单 + 确定性校验 + 召回（任务 2）
  tools.py          只读工具 registry（任务 2）
skills/pku-course/  北大搜课工具（uv 子进程调用，契约不改）
skills/project-*/   给模型的工作说明（SKILL.md）；代码与 skill 的分工见 skills/README.md
knowledge/          学科知识包（disciplines.json）、项目来源清单（project_sources.json）、
                    院系-专业培养方案库（curriculum/ 199 张认知卡、minor/ 98 条辅修双专业）
eval/               真实模型评测集与跑分器（任务 2）
docs/               ARCHITECTURE / DIALOGUE_CONTRACT / DESIGN_SPEC / CHANGELOG / DEPARTMENT_KNOWLEDGE /
                    TASK2_DIALOGUE / TASK3_PROJECTS / paths（文档状态见 docs/README.md）
```

## 院系-专业知识库（含 MCP）

`knowledge/curriculum/` + `knowledge/minor/` 是用脚本从《北大本科培养方案（2026）》文理两卷和
《辅修双专业培养方案（2025）》抽出来的结构化数据：199 张专业认知卡、198 份培养方案、98 条辅修/双专业、
1077 门被必修的课。查询入口有三条，都是**只读本地 JSON、不联网、不调模型**：

```powershell
python tools/curriculum/lookup.py --kb knowledge/curriculum --ask 软件工程专业   # 命令行抽查
python server/main.py                                                            # 产品接口 + MCP(http)
python server/mcp_curriculum.py                                                  # MCP(stdio)，给外部 AI 客户端
```

第三条入口在产品**对话**里：问「信管有哪些专业分流」「大数据管理与应用要修哪些必修课」
「我学过高数B、线代B，像哪个院系的」，对话 agent 会调 `major_lookup` / `major_detail` /
`match_transcript`（工具表与 MCP 共用一份，见 [docs/DEPARTMENT_KNOWLEDGE.md](docs/DEPARTMENT_KNOWLEDGE.md) §4.6）。

接口、MCP 工具表与接入 DSH 的配置：**[docs/DEPARTMENT_KNOWLEDGE.md](docs/DEPARTMENT_KNOWLEDGE.md) §4.3/§4.5**；
离线测试 `python -m pytest server/tests -q`（含 31 条知识库与 MCP 用例、20 条对话侧知识库用例）。

## 协作

- **本轮的轮次目标与分工以仓库上一级的 `任务表.md` 为准**；文档状态、过期条目与「哪些不能作为开发依据」见 **[docs/README.md](docs/README.md)**
- 接口契约（冻结）：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §3/§5；对话与记忆契约：**[docs/DIALOGUE_CONTRACT.md](docs/DIALOGUE_CONTRACT.md)**
- 文档变更一律登记 [docs/CHANGELOG.md](docs/CHANGELOG.md)
