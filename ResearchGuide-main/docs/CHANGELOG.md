# CHANGELOG · 需求与文档变更日志

> **这是所有变更的唯一事实源。** 规则见 DOC_SYNC §1–2：改文档必须在这里追加条目；
> 每个 AI 开发会话开始时 agent 必读本文件并提醒用户新变更。
> 条目格式：`## [日期] [类型] 标题` + 变更内容 / 影响文档 / 影响模块 / 决策来源 / 登记人。

---

## [2026-10-22] [FEAT] 文献库接入 bioRxiv / medRxiv：元数据、今日、收集、打开、预热全链路

- 变更内容：新增 `server/biorxiv.py`（照 `osf.py` 骨架）：官方 `api.biorxiv.org` details 接口取元数据与最新论文（分类过滤 + 本地关键词过滤，30 条/页翻页）；全文下载 JATS XML（`<sec><title>` 成行、`<table-wrap>` 抽表，契约同 `arxiv.parse_html`），取不到退摘要并按 `retry_after` 重试。内部 id 形如 `biorxiv:10.1101.2024.12.06.627082`（DOI 的 `/` 编码为 `.`，路径参数安全；bioRxiv 混用 10.1101 / 10.64898 前缀，都留在 id 里）。api 域名 1 秒串行锁、www 域名（Cloudflare 后面）3 秒单独锁，与 arXiv / OSF 分锁；多版本论文最新版 JATS 路径 404 时退回最早版路径。medRxiv 同一模块 `server` 参数即用（树上暂无临床节点，未挂话题）。`knowledge/arxiv_topics.json` 给 310 基础医学 9 个 thin 节点各追加一个 `bio-*` 话题（q-bio 仍是第一来源，biorxiv 补量；今日取每节点前 2 个话题所以两个来源都会打）。接线：`reading.topic_source`/`triage`、`main.py` 打开论文（库优先，缺则现场取并落库）与 `_paper_ref`、`paper_collect` 元数据与正文分支、`admin.papers_warm` 与 `paper_warm.py` 改走新的 `reading.warm_fetch` 按 id 前缀分派（顺带修掉 psyarxiv 论文在预热里永远失败的旧问题）、`interests.is_thin_topic`。收集加来源过滤：`collect(..., source=…)`、CLI `--source`、`POST /api/admin/papers/collect?source=…`（限 arxiv/osf/biorxiv/medrxiv），可强制只收某一来源的话题铺目录——q-bio 常常自己凑够 thin 定额，轮不到 biorxiv，服务器上铺 bioRxiv 目录靠这个开关（正文定额同样只数该来源）。前端 `app.js`：留文列表外链正则加 `biorxiv|medrxiv` 前缀（否则生成 arxiv 死链）、「在 arXiv 读」判断精确化（`/arxiv/i` 子串会把 biorxiv 误判）、卡片头与重试提示不再写死 arXiv 字样。测试 `server/tests/test_biorxiv.py` 16 例全离线。
- 影响文档：docs/CHANGELOG.md、docs/PAPER_COLLECTION.md（来源范围自此含 bioRxiv）
- 影响模块：server/biorxiv.py（新）、server/reading.py、server/main.py、server/paper_collect.py、server/paper_warm.py、server/admin.py、server/interests.py、knowledge/arxiv_topics.json、web/js/app.js、server/tests/test_biorxiv.py（新）
- 决策来源：扩文献库（310 基础医学 q-bio 文献少，PAPER_COLLECTION §2 thin 名单）
- 登记人：Yukinannie

## [2026-10-21] [FIX] --allow-ephemeral 发生产不再被 NAS 门挡住

- 变更内容：现网仍是 `/tmp` + 速度优先。`production_release_checks` 在 `--allow-ephemeral` 时不再要求函数已挂 NAS、也不再要求 `health.db.ephemeral=false`，但仍带 `QIYAN_ALLOW_EPHEMERAL=1`，否则 PR #27 守门会让 `/api/*` 全 503。正常发生产（不带开关）规矩不变。
- 影响文档：docs/CHANGELOG.md
- 影响模块：tools/deploy/fc_safety.py、tools/deploy/deploy_fc.py、server/tests/test_deploy_fc.py
- 决策来源：上线刚合入的 PR #23–#28，沿用下午的 /tmp 加速
- 登记人：邬程灿

## [2026-10-21] [FEAT] 登录邀请码解锁管理员、运营监测面板、逐请求日志

- 变更内容：登录选填邀请码，填对 `ADMIN_TOKEN`（≥32 字节）则该账号升为管理员，之后不用再填；普通注册不需要邀请码。管理员会话可看 `/api/admin/stats` 与侧栏「监测」面板（默认隐藏）。每条 `/api` 请求打一行 JSON（path/method/code/ms/uid）方便 SLS（pytest 多线程场景不打，避免与捕获器锁死）。合入时叠在 PR #23–#27 之上：侧栏文案与「定位/项目」隐藏仍按 PR #26，监测按钮另加一栏；异常路径的 metrics 仍带 `synthetic=`（PR #24 人工学生）。
- 影响文档：docs/CHANGELOG.md、.env.example
- 影响模块：server/auth.py、server/admin.py、server/store.py、server/account_store.py、server/main.py、web/index.html、web/js/app.js、web/css/styles.css、server/tests/test_auth_password.py、server/tests/test_admin.py、server/tests/test_web_assets.py
- 决策来源：段晓（PR #28）
- 登记人：段晓

## [2026-10-20] [FIX] 服务端守门：函数计算上库不在持久盘就不开业；部署规矩进 README 与仓库根 AGENTS.md

- 变更内容：现网 `/api/health` 仍 `db.ephemeral: true`，每次发版清空账号。`store.db_blocked()` 在函数计算上遇到没设 `QIYAN_DB`（`no_qiyan_db`）、`QIYAN_DB` 在 `/tmp`（`db_in_tmp`）、目录没挂盘（`nas_not_mounted`）时都不建库，`/api/*` 回 503 `db_not_ready`（body 带 `reason` 与按原因写的 `detail`），`/api/health` 的 `db.blocked` 写原因；放行开关 `QIYAN_ALLOW_EPHEMERAL=1`，`deploy_fc.py` 给 `qiyan-test` 和 `--allow-ephemeral` 自动带上、正常发生产时去掉（`fc_safety.apply_ephemeral_flag`）。README 新增「部署（发版前必读）」四条硬规矩 + 验收命令 + 环境变量表；仓库根新增 `AGENTS.md` 给 Codex/Grok 这类 agent 先读。合入时叠在 PR #23–#26 之上，CHANGELOG 各段都留。
- 影响文档：README.md、docs/DEPLOY.md、AGENTS.md（仓库根）、docs/CHANGELOG.md
- 影响模块：server/store.py、server/main.py、tools/deploy/fc_safety.py、server/tests/test_db_mount_gate.py、server/tests/test_deploy_fc.py
- 决策来源：陈浩文（现网每个新版本清空数据；让 agent 也能照规矩发版）
- 登记人：陈浩文

## [2026-10-19] [FEAT] 人工学生：任何 AI agent 通过 MCP 当学生用启研，按屏幕统计走通率与卡点

- 变更内容：新增 `server/mcp_student.py`：十四个「学生能做的事」工具（上线、看屏幕、说一句、按按钮、打开第几篇、带读三步、选方向、项目搜/选/交、stuck、end_day），stdio 与 `/mcp/student`（streamable-http，Authorization: Bearer AGENT_TOKENS 令牌，服务端设 `QIYAN_SELF_URL` 才开远程）两种传输，工具层通过回环调公开接口，限频/预算/锁与真人一致；`server/personas.py` 五张人设卡 + 四项今日目标；`/api/agent/personas`、`/api/agent/register`（走真实注册 + 同意，账号记入 `agent_accounts`）、`/api/agent/event`、`/api/agent/run/end`、`/api/admin/agent-runs`（按 operator/模型/人设汇总、卡住最多的屏幕）；预算加 `LLM_DAILY_AGENT`（operator 合计，默认 200/天）；metrics 加 `qiyan_users{kind="synthetic"}`、`qiyan_agent_runs_total`、`qiyan_agent_stuck_total`、`qiyan_agent_plan_done_total`、`qiyan_agent_http_requests_total`；`tools/synthetic/run_student.py` 用 DeepSeek（OpenAI 兼容）当学生脑子跑一遍；`docs/agents/STUDENT_AGENT.md` 给运维与接入方。测试 `server/tests/test_mcp_student.py`（令牌、注册标记与预算、工具走完首日、事件归属、JSON-RPC 与远程门）。
- 影响文档：docs/CHANGELOG.md、docs/agents/STUDENT_AGENT.md
- 影响模块：server/mcp_student.py、server/personas.py、server/main.py、server/store.py、server/budget.py、server/auth.py、server/metrics.py、tools/synthetic/run_student.py、server/tests/test_mcp_student.py
- 决策来源：陈浩文（人工用户：让别人的 Codex/agent 也能来，拿使用统计）
- 登记人：陈浩文

## [2026-10-19] [FEAT] 监控：本机 Prometheus + Grafana 看启研（tools/monitoring），按路径延迟直方图，告警规则

- 变更内容：照 isse-labs lab4 的做法（不要 Docker，官方二进制放 `.tools/`，运行数据放 `.runtime/`），新建 `tools/monitoring/`：`prometheus.yml`（抓本机 8100）与 `prometheus-prod.yml`（再抓生产，Bearer 读 `.runtime/metrics-token`）、`alerts.yml` 九条规则（抓不到、库没就绪、生产库在临时盘、5xx 比例、注册用户骤降、账号副本过期、刮取错误、SSE 错误、对话 p95）与 `tests/alerts.test.yml`（promtool 单测）、`grafana.ini` + provisioning（数据源 uid `qiyan-prometheus`、仪表盘目录）、`start-prometheus.zsh` / `start-grafana.zsh` / `fetch-tools.zsh`（按 OS/架构下载并校验 sha256，或软链已有的一份）、`smoke_traffic.py`（造一点流量）、README。仪表盘从 `docs/grafana/qiyan-overview.json` 搬到 `tools/monitoring/grafana/dashboards/`，重做成 19 个面板（状态格：进程/持久盘/库就绪/模型/注册/访客/副本用户/副本年龄；请求速率按状态码、在途、5xx 与流错误、p50/p95、按路径 p95、用户数、会话、按路径速率、论文库、刮取错误）。`server/metrics.py`：新增按路由模板的延迟直方图 `qiyan_http_latency_seconds`（21-D34 的桶界）、`qiyan_http_inflight`、`qiyan_db_ready`，`qiyan_process_uptime_seconds` 改单调时钟（21-D38），health `checks.uptime_seconds` 同源；旧全站 p95 保留标 legacy。`server/tests/test_metrics_sync.py` 新增：直方图桶界与路径隔离、在途/就绪/单调寿命、面板与规则引用的指标族都在 `/metrics` 且数据源 uid 一致、有 promtool 就 `check rules` + `test rules`。合入时叠在 PR #22 的 main 上，CHANGELOG 两段都留。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、tools/monitoring/README.md、.env.example
- 影响模块：server/metrics.py、server/main.py、server/tests/test_metrics_sync.py、tools/monitoring/*
- 决策来源：陈浩文（把 lab4 的监控搬过来）
- 登记人：陈浩文

## [2026-10-19] [FIX] 登录可靠性第三批（PR #22）：Codex 第九轮剩余 11 条边界

- 变更内容：接 PR #21。副本与库：登出写 `revoked_sessions` 墓碑（45 天后清），快照带会话吊销与删号墓碑，灌回先合并墓碑再插人/会话，已登出的令牌不会被旧副本灌回来（R9-03）；本地文件写成功即更新新鲜度标记，OSS 旧副本不再越过最新本地提交；OSS 取副本拆成「只取+解密」与「比较+灌回」，10 秒超时后迟到的结果作废（R9-11）；OSS 只有 404 算没有副本，其它错误进 health `last_error`（R9-14）；第一次「进入启研」写 `entered_at` 后触发副本更新（R9-07）；`_conn()` 在盘没挂上时抛 `OSError("db_not_ready")`，`/metrics` 不会在根盘建空库（R9-08），挂载确认后按路径记住、不再每次 stat NAS。登录限频：同用户名失败名额在验密码前原子预约（在途数只记活请求，归零即删），并发 11 个错密码只验 10 个（R9-12）；失败桶 TTL 用实际 `AUTH_SIGNIN_USER_WINDOW`，`AUTH_*` 非正整数启动即拒（R9-13）。发布脚本：远端没显式 `QIYAN_DB` 而本次带 `--db` 时按旧默认库 `server/data/demo.db` 核数、要求 `--migrate-from-backup`（R9-10）。前端：登录/注册/me/同意的 20 秒上限盖住响应正文（R9-04）；启动时 `me` 成功先画页面，prefs/画像/trail 加载 15 秒上限，超时提示「页面资料暂未加载，请刷新重试」，迟到响应不落缓存不导航，资料接口 404/503 不清身份（R9-05）；恢复公告存 `{event_id,state}`，health 回到 normal 即清，登录成功记下已见事件，之后普通到期只显示「登录已失效，请重新登录」（R9-06）。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/store.py、server/account_store.py、server/main.py、server/auth.py、tools/deploy/deploy_fc.py、tools/deploy/fc_safety.py、web/js/app.js、tools/verify/verify_frontend_state.js、server/tests/test_account_store_v2.py、server/tests/test_db_mount_gate.py、server/tests/test_auth_password.py、server/tests/test_deploy_fc.py、server/tests/test_frontend_state.py
- 决策来源：陈浩文（Codex 第九轮报告 R9-03…14）
- 登记人：陈浩文

## [2026-10-19] [FIX] 登录可靠性：Codex 第九轮三条速修（换库备份 oss 判定、DB_READY 等副本灌回后再开、显式未认领不替人认领）

- 变更内容：Codex 第九轮对 4d5e09a 逐项验收后给 14 条令（R9-01…14，见 docs/tasks/25 文末与 PR #21 评论），先修三条最小的：`deploy_fc.backup_remote` 只把 admin 返回的 `oss == "ok"` 当上传成功（原来 `"local"` 字符串也算真，换库后新实例 restore 404）；`store.init_db` 把 `DB_READY = True` 挪到本地副本灌回之后（挂盘重试窗口里先进来一个注册会把唯一旧副本覆盖成只剩他一人）；`account_store` 灌回保留显式 `claimed_at = NULL`（旧的仅 uid 账号还能走 `/api/auth/legacy` 认领）。其余 11 条（会话吊销墓碑、OSS 迟到结果作废、认证 deadline 覆盖正文、启动资料 15 秒、公告绑定 event_id、enter 触发副本、`_conn` 遵守挂载门、隐式旧默认库路径也核数、在途失败名额、窗口 TTL、OSS 非 404 进 last_error）下一批。
- 影响文档：docs/CHANGELOG.md
- 影响模块：tools/deploy/deploy_fc.py、server/store.py、server/account_store.py、server/tests/test_deploy_fc.py
- 决策来源：陈浩文（Codex 第九轮）
- 登记人：陈浩文

## [2026-10-19] [FIX] 登录可靠性第二批（Codex 第八轮对账）：盘没挂上不建空库、墓碑不复活、只有 me 的 401/410 才清身份、启动与登录后有上限、全局恢复状态

- 变更内容：Codex 第八轮按现网配置实测 8e606c2 后给了七条令，与第一批对账后本批做五件（其余推迟，见 docs/tasks/25 「第八轮对账」）。（L05）函数计算上设了 `QIYAN_DB` 而那个目录没挂盘时，`init_db` 不再在实例根盘上悄悄建一份空库（这正是 10-18 丢人的形状）：`store.db_blocked()` 返回 `nas_not_mounted`，`DB_READY=False`，所有 `/api/*`（除 `/api/health`）回 503 `{"detail":"数据库所在的盘还没挂上，请稍后重试","code":"db_not_ready"}`，health 200 但 `ok=false`、`db.ready=false`、`db.blocked`，`_lifespan` 每 5 秒重试直到挂上；副本灌回跳过 `deleted_users` 里的 uid（删号后 dump 失败也不复活，`tombstoned` 计数）；副本多存 `user_prefs` 里的 `entered_at`，灌回后老用户仍直接进今日而不是回到开场对话。（L06）`confirmSessionGone`/`recoverUser` 只在 `/api/auth/me` 对同一令牌回 401/410 时清身份，503/429/网络错误/超时一律保留并提示「暂时无法核验登录状态，请稍后再试」（原来发版、冷启动时整班被踢回登录页）；启动的 `/api/health`、`/api/paths` 5 秒上限，`/api/auth/me` 的 404 不再当会话失效，开场遮罩最多挡 5 秒；登录/注册成功后的初始化 15 秒上限，超时保留会话并提示「登录成功，页面资料暂未加载，请刷新重试」，不重发 POST；health `auth.recovery={event_id,state}` 来自 `QIYAN_RECOVERY_EVENT`/`QIYAN_RECOVERY_STATE`（normal|recovering|accounts_restored|lost），运维登记 `lost` 后，持旧令牌的浏览器在登录页直接看到「服务器确认原账号数据无法恢复，请重新注册；旧记录不会自动找回。」并默认到注册步骤；`normal` 时第一批的推断提示仍是兜底。
- 影响文档：docs/CHANGELOG.md、docs/tasks/25-login-reliability.md
- 影响模块：server/store.py、server/main.py、server/account_store.py、server/auth.py、web/js/app.js、tools/verify/verify_frontend_state.js、server/tests/test_db_mount_gate.py、server/tests/test_recovery_status.py、server/tests/test_account_store_v2.py、server/tests/test_frontend_state.py
- 决策来源：陈浩文（Codex 第八轮报告对账）
- 登记人：陈浩文

## [2026-10-19] [FIX] 登录可靠性：10-18 换库丢人之后——账号副本 v2、限频只计失败、登录页说清、换库必须迁移

- 变更内容：现网 10-18 夜里把 `QIYAN_DB` 从 `/tmp` 换到 NAS 时没有迁数据，换库前的全部账号、会话、访客都没了，之后注册的 5 个用户名账号才进了上游新加的副本；学生看到的是「登录已失效」和「用户名或密码不对」，不是登录代码坏了（实测 signin 401 1.6 秒、register 400、CORS 正常）。本批四件事按 docs/tasks/25-login-reliability.md 的 L01–L04：（L01）`account_store` 副本升到 v2：导出 `users` 表全部行（含访客、微信号）和未过期会话（只有 token 哈希），不含个人 key、明文密码、原始 token；写入先落 `.tmp-<pid>` 再 `os.replace`；2 秒内连续触发合并成一次全量重写，删号立即写，进程退出前 `flush`；灌回改成**合并**（按 uid 补缺、同名不同 uid 记 `collisions` 不覆盖、会话按哈希补缺），不再只在空库时灌；OSS 的拉取从 `init_db` 挪到 `_lifespan` 线程池、10 秒上限，OSS 副本比本地文件旧就不灌（免得复活已删的号）；`/api/health` 的 `db.account_store` 多了 `users`（全部行）、`snapshot_users`、`snapshot_sessions`、`last_dump_at`、`last_hydrate_at`、`last_error`、`collisions`，metrics 多了 `qiyan_account_snapshot_users`、`qiyan_account_snapshot_age_seconds`；旧副本用当前密钥解不开就改名 `.undecryptable-<日期>` 留着。（L02）同用户名的登录桶只记失败、登录成功清桶（原来 10 分钟内含成功一共 10 次，一个班共用账号或输对后反复刷新也会被 429）；signin 的 IP 桶与访客注册桶分开，`AUTH_SIGNIN_PER_IP_HOUR` 默认 3000，`AUTH_SIGNIN_USER_LIMIT`/`AUTH_SIGNIN_USER_WINDOW` 可调。（L03）启动时 `/api/auth/me` 401/404/410 记一个本地标记，随后密码登录再 401 就在登录页加一句「登录已失效且这个账号现在不存在：服务器在 2026-10-18 更换过数据库，之前的账号需要重新注册（用户名可以沿用）…」，登录/注册成功清掉；标记只在 2026-12-01 前写；登录、注册、同意、`me` 四个请求 20 秒超时，超时提示「服务器响应慢（可能在冷启动），请 10 秒后再试」。（L04）`deploy_fc.py --db` 改路径且远端已有用户时必须带 `--migrate-from-backup <本地备份>`，否则拒绝发布；带了就按序 `POST /api/admin/backup` → 本地备份 `integrity_check` → 发版 → health → `POST /api/admin/restore` → 核 `db.account_store.users` 不少于发版前，少了不记成功；备份没上 OSS 且 `BACKUP_DIR` 不在 NAS 时发版前就拒绝（新实例拿不到老实例 /tmp 里的备份）。另：`account_store.status` 在库打不开时也不让 health 500（上游 main 上 `test_health_says_whether_the_db_will_survive_a_restart` 失败）。两版旧副本测试随 v2 契约改写（`v==2`、库里有人也合并、OSS 不在 `init_db` 里拉）。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、docs/tasks/25-login-reliability.md
- 影响模块：server/account_store.py、server/store.py、server/main.py、server/metrics.py、server/auth.py、web/js/app.js、tools/deploy/deploy_fc.py、tools/deploy/fc_safety.py、tools/verify/verify_frontend_state.js、server/tests/test_account_store.py、server/tests/test_account_store_v2.py、server/tests/test_auth_password.py、server/tests/test_deploy_fc.py、server/tests/test_frontend_state.py
- 决策来源：陈浩文（学生反馈登不上；实施令 docs/tasks/25）
- 登记人：陈浩文

## [2026-10-19] [FIX] 发版换空库后已注册账号密码仍能登录

- 变更内容：叠在 GitHub main 35db933（PR #19/#20，界面 w102）上。对照现网「过一会账号密码无效、只能再注册」。根因仍是库在 `/tmp`（`health.db.ephemeral=true`），发版或实例回收换空 SQLite，原用户名不在库里，登录被说成密码不对。新增独立加密副本：注册/升级/删号后写入 `QIYAN_ACCOUNT_STORE` 或 OSS `durable/accounts.json.enc`（不走 `daily/` 的 7 天删除）。新空库启动时若还没有已注册用户，把同一批 uid、用户名、密码哈希灌回，原密码可登录。副本不含明文密码。对话记录、个人 key、方向仍随主库走；A5 NAS 未挂上，本轮不发生产函数。`/api/health` 的 `db` 保留上游 `writable`，并加 `account_store`。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、.env.example
- 影响模块：server/account_store.py、server/store.py、server/main.py、server/tests/test_account_store.py
- 决策来源：邬程灿（2026-10-18：版本更新保留已注册账号密码）
- 登记人：助手

## [2026-10-18] [FIX] 并入 PR #19 后：规则版五问的轮次不再被 gathering 抹掉

- 变更内容：上游 PR #19 的 `dialogue._sync_onboard_state` 每轮把 onboard 的 `phase` 改写成 `gathering`，而模型不可用时的规则版五问（`onboarding.advance`）把当前轮次记在 `phase` 里，结果没模型的新同学被一遍遍问同一句（本分支 `test_degraded_onboarding` 抓到，Codex 第四轮曾复现过同类问题）。现在只有第一轮/空状态记成 `gathering`，已经往后走的轮次与「问完」原样保留，`guide.assess` 判定就绪时仍记 `done`；上游 `test_enter` 对首轮 `gathering` 的期待不变。随上游契约改写了五个本分支测试：对话不再布置任务（接受 → ValueError、拒绝 → abandoned）、开场白是固定文案不调模型（`_voice` 的截断保护单测）、没模型时问完后的提示指向研读模块、`same_user_races` 的 env 桩补 `guidance`。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/dialogue.py、server/tests/test_degraded_onboarding.py、server/tests/test_journey_r6.py、server/tests/test_model_output_callers.py、server/tests/test_same_user_races.py
- 决策来源：陈浩文
- 登记人：陈浩文

## [2026-10-18] [FIX] 并入 main（PR #19：对话首次引导、成绩单接话、「我的」页信息层级，本地 w98–w101）

- 变更内容：把上游 b34ca59（Bekzat070101 的 PR #19）并进本分支。7 处冲突的对接：`dialogue.py` 取上游的「对话不再布置任务」（`action_event` 只接受 decline → abandoned，其它事件 ValueError）与新的模型不可用提示；成绩单导入保留本分支「按学期替换」（`store.replace_terms`）并接上游新的 `append` 模式（`store.add_enrollments`）；`test_dialogue.py` 取上游参数化的「模型动作一律丢弃」；`styles.css` 窄屏对话列取上游；app.js 保留本分支登录页与 `openUpgrade`（上游重做的 `renderLogin` 不接入），删掉「我的」页里的成绩单粘贴列（上游已把成绩单改成对话里接话）；chat.js 保留本分支「等待期间切页不写入」的守卫并接上游 `renderGuide/enterCard(history.guide)`。界面 w102（上游 w101 与本分支 w98 内容不同）。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/dialogue.py、server/tests/test_dialogue.py、web/css/styles.css、web/index.html、web/js/app.js、web/js/chat.js
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文

## [2026-10-18] [TEST] 对话优化 PR 集成验收

- 汇总本轮最终代码和验收材料，新增 docs/verification/dialogue-2026-10-18.md、虚构连续对话实录及三张测试页面截图，方便组内直接复查。
- 提交前对认证、首次引导、记忆、成绩单、保留工具、前端渲染和独立研读执行相关回归，376 项全部通过；最终真实模型持续会话 18 轮，详细边界与自然度局限保留在验收材料中。
- 按用户要求，先一致性备份再清空本地 8789 的两个账号及关联数据，users/sessions/messages/facts/enrollments 均核对为零。清理脚本、数据库、备份、密钥不提交，线上未操作。
- 远端 main 基准已更新核对为 57f4464daff2d3a48883495ee6f96c4826d41464；通过 Fork 分支提交评审，不直接推上游主分支，不部署。
- 登记人：助手。

## [2026-10-18] [FIX] 首次先了解院系、专业、年级与课程（本地 w101）

- 用户决定：按现有对话与进入流程走，开场先邀请院系、专业、年级及成绩单，可选补充本学期在修课程，再自然互相了解，不继续设计新的访谈框架。
- 变更：欢迎语移除研究主题问题，未分专业可只说院系年级；纯成绩单后先补身份，再可选问在修课程，已有信息不重问。侧栏标题改为“目前对你的了解”，不宣称必问五方面。提示词限制主动展开培养方案、逐项采访或强问阅读目标；课程自述独立保存，不声称改了成绩单。进入覆盖规则、确认弹窗与跳过方式不变。
- 修复：用户在“我的”亲自修改的背景仍计为已知；明确兴趣尽量保留原话，避免改写后被证据校验降为推断导致无法推荐开始。
- 验证：最终两组真实模型连续对话共 18 轮（12/6），无降级/拒写/新增任务行动，流式与最终正文一致；91 项相关回归通过。独立注册账号实测新开场并截图；报告和完整实录在工作区 output。当前 8789 用户数据保留，测试使用隔离库。仅本地修改，未提交、推送或部署。
- 影响：server/onboarding.py、dialogue.py、guide.py；web/js/chat.js、index.html 及对应测试、DIALOGUE_CONTRACT、CHANGELOG。
- 登记人：助手。

## [2026-10-18] [FEAT] 约五分钟首次引导与低覆盖进入确认（本地 w100）

- 决策来源：用户本轮明确要求引导尽量充分、重设何时建议进入、信息少时确认但不禁止；覆盖此前 D5 的旧首次进入判定。其余 D1–D6 职责不变，对话不新增任务或论文带读工具。
- 信息设计：五方面为学习背景、主题/目的、课程/实践基础、阅读经历/困难、帮助方式；主题/目的明确且至少四方面已了解才推荐。无固定五分钟/轮数/成绩/填满字段门槛。新增三个只接受自述的阅读记忆 key，原固定信息栏不重做。已有信息和明确拒绝均不追缴。
- 流程：新账号仅显式进入后结束首次界面，发过消息和刷新不再自动进入。首次达到覆盖条件且无当轮问题时，从已提交信息生成一次回顾并停止访谈。低覆盖点击进入统一弹再聊/直接进入，接口不设硬门槛。新 readiness API、history/turn 的 guide 元数据解释判断。
- 页面：移除消息与输入间独立滚动区，回执和进入邀请进入对话流，建议按钮归输入区；长成绩单原文折叠，短输入无多余滚动条；首次侧栏为五方面已知摘要，普通对话保留原结构。
- 实测修复：保留精确 registry key 的下划线（base:math_code 不再变成不合法 key）；长聊保留准确回执的预览历史摘要，不把“没导入”说成“从未预览”。
- 验证与清理：按用户授权先一致性备份并清空 8789 本地预览的三名现有用户及关联数据，线上未动；254 项相关离线回归、认证与隔离等 69 项、摘要去重/渲染收尾 15 项通过（批次重叠不相加）。修正旧测试误将管理员独立口令拒绝当作会话 401 的断言，产品管理员鉴权不变。三组真实模型连续决策 32 轮（含三次确定性完成回顾），实际浏览器另有同一账号 6 轮及零信息确认进入；试跑不混入最终计数。导出实录后清理两个演示账号，当前本地零账号，可重新注册。
- 影响文档：DIALOGUE_CONTRACT、ARCHITECTURE、DESIGN_SPEC、CHANGELOG；影响模块：guide/auth/memory/dialogue/main、chat/app/CSS 与对应回归。仅本地修改，未提交、推送或部署。
- 登记人：助手。

## [2026-10-18] [FIX] 成绩单后接话与“我的”页信息层级（本地 w99）

- 变更内容：修复成绩单独立分支返回后终止本轮的问题。确定性解析及写入后继续模型对话，合并短回执和轻量接话；课程明细默认折叠，保留预览/替换/追加。补充不催答、不重复收集、不将课程等同于熟练程度的提示。
- 页面：“我的”优先显示当前画像、课程基础及可修改记忆，课程/GPA/依据按需展开；选填背景、画像设置、账号数据折叠。移除填表缺项压力与任务难度文案，字段类型和进入条件保持不变。桌面与 390px 窄屏实测，修改记忆后重载可见。
- 验证：200 项相关离线测试通过；最终三组真实模型连续对话共 42 轮（16/14/12），含 6 轮成绩单处理并继续对话，零降级、零被拒记忆写入、零新增任务/行动，全部流式正文与最终回复一致。另以独立演示账号验证真实浏览器导入和后续问答。实录与报告在工作区 output/启研w99连续对话实录.md、output/启研w99成绩单接话与我的页面验收报告.md。自然度仍有少量重复确认及答复偏长，报告如实保留。
- 影响文档：DIALOGUE_CONTRACT.md、ARCHITECTURE.md、DESIGN_SPEC.md、CHANGELOG.md。
- 影响模块：server/dialogue.py 与相关测试；web/js/app.js、chat.js、styles.css、index.html 与渲染验收脚本。
- 决策来源：用户反馈导入后中断及“我的”页面体验；延续 D1–D6 已确认范围。仅本地修改，未提交、推送或部署。
- 登记人：助手。

## [2026-10-18] [FIX] 对话回归信息获取与答疑，首次使用引导与登录样式（本地 w98）

- 变更内容：取消首次开场固定五问承诺；可选介绍一次树洞成绩单全选、复制、粘贴；自然提问先答后补、允许跳过，达到现有进入条件后仅给一次进入邀请。跨近期窗口保留介绍状态和明确交流偏好。回复生成使用实际记忆提交回执。成绩单明确只预览时不保存课程/派生能力，追加时保留旧课程。登录/注册表单统一宽度、间距、主题颜色、标签与复选框尺寸，隐私说明可展开。
- 职责收窄：删除对话 project.search/project.review；代码拒绝所有对话 next_action，关闭旧 action accept/complete，移除对话任务卡和完成通知。项目模块、历史任务、研读 Agent 的练习与判答保留。保留七个专业/课程/成绩工具，尤其 major_lookup 的简称规范化和背景理解。固定字段、onboard_done 条件不改，不新增论文工具，不把对话改为主入口。
- 验证：212 项相关离线测试通过（含独立带读）；最终候选版四组真实模型连续对话共 54 轮、最长 16 轮，零降级、零被拒记忆写入、零新增任务/行动。真实页面检查登录、注册和首次对话；具体实录在本次工作区 output 中。
- 影响文档：docs/DIALOGUE_CONTRACT.md、docs/ARCHITECTURE.md、docs/CHANGELOG.md。
- 影响模块：server/dialogue.py、onboarding.py、tools.py；web 登录与对话及对应测试/验收脚本。
- 决策来源：本次用户对 D1–D6 的确认与首次对话优化要求。仅本地修改，未提交、推送或部署。
- 登记人：助手。


## [2026-10-18] [FIX] 第三轮实施令第四批：Codex 第六轮复核指出的 2 处

- 变更内容：Codex 第六轮确认 R5-01 的 P0 跨画像覆盖已修、R5-02 符合，剩两个 P2。R6-01：`dirAwait` 在请求 fulfilled 与 rejected 之后都校验上下文（`try { return await run(); } finally { c.check(); }`），`saveTrail` 的上下文模式校验完成并重新抛出 `STALE_DIR`，`addMyDirection` 不再在重新读取前读旧 `S.myDirs`；失效操作静默结束，零新增请求/状态写入/绘制/提示，当前上下文的既有提示文案不变。R6-02：方向弹层的「添加到我的方向/取消添加/确认这个方向」、`renderCards/chooseFocus`、「我的」页的「移出」三处 UI 调用方在 await 后、任何状态修改/绘制/课程请求/导航前校验上下文，`STALE_DIR` 静默，其它错误照旧。新增 `test_frontend_state.py` 三个测试与 `verify_frontend_state.js` 场景 `direction-context-errors` / `direction-context-save` / `direction-context-ui`。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、tools/verify/verify_frontend_state.js、server/tests/test_frontend_state.py
- 决策来源：陈浩文（按 Codex 第六轮实施令）
- 登记人：陈浩文

## [2026-10-18] [FIX] 第三轮实施令第三批：Codex 第五轮复核指出的 2 处

- 变更内容：Codex 第五轮在 bd20f1b 上复核第二批 12 条：10 条符合，R4-01 与 R4-12 各有一个新缺陷。R5-01（P0）：切画像时在途的方向操作（add/remove/choose 的 trail GET、prefs GET、choose POST）会在返回后把旧画像的 code/dir/tasks/dirs/sel 写进新画像；现在方向操作在开始时捕获 uid/token/pid/缓存键/纪元（`directionContext`），每次 await 前后校验（`dirAwait`），`portraitOp` 接纳切换/新建/删除以及 `setSession/clearSession` 都递增纪元，失效的操作抛 `STALE_DIR` 静默结束、零写入、不显示旧成功提示；`loadPrefs/putPrefs/saveTrail` 在写 S/缓存/发 PUT 前校验，`saveTrail` 用捕获的 pid/键。R5-02（P2）：`test_network_isolation.py` 的守卫只放行 `ipaddress.ip_address(...).is_loopback` 为真的数值地址，域名（含 localhost、`127.review.invalid`）、空值、通配地址一律记录并拒绝。新增 `test_frontend_state.py::test_direction_write_aborts_after_portrait_switch`（`verify_frontend_state.js` 场景 `direction-context`，六种在途时序 + 切走再切回 + 会话失效）、`test_network_isolation.py::test_guard_rejects_non_numeric_loopback_targets` 与子进程对照。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、tools/verify/verify_frontend_state.js、server/tests/test_frontend_state.py、server/tests/test_network_isolation.py
- 决策来源：陈浩文（按 Codex 第五轮实施令）
- 登记人：陈浩文

## [2026-10-18] [FIX] 第三轮实施令第二批：Codex 第四轮复核指出的 12 处

- 变更内容：Codex 第四轮在 bca5c99 上复核第一批九项：R04/D13/D29 符合，R05/D11 偏离，R01/R02/R03/D02 有新缺陷；按其 12 条实施令由五个 Opus 5.5 会话分支实现后并入。R4-01 方向的 add/remove/choose 写入前先 GET 当前画像 trail（`readTrailForWrite`），以其为准构造修改，GET 失败零写入；R4-02 `pullTrail` 把带 dirs/sel 数组（含显式空数组）的 trail 视为现代数据不迁移，GET 失败不触发 prefs 迁移；R4-03 低于本地 base_rev 的 GET/ACK 不回退草稿版本；R4-04 旧格式「已清空未同步」的 meta 迁移为 dirty+conflict，不被云端旧稿吞掉；R4-05 冲突按钮随当前表单重绑；R4-06 恢复改为先取 `_LOCK` 再关恢复闸，共用五秒截止，排队写入不再与恢复互锁；R4-07 `assert_db_under_nas` 拒绝反斜线/非绝对路径并返回规范化路径，下发给 SDK 的就是校验过的值；R4-08 登录先校验用户名形状，非法名 401 且不建任何桶、不查用户表；R4-09 草稿非 409 失败 toast「草稿未同步，联网后重试」；R4-10 收藏未加载时「我的」显示「收藏未加载，请重试」与重试按钮；R4-11 uptime 版本戳按去重集合比对；R4-12 八个会外联的测试补桩（collect 锁测试 stub `_pin_kit`、缓存性能测试 stub `arxiv._get`、慢 DNS 测试用假解析器、admin 共用 fixture 清 `BACKUP_OSS_BUCKET`），新增 `test_network_isolation.py` 以子进程审计钩子断言零外联。`tools/verify/verify_frontend_loss.js` 加 `readTrailForWrite` 与 trail GET 桩。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、server/admin.py、server/auth.py、tools/deploy/fc_safety.py、tools/verify/verify_frontend_state.js、tools/verify/verify_frontend_loss.js、.github/workflows/uptime.yml、server/tests/
- 决策来源：陈浩文（按 Codex 第四轮实施令）
- 登记人：陈浩文

## [2026-10-18] [FIX] 第三轮实施令第一批：恢复/换钥/草稿/收藏/登录桶/发布门/探活

- 变更内容：按 docs/tasks/20、21 的实施令，由四个 Opus 5.5 实现会话在各自分支完成后并入：20-R01 恢复用唯一 stage 文件 + 连接闸（`store.restore_gate`），写入/校验/替换失败保留原库，换入后清理失败返回 200 带 `cleanup_pending`/`warning`；20-R02 `drafts` 表加 `rev`，`PUT /api/tasks/{tid}/draft` 按 `base_rev` 做 CAS（不等 → 409「草稿已在别处更新，本地稿已保留」），前端草稿按 uid/tid 记 `edit_seq/dirty`，旧响应不再覆盖新稿，409 给「用本地稿覆盖云端」；20-R03 `loadPrefs` 失败置 `undefined`，写收藏前重新 GET，`boot()` 恢复 directions；20-R04 rewrap 按快照密文 CAS，学生并发重存 → 409 `secret_changed`；20-R05 `_HITS` 总数上限 10000、用户名桶 600 秒过期、满时拒绝新桶，形状不对的用户名直接 401 不建桶；21-D02 生产发布须 `QIYAN_DB` 落在函数已配置的 NAS 挂载目录（`fc_safety.nas_mount_dirs/assert_db_under_nas`），缺探活地址或 health 非 `ephemeral=false && writable=true` 不写成功状态，health 增加 `db.writable`；21-D11 uptime 按唯一 wN 比对 Pages 与 API，`QIYAN_REQUIRE_PERSISTENT` 时 ephemeral 必须是 JSON false；21-D13 `deploy_fc.llm_settings()` 只取第一个有密钥的源整组配置；21-D29 备份 OSS 失败 502 回包补 `ok:false`。新测试：`test_admin.py`（恢复并发/清理/连接闸、rewrap CAS、备份回包）、`test_user_store.py`（草稿 CAS）、`test_frontend_state.py` + `tools/verify/verify_frontend_state.js`（草稿同步、收藏重读、boot 恢复）、`test_auth_password.py`（桶上限）、`test_uptime_workflow.py`、`test_deploy_fc.py`（NAS 门、模型配置源）。全量 1173 passed（3.12、3.11）。Codex 第四轮复核进行中，结论回写 docs/tasks/20、21。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/admin.py、server/store.py、server/main.py、server/auth.py、web/js/app.js、tools/deploy/fc_safety.py、tools/deploy/deploy_fc.py、tools/verify/verify_frontend_state.js、.github/workflows/uptime.yml、server/tests/
- 决策来源：陈浩文（按 20/21 实施令）
- 登记人：陈浩文

## [2026-10-18] [FIX] 并入 main（w97：画像内多选方向、画像命名、意见反馈、研读/论文页工具栏）

- 变更内容：把上游 57f4464（Yukinannie 的 w97，经 PR #18 合入 main）并进本分支。3 处冲突的对接：`POST /api/portraits/rename` 加本分支的 `dependencies=SERIAL`（按人串行），`DELETE /api/portraits/{pid}` 保留本分支的 SERIAL + `_model_work`；app.js 的页面分发仍用本分支带 try/catch 的 switch，加 `feedback` 视图；上游新的「画像与方向」面板整体采用，但切换/新建/删除三处改走本分支 `portraitOp`（服务器成功后才改前端画像、换画像时清上一份的方向/项目/定位草稿、防连点），`portraitOp` 加 `after` 钩子；`clearSession` 补清 `portraitName/mePortrait`；界面 w98（上游 w97 与本分支 w84 内容不同）。其余原样并入：`feedback` 表与 `/api/feedback`、`/api/admin/feedback*`、`portraits.name` 迁移、`interests.direction_nodes`（trail.sel 多选）、`daily_for_user` 合并多方向目录、研读 `read-toolbar`、论文页 `paper-tools` 与滚动联动、`test_feedback.py`。
- 影响文档：docs/CHANGELOG.md、docs/tasks/21-r3-sheet-audit.md（加「w97 之后」一节）
- 影响模块：server/main.py、server/auth.py（`/api/admin/feedback`、`/api/admin/feedback/status` 补进 PUBLIC，与其它 admin 路由同一约定）、web/index.html、web/js/app.js
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文

## [2026-10-17] [FIX] 并入 main（w83：request-10-16 缺陷修复）

- 变更内容：把上游 f4c91f6（w83，按 docs/tasks 15–19 缺陷单修复）并进本分支。本次上游已是 LF；`.gitattributes` 取本分支版本（多列二进制类型）。对接本分支：auth 的 `PER_IP_HOUR` 仍按本分支（环境变量 `AUTH_PER_IP_HOUR`，默认 600）并加上游的 `SIGNIN_USER_LIMIT/WINDOW`，`_allow`/`_recent` 加 `window` 参数；`budget.py` 仍是本分支那份，补上游 `consented()`；`llm.enabled` = 同意检查 + 本分支「服务器 key 要有额度」；`PUT /api/tasks/{tid}/draft` 保留本分支「已完成的作业可清空草稿」的放行，返回上游 `set_draft_row`（带 updated_at）；`deploy_fc.llm_settings()` 取上游形状（key/base_url/model，`test_local_defaults_returns_three_keys` 钉着），`_local_defaults` 只在 DeepSeek 地址下写 `DEEPSEEK_API_KEY` 并透传 `LLM_REASONING_EFFORT`；app.js 会话块仍是本分支的（`setSession/clearSession` 补 `llmKeyInvalid`），任务草稿键仍按人（`rg_draft_${uid}_${tid}`、`rg_draft_meta_${uid}_${tid}`）并接上游的 updated_at/dirty 同步。界面 w84（上游 w83 与本分支 w82 内容不同）。
- 影响文档：docs/CHANGELOG.md
- 影响模块：.gitattributes、server/auth.py、server/budget.py、server/llm.py、server/main.py、tools/deploy/deploy_fc.py、web/index.html、web/js/app.js
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文
## [2026-10-16] [OPS] Pages 发到 w83，生产 API 仍是 w74

- 变更内容：前端同步 GitHub Pages（w83）。现网 API 仍是 w74，新页面打旧接口的 P0 未解除（ranking / 搜索 / PDF / `/metrics` 等在现网上不可用或行为不同）。A5 未挂盘，本轮不发生产函数，未把 `/metrics` 发到现网。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md
- 影响模块：web/（gh-pages）
- 决策来源：邬程灿（request-10-16 评审 2026-10-16）
- 登记人：助手

## [2026-10-16] [FIX] request-10-16 本机可证缺陷修复（w83）

- 变更内容：对照 15–19 缺陷单，本机落地 P0/P1 与部分 P2。16：密钥 InvalidTag 回落、rewrap 不改环境变量、restore 原子换库、refuse_unsafe 拦未挂载路径、登录先限流+用户名桶、自带 key 也查隐私同意、OSS 失败 502、测连成功后再写配置、ticket 过期 410。18：收藏/草稿/输入框数据丢失；带读旧题失败保留 retry、restart 升 rev、标题规则收紧、节选以 loc 为中心。17：目录模式 OSF 打开、补搜失败保本地、今日去重、thin 先读目录、目录优先；_load 以库为准、abstract 不盖 html；今日取数进 BoundedPool；stale 每天每话题刷新 1 次；OSF `_vN` 可 triage；admin 采集切片+互斥锁。19：/metrics 路由模板+基数上限+锁内快照+令牌；本机可证后**仍不把 /metrics 发到生产**。界面 w83。A5 NAS、两次发版后账号、监控计划 §2.2 的 1–4 仍待确认。本轮未发生产函数。
- 未做完的 P2（不准写成已完成）：17-C18、C19、C20、C22、C23、C26；19 建议项 histogram / llm_calls；Grafana alert rules；生产抓取 /metrics。16-A1 真挂 NAS 与两次发版后 qiyan_users 不变：待确认。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、.github/workflows/uptime.yml
- 影响模块：server/auth.py、store.py、llm.py、budget.py、admin.py、arxiv.py、tutor.py、main.py、paper_collect.py、reading.py、metrics.py、pdf.py、web/js/app.js、web/js/chat.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（request-10-16 评审 2026-10-16）
- 登记人：助手

## [2026-10-15] [FIX] 并入 main（w81：/metrics、paper_dir 与方向论文入库、隐私渲染、带读拒标题）

- 变更内容：把上游 7a8dc36 并进本分支。上游这 30 个文件是在 Windows 上以 CRLF 提交的：新增 `.gitattributes`（`* text=auto eol=lf`），用 `git merge -X renormalize` 合并，仓库内容仍全部 LF，只剩 5 处真冲突。对接本分支：`workpool.BoundedPool` 仍是本分支的有界队列实现，补 `snapshot()` 给上游 `_model_pool()` 用；health `model_pool` = 本分支模型池实值（workers/threads/running/queued/max_queued）+ 上游的 threads_borrowed/threads_max/pdf_queued/pdf_running；「我的」账号面板与登录页保留本分支（已有 openPrivacy 隐私说明、consentRow 同意勾选、导出、切回账号、设用户名密码），上游新加的 `privacyBox()` 保留但登录页不重复渲染；`applyLlmPill` 取上游（未登录隐藏，不再显示「连接模型」）；上游 `/metrics`、`record_metrics` 中间件、`paper_dir` 表与 `GET /api/papers/dir`、`paper_collect.py`、搜索库优先、研读左栏读目录、带读拒绝标题、`direction_node` 不再回落 7 码、`test_metrics_sync`/`test_paper_dir` 原样并入。界面 w82（上游 w81 与本分支 w81 内容不同，再 +1）。
- 影响文档：docs/CHANGELOG.md
- 影响模块：.gitattributes、server/workpool.py、server/main.py、web/index.html、web/js/app.js
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文

## [2026-10-15] [OPS] Pages 发到 w81，生产 API 仍是 w74

- 变更内容：前端同步 GitHub Pages（w81）。现网 API 仍是 w74，新页面打旧接口的 P0 未解除（ranking / 搜索 / PDF / `/metrics` 等在现网上不可用）。A5 未挂盘，本轮不发生产函数。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md
- 影响模块：web/（gh-pages）
- 决策来源：邬程灿（体验问题/监控计划/论文入库 2026-10-15）
- 登记人：助手

## [2026-10-15] [FEAT] 本机落地体验/监控小修与方向论文目录

- 变更内容：隐私说明在登录/注册页渲染后再 consent；「记录」改为「我的」；「我的」页可导出 JSON；侧栏默认「我的 DeepSeek」，管理员入口只留登录页。`papers/{id}` 合并工具包 `title_zh`。带读拒绝把论文标题当主张原句。ranking 有条目时 recommend 不再长期空。`direction_node` 不再用 7 码假装细类。占位页不调用死代码。health.model_pool 改为线程池/PDF 池实值。`/metrics` 的 5xx 与分路 counter 同源；补 `qiyan_paper_dir_*`。uptime 多记 ephemeral 与 Pages/API 版本，仅 secrets `QIYAN_REQUIRE_PERSISTENT` 时因 ephemeral 失败。新增 `paper_dir`、`server/paper_collect.py`、`GET /api/papers/dir`、搜索库优先、研读左栏读目录。界面 w81。A5 仍待确认，本轮不发生产函数。
- 影响文档：docs/CHANGELOG.md、docs/grafana/qiyan-overview.json
- 影响模块：server/metrics.py、server/main.py、server/workpool.py、server/planner.py、server/interests.py、server/tutor.py、server/store.py、server/reading.py、server/admin.py、server/paper_collect.py、web/js/app.js、web/index.html、web/css/styles.css、.github/workflows/uptime.yml
- 决策来源：邬程灿（体验问题/监控计划/论文入库 2026-10-15）
- 登记人：助手

## [2026-10-15] [DOCS] 体验问题、监控计划、方向论文入库拆成三份

- 变更内容：原 `docs/MONITORING_OPS.md` 拆成 `docs/PRODUCT_ISSUES.md`（产品体验问题）和 `docs/MONITORING_OPS.md`（系统监控 + 系统运维）。新增 `docs/PAPER_COLLECTION.md`：方向树 53 个节点各检索定额论文写入后端（目录 20 / 正文 8，thin 节点 8/4），研读不再只靠现场搜索。副本同步到仓库外 `运维信息/`。
- 影响文档：docs/CHANGELOG.md、docs/PRODUCT_ISSUES.md、docs/MONITORING_OPS.md、docs/PAPER_COLLECTION.md
- 影响模块：无代码（计划稿）
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-15] [OPS] 暴露 /metrics，写监控运维计划

- 变更内容：本机 `GET /metrics` 输出 Prometheus 文本（存活、ephemeral、用户/会话、论文库、HTTP 计数与 p95）。Grafana 看板 JSON 在 `docs/grafana/qiyan-overview.json`。体验记录与指标选取后拆到 `docs/PRODUCT_ISSUES.md` 与 `docs/MONITORING_OPS.md`。生产库仍 ephemeral，不发生产函数。
- 影响文档：docs/CHANGELOG.md、docs/MONITORING_OPS.md、docs/grafana/qiyan-overview.json
- 影响模块：server/metrics.py、server/main.py、server/auth.py
- 决策来源：邬程灿（第 4 次小组汇报 / 课程「系统监控与运维」）
- 登记人：助手

## [2026-10-15] [FIX] 并入 main（w75–w80：论文缓存止血、会话与方向、今日按方向、研读三栏与论文库、带读判卷）

- 变更内容：把上游 372e767 并进本分支（它按 docs/tasks 的 11–14 实现了 w75–w80）。对接本分支：预算仍是 budget.py 这一份（按人按天、同意隐私才调、bind/unmetered），补上上游要的 `metered_as("*xlate")`、`current()`、`LLM_DAILY_XLATE`，`*` 开头的伪账号只查自己和全站的上限；`llm.chat/chat_json` 用本分支的扣额逻辑，加上游的 `total`/`retry`；线程池仍是本分支的 workpool（取消即出队、带上下文变量、能 drain），加 `PoolFull = Overloaded` 别名和 kwargs，上游的 pdf 池直接用它；`_FLIGHTS` 全部用 BoundedPool（新增 xlate 1 线程 8 排队）；带读 start/turn 走 `_paper_ref`（本地 PDF 不取 arXiv）但仍进模型池、挂用户锁；`_review_full` 保留；论文原文仍要登录（不按 arXiv 编号放行）；缓存写盘失败照上游打 warn 不 500。前端：会话仍用本分支那套（localStorage 三键、停着的号、换人整页重载），接上上游的 `sessionGoneMsg`（401 落登录页并提示）、`confirmLeave`/`clearChatDraft`、`logout(next)`（退出 / 换账号）、用户菜单四项、`chooseDirection`、`renderPositionSoon`、今日按方向文案、分拣不带 kit；隐私说明用上游 w80 那份（PDF、作答发给模型），版本 2026-10-15。界面 w81。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/budget.py、server/workpool.py、server/llm.py、server/main.py、server/arxiv.py、server/auth.py、server/dialogue.py、server/tests/test_auth.py、tools/deploy/bootstrap、tools/deploy/download_wheels.py、web/js/app.js、web/js/chat.js、web/index.html
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文

## [2026-10-14] [FIX] 今日去掉「定位微调」

- 变更内容：今日不再渲染定位入口。服务端 `daily_tweak` 仍回 `view:"position"`（测试不动），页面不画。界面 w80。A5 仍待确认，本轮不发生产。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FEAT] 研读三栏、论文库、PDF 导入、定位待开发

- 变更内容：工具包 / 已读正文进 `paper_text`，预热 `POST /api/admin/papers/warm`，发版后循环预热（无 `ADMIN_TOKEN` 打印请手动预热）。研读页一屏三栏内滚，去掉「阅读顺序」。搜索路由写在 `{arxiv_id}` 之前，拒中文。导入 PDF 仅本人可见、能带读、不交卡。论文页 16px、标题下中文行、侧栏可收起。定位栏占位「待开发」。工具包 10 篇 `title_zh`/`keywords` 按标题如实写中文并标机器待核。界面 w79。A5 仍待确认，本轮不发生产。
- 影响文档：docs/CHANGELOG.md、docs/READING_POSITIONING.md、docs/DESIGN_SPEC.md、docs/DEPLOY.md
- 影响模块：server/store.py、server/arxiv.py、server/reading.py、server/tutor.py、server/main.py、server/admin.py、server/pdf.py、server/workpool.py、web/js/app.js、web/css/styles.css、web/index.html、knowledge/kits/llm-eval.json
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FEAT] 今日按当前方向推荐、标题机翻

- 变更内容：今日 `GET /api/daily` 不再带 kit。候选按 `trail.dir`：方向话题保底 4/6，同级兴趣最多 2/6。心理/社会学走 PsyArXiv/SocArXiv，经济/金融走 arXiv 分类。标题下机翻 + 关键词写入 `paper_zh`，`*xlate` 记账。冷话题每请求最多现取 1 个。未知 kit_id 的 prefs 回 400。界面 w77。
- 影响文档：docs/CHANGELOG.md、docs/ARCHITECTURE.md、docs/tasks/03-reading-today.md
- 影响模块：server/interests.py、server/reading.py、server/osf.py、server/budget.py、server/store.py、server/main.py、server/admin.py、web/js/app.js、knowledge/arxiv_topics.json
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FEAT] 带读作答框、两页签、模型按标准判卷

- 变更内容：论文页「AI 带读 / 写阅读卡」。每题可见 `textarea.tutor-answer`，可手输、粘贴、填进作答框。硬判（引文逐字、局限段、字数、不抄）过了再调 `paper-quiz-grade` 按 criteria 判；形状不对或没模型记 pending，可重判，不回退规则判。没模型或非工具包则带读页签灰显。跳站按英文 heading。隐私说明改为作答会发给模型判对错，`PRIVACY_VERSION=2026-10-15`。界面 w76。当前树没有独立额度模块，上课周 `LLM_DAILY_TOTAL=5000` 未落地（w74 无 `budget.py`），本轮不另起额度方案。
- 影响文档：docs/CHANGELOG.md、docs/READING_POSITIONING.md、docs/ARCHITECTURE.md、docs/tasks/04-paper-tutor.md、skills/README.md
- 影响模块：server/tutor.py、server/llm.py、server/main.py、server/auth.py、web/js/app.js、web/css/styles.css、web/index.html、skills/paper-tutor、skills/paper-quiz-grade
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FEAT] 会话持久、方向 ranking、多方向、对话不派任务

- 变更内容：登录写回 localStorage 三键、30 天滑动；失效 toast 后落登录页。右上角下拉：我的 / 我的 DeepSeek / 退出 / 换账号。对话右栏改规则 ranking。`prefs.directions` + dirChip 切换走共用 `chooseDirection` 并 await PUT trail，进度进 `byDir`。对话 `next_action` 只留 `explore_direction`。画像剪枝，旧数据进「其他」。各页居中，对话页 1240px。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、docs/DIALOGUE_CONTRACT.md、docs/TASK2_DIALOGUE.md、docs/ARCHITECTURE.md
- 影响模块：server/planner.py、server/interests.py、server/memory.py、server/dialogue.py、server/main.py、web/js/app.js、web/js/chat.js、web/css/styles.css
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [OPS] A5 持久盘本轮仍待确认

- 变更内容：DEPLOY.md 写明本轮 NAS 未挂上、不准假装已挂盘、不准把生产发版写成已完成；补本机验收命令。没挂盘前不为演示发生产（发版会清 `/tmp` 库）。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md
- 影响模块：无代码
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FIX] 论文缓存止血（13.0）

- 变更内容：`ARXIV_CACHE_DIR` 生效（FC 默认 `/tmp/papers`）。写盘失败打 warn 仍返回结果。抽表前的旧 HTML 缓存用户路径照常返回并带 `needs_reparse`。先取 HTML，成功则从页面取标题，失败才打元数据；调用方可给 `hint`。非法 arXiv id 回 400。摘要状态行加「重新取正文」。界面 w75。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md
- 影响模块：server/arxiv.py、server/main.py、web/js/app.js、web/index.html
- 决策来源：邬程灿（request-10-14）
- 登记人：助手

## [2026-10-14] [FIX] 并入 main（w71–w74：壳与栏目、开场全屏对话与「我的」、按兴趣每日论文、服务端带读、运维止血）

- 变更内容：把上游 d874523 并进本分支（它按 docs/tasks 的五份任务书实现了 w71–w74）。和本分支对接：模型池与限流照旧——带读的 start/turn 进 `_MODEL` 池（`_in_model_pool`，从 `_model_work` 抽出）并挂用户锁，`/api/cards` 仍挂 SERIAL，每日取数用 `workpool.BoundedPool(1, 16)` 的独立小池而不是 `ThreadPoolExecutor`；`/api/health` 同时给 `model_pool` 的真实计数和上游的 `checks`；`quotes.locate` 同时支持 `prefer`（多处出现时挑局限段那一处）和带读的 `within`/`at`，`check_limitation` 仍传 `prefer=LIMIT_SECTIONS`；`store.init_db` 先建墓碑触发器再迁移旧的 XOR 封装；deploy_fc 用上游的 merge_env/refuse_unsafe、删掉本分支那份吞异常的 `existing_env`，更新与创建都保持单实例 200 并发。前端：保留本分支的会话持久化（localStorage、停着的号、换人整页重载）、微信 / 账号 / 注册三栏登录页和「设用户名密码」，接上上游的 `entered`（`setSession` 读 `r.entered`、`firstView`/`enterApp`、登录后统一走 `afterEnter`）、`confirm` 转「我的」页、项目占位页、今日论文区、对话右栏只读；`/api/nba` 随上游删去。隐私说明合成一份（带读作答一条照上游），指纹重算。界面 w75。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、tools/deploy/README.md
- 影响模块：server/main.py、server/quotes.py、server/reading.py、server/store.py、tools/deploy/deploy_fc.py、web/js/app.js、web/js/chat.js、web/index.html、server/tests/test_auth.py
- 决策来源：陈浩文（并入上游）
- 登记人：陈浩文

## [2026-10-13] [FIX] deploy_fc 适配当前 FC SDK 的 GetFunction

- 变更内容：`get_function` 必须带 `GetFunctionRequest`，否则读现有环境变量在发版时直接 TypeError。
- 影响文档：docs/CHANGELOG.md
- 影响模块：tools/deploy/deploy_fc.py
- 决策来源：邬程灿（现场发版）
- 登记人：助手

## [2026-10-13] [FIX] 带读 404 与抽表前的论文缓存

- 变更内容：抽表之前写下的 HTML 正文缓存（没有 `tables` 字段）下次打开会重取。latexml 用 `<td>` 写的表头升成表头。本机旧进程没有 `/api/tutor`，需重启后带读才通。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/arxiv.py
- 决策来源：邬程灿（现场）
- 登记人：助手

## [2026-10-13] [OPS] 运维止血、备份接口与手册

- 变更内容：`deploy_fc` 读现有环境变量失败不再当空字典；生产缺盘或缺 `QIYAN_SECRET`/`WECHAT_APP_ID` 拒绝发布；单实例 `reserved_concurrency=1`。新增 `/api/admin/backup|restore|stats|maintenance|user/delete|rewrap`。删号打 `user_deleted` 日志。个人密钥改 AES-GCM。health 增加 `checks`（状态码仍 200）。探活工作流、发版清单和事故手册写入 DEPLOY.md。NAS / 钉钉 / RAM 标待确认，没有持久盘不准把生产发版写成已完成。人工找回口径改为用户名密码、每次打开要重新登录。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、tools/deploy/README.md、.env.example
- 影响模块：tools/deploy/deploy_fc.py、tools/deploy/fc_safety.py、tools/deploy/pack_fc.py、tools/deploy/publish_gh_pages.py、server/admin.py、server/store.py、server/auth.py、server/main.py、server/llm.py、.github/workflows/
- 决策来源：邬程灿（request-10-13）
- 登记人：助手

## [2026-10-13] [FEAT] 服务端带读论文

- 变更内容：研读论文页加「让它带我读」。启研 DeepSeek 按 `skills/paper-tutor/SKILL.md` 出题，代码按节判对错；学生作答不发给模型。读完预填阅读卡七栏，不自动交卡。只允许工具包论文。隐私说明列出「带读里的作答」并升到 2026-10-13。界面 w74。
- 影响文档：docs/CHANGELOG.md、docs/READING_POSITIONING.md、docs/ARCHITECTURE.md、docs/DESIGN_PROPOSAL.md、skills/README.md
- 影响模块：server/tutor.py、server/quotes.py、server/reading.py、server/store.py、server/main.py、skills/paper-tutor/SKILL.md、web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（request-10-13）
- 登记人：助手

## [2026-10-13] [FEAT] 今日按兴趣推荐每日新论文

- 变更内容：每日候选按策展词表匹配兴趣与方向节点，不调模型翻译。今日主体改为分拣最近新论文；无话题时仍走先聊聊/核对/选方向。页面不加搜索框。删 `/api/nba`。界面 w73。
- 影响文档：docs/CHANGELOG.md、docs/ARCHITECTURE.md、docs/DESIGN_SPEC.md
- 影响模块：server/interests.py、server/reading.py、server/arxiv.py、server/store.py、server/main.py、server/planner.py、knowledge/arxiv_topics.json、web/js/app.js
- 决策来源：邬程灿（request-10-13）
- 登记人：助手

## [2026-10-13] [UX] 开场全屏对话，「我的」承接核对与记忆

- 变更内容：登录后未进入的人先全屏开场对话，可随时「进入启研」。进入按人记在 prefs。记忆编辑收到「我的」页，对话右栏只读。刷新仍要重新登录，登录后按后端 entered 恢复，不关回开场。界面 w72。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md、docs/TASK2_DIALOGUE.md、docs/DIALOGUE_CONTRACT.md、docs/ARCHITECTURE.md
- 影响模块：server/auth.py、server/main.py、server/dialogue.py、server/onboarding.py、web/js/app.js、web/js/chat.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（request-10-13）
- 登记人：助手

## [2026-10-13] [UX] 壳与栏目：首页加快、作业出侧栏、项目待开发、对话进侧栏

- 变更内容：首页换屏约 1.4s、能排队连翻。侧栏改为今日 / 对话 / 研读 / 定位 / 项目（待开发）。作业不占栏目，任务页改称「这一步」。项目进占位页。研读修正文溢出、字重、伪章节、前置信息、矩阵宽度。界面 w71。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md
- 影响模块：web/js/app.js、web/js/chat.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（request-10-13）
- 登记人：助手

## [2026-10-12] [FIX] 并入 main（w67–w70：四栏引导、作业正式题与提示、项目问答、研读真表、定位人话；注册登录、每人一把 DeepSeek、进度入库）

- 变更内容：把上游 5898874、743b5e2、0d188ae 并进本分支。拿过来的：四栏「这一页先做什么」、作业正式题（已知 / 求 / 交）和「卡住了要提示」、项目先问答再按整条链检索、研读真表和一张卡出矩阵、定位序号人话和卡片地图、Windows 慢响应头按总时限断开、云端依赖按 wheel 清单重装；用户名 + 密码注册 / 登录 / 升级（`/api/auth/register`、`signin`、`upgrade`）、每人一把 DeepSeek（`user_secrets`，调用按 uid 带 key）、作业草稿 / 项目路径 / 研读工具包进库按人隔离。和本分支对接：模型预算（budget.py）只管服务器的密钥——用自己密钥的人（`llm.resolve_cfg` 给 `own=True`）不问额度也不扣；注册了用户名的人按「能找回的账号」算 `LLM_DAILY_USER`，不按访客；`/api/auth/me` 同时给 `llm_left` 和 `has_llm_key`；流式回复同时带 `status` 和 `uid`；`llm.ready(uid)` 兼容测试里零参的 `enabled` 桩；导出仍在一个读事务里，多出 `username` 一列；删号和墓碑触发器按列自动覆盖新表。登录页：微信 / 账号登录 / 注册三个标签，访客入口照旧要先勾隐私说明，密码登录后隐私说明过期会再问一次；「记录」页多「设用户名密码」（访客和微信号都能补）和「我的 DeepSeek」；用户菜单和侧栏模型钮也能开「我的 DeepSeek」；已完成的作业用上游的折叠面板，刷新后没有评阅的照样去服务器取。没拿的两条上游界面决定（w69「只留登录 / 注册、不再给访客入口」、w70「登录只记在内存里、打开页面重新登录」）：本分支的访客号、停着的另一个号、跨标签页同步、草稿都靠这台浏览器里存着的会话，改成每次重新登录等于访客的记录一刷新就没了；要跟上游走的话改 `boot()` 开头和 `setSession` / `clearSession` 即可。服务器上的草稿写进带 uid 的本地键。隐私说明补上用户名、上传的密钥、草稿和路径进度（版本 2026-10-12）。界面版本 w71。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md
- 影响模块：server/llm.py、server/budget.py、server/auth.py、server/store.py、server/dialogue.py、server/workbench.py、web/js/app.js、web/index.html
- 决策来源：陈浩文（并入上游，保留本分支的会话和访客模型）
- 登记人：陈浩文

## [2026-10-12] [UX] 打开页面必须重新登录

- 变更内容：登录只记在这一页的内存里，不再写入 `localStorage`。刷新或重新打开都要重新登录。界面 w70。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-12] [UX] 进入只留注册和登录，不再劝访客升级

- 变更内容：首页进入只留「登录 / 注册」。访客接口还在，页面不再出现「先看看」和「升级成可找回的账号」。界面 w69。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-12] [FEAT] 注册登录、每人一把 DeepSeek、进度按人入库

- 变更内容：用户名+密码注册/登录/访客升级。每人可上传自己的 DeepSeek，探测后写入同一份库的 `user_secrets`，调用按 uid 带 key，不写 `os.environ`。作业草稿、项目路径、研读工具包进库，按 `user_id` 隔离。访客入口形状不变。界面 w68。
- 影响文档：docs/CHANGELOG.md、docs/DEPLOY.md、docs/DESIGN_SPEC.md
- 影响模块：server/store.py、server/auth.py、server/llm.py、server/main.py、web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（产品部署要求 + 账号入库）
- 登记人：助手

## [2026-10-12] [DOCS] 三套环境各用各的库

- 变更内容：DEPLOY 写清开发/测试/生产三份库；生产 `QIYAN_DB` 要指到会留的盘；一人一份 sqlite 不做。
- 影响文档：docs/DEPLOY.md、docs/CHANGELOG.md
- 影响模块：文档
- 决策来源：邬程灿（产品部署要求）
- 登记人：助手

## [2026-10-12] [FIX] 云端依赖按 wheel 清单重装，避免 pydantic 打架

- 变更内容：bootstrap 用 vendor_wheels 文件名当缓存标记，清单变了就清空 `/tmp/pydeps` 再解。避免新旧 pydantic-core 叠在一起导致函数起不来。
- 影响文档：docs/CHANGELOG.md
- 影响模块：tools/deploy/bootstrap
- 决策来源：邬程灿（发布 w67 到 Pages / 函数计算）
- 登记人：助手

## [2026-10-12] [FIX] Windows 上慢响应头也要按总时限断开

- 变更内容：看门狗到点后同时 shutdown/close 原 socket，不只关副本。响应头慢滴时，Windows 不再把线程挂到整段头写完。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/limits.py
- 决策来源：邬程灿（problem-10-12 总验收，run_existing_tests.py）
- 登记人：助手

## [2026-10-12] [UX] 定位改成序号人话 + 卡片地图

- 变更内容：五个 tab 改成「1 我有什么」等人话；每页一句 `.pos-lead`；竞争地图改成卡片；没有边时下注回「我有什么」。界面 w67。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-12] [UX] 项目先问答再按整条链检索

- 变更内容：找项目第一屏改成由宽到窄的选项问答；旧方向芯片收到「改条件」。检索可带 `chain`，整句权重大于单关键词；空方向不再把候选打成 0。新路径 `POST /api/projects/interview`。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/projects.py、server/main.py、web/js/app.js
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-12] [UX] 研读跳转加大、正文见表、一张卡出矩阵

- 变更内容：章节跳转改成 36px 描边按钮。HTML 表抽成 `tables` + `[[table:tN]]`，阅读区渲染真表；抽失败给原文链接。矩阵改为 1 张过线卡即可。界面 w67。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md
- 影响模块：server/arxiv.py、server/reading.py、server/main.py、web/js/app.js、web/css/styles.css
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-12] [FEAT] 作业正式题 + 卡住了要提示

- 变更内容：新生成作业带 `formal`（已知 / 求 / 交）；校验失败走骨架，不编课名。未完成作业可 `POST /api/tasks/{tid}/hint`，拒绝替写提交全文。
- 影响文档：docs/CHANGELOG.md
- 影响模块：server/workbench.py、server/main.py、server/schemas.py、web/js/app.js
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-12] [UX] 作业先看题，细则点开

- 变更内容：作业主面板改成题干（已知 / 求 / 交）+ 提交；题目说明、怎样算做到、评阅、下一步、路径说明收到按钮后面。已完成默认打开评阅。界面 w66。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-12] [UX] 四栏加「这一页先做什么」

- 变更内容：作业 / 项目 / 研读 / 定位页头下加三步引导，可关掉；走过主流程的人默认只留「为什么是这几步」。页头副题收短。界面 w65。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿（problem-10-12）
- 登记人：助手

## [2026-10-10] [FIX] Codex 七轮复查、两百人压测、学生全流程走查，并入 main（五栏 + w64）：权限、预算、注入、并发写、可用性、数据正确性

- 变更内容：Codex（gpt-6-astra）对 #16/#17 和全站做了四轮审查，逐条复现后修掉。权限：uid 核对不再看 Content-Type 大小写；微信只认安全模式、同一条消息不能配两次登录、猜数字先查限额。预算：模型调用按人/按全站每天封顶，访客、没同意隐私说明的、匿名接口不调（budget.py）。注入：前端所有 innerHTML 先消毒，几处模型/用户文本逐处转义。并发：同一个人的改动一件一件来（userlock.py，异步等锁、每人排队上限），流式对话在自己的线程里跑完再放锁；删号后在路上的请求写不回来（墓碑 + 触发器）；评阅记录按当时的画像。可用性：MCP 不卡事件循环，论文抓取有上限、没人等的就取消，/api/papers 要登录。数据：成绩单按学期替换、「不及格」保留、半学分照算、无穷大学分拒收并修老库；改方向换键而不是换标签；替换写入新来源和可信度；重复结论和重复卡都对账；任务反馈存库；导出在一个读事务里。前端：换人整页重载、草稿按人存、只在明确退出时清；对话页晚到的渲染不覆盖别的页。压测（模拟两百人同时用）：等模型的接口挪进自己的线程池（workpool.py），公用池留给普通读写，原来两百人等模型时连健康检查都要排五秒；浏览器走了，还在排队的那轮对话撤掉、马上放锁，排队有上限（满了回 503）；同一出口地址一小时 600 次登录（原来 30，一个机房开不了号），全体访客另有每天合计的模型额度；上游重试也扣额度；服务关闭时等在跑的那一轮写完。全流程走查：访客绑微信、这个微信却已有账号时先问，访客号的会话留在这台浏览器上可以切回去（原来直接切走，访客的记录再也进不去）；交成果的评阅按一次读出的项目和画像记，项目页带上它属于的画像；模型给的任务格式不对（「20分钟」）照样建出任务，建不出来行动也不记成已接受；对话生成中切走再回来，新页面接上那一轮；切画像后方向重新取。
- 并入 main：保留五栏和 w64 首页；登录页用本分支的（公众号没开通时只给访客入口，要先勾隐私说明——没同意的人不调模型）；会话过期先重试一次再踢人（上游的多实例兜底）照留；论文原文 /api/papers 仍要登录（上游按路径放开了，没登录也能让服务器去 arXiv 取任意论文）。界面版本 w65。五栏前端走查（Codex 第九轮）：作业列表没取到时不再当成「没做过」重新出题；定位页保存只清自己那一版草稿、做完不把人拉回定位、加目标双击只算一次；切画像清掉项目表单等上一份画像的状态；今日页只在「还没方向」和「继续当前节点」时用服务端建议；「我的项目」没取到时说没取到；各页加载失败给提示和重试；每日分拣双击只算一次、数字用服务器的；用户菜单不叠两个、「继续聊」去对话；刷新停在原来那一栏；手机上对话页单列。回归复查（Codex 第十轮，重跑前九轮全部复现脚本）：项目评阅和论文抓取的排队改用 workpool，断开的请求连同压缩包当场出队；局限引文在全文多处出现时按局限段那一处算；笔记本只认代码单元的真实输出；定位页写完只在原来那次渲染上重画；对话页加载失败也给提示；分拣计数只往大里改；发版时推理强度跟着模型设置一起换。模型输出走查（Codex 第十一轮，用假模型喂坏 JSON、错类型、超长、截断的流）：记忆的证据只认学生自己说的话，短值也要在原话里有着落（年级按阶段和年份比）；模型 op 字段类型不对只拒这一条；先规整整份决策再写库；回包和流的形状逐层核对，流断在半截时用第一次调用的完整回复；评分只认真的 true/false 和 0–100 的有限分数，评分出错不留「已完成、没反馈」的任务；项目评阅的证据文件必须在压缩包里，说做到却拿不出证据降成部分做到；重排全是编的 id 时退回规则候选；开场白先给下一问留位置再截；关服务时也等在评的项目评完。第十二轮：年级按阶段和年份比，原话没说第几年、或者被「不是」否定的不算，英文 freshman/sophomore 也认；常见缩写（AI/人工智能、ML/机器学习）算同一个；值里的数字原话里得有；数字写的年份年龄转成文字收下；流到字数上限（length）也算半截；模型给的落单代理字符先清掉；大整数分数不再 500；引文类型不对的证据整条不要；跨零点倒序回来的分拣计数不倒退。第十三轮：试过让「前者」「好」借助手上一句当证据，Codex 复现「好」能把助手整段话变成学生自述、还能被伪造，撤掉——只回确认时不记、下次再问；数字按数值比（3.70 = 3.7，3.07 ≠ 3.7），中文数带单位也比；「本科第2年」认得，「以前是大一」「不算大二」不算现在的；模型 JSON 嵌套太深当失败回退；stop 之后又来正文或传输出错都不算说完。第十四轮：「第N年」要有「第」（「学了三年」「一年内」不是年级），「去年读大二」「打错了」不算现在的，英文 second-year、「大 2」认得，年级值里夹带的 GPA 要另有着落；数量连着单位比（「每周2小时，总共20周」撑不住「每周20小时」），小时和分钟可换算，全角、范围、课程序号、「四级」都认；「对 X 没有兴趣」不记成兴趣；升级前存在旧键里的作业草稿搬到新键；删一份不在用的画像不清当前画像没写完的陈述。第十五轮：年级和否定两条约束不分值的长短都查；英文年级认否定、阶段看这一处后面的词；「本科二年」认得；「作业错了」「代码写错了」不是改口；双重否定不算否定，缩写要有词界；½ 当 0.5；门、篇、个各算各的；新旧草稿键并存时以新的为准、清空时一起删，搬不过去照样显示；年级正则在长串空白上的平方级回溯修掉。第十六轮：带分数「1½」和「⅓」整体算、「半小时」「一个半小时」认得；紧跟的「不对」「错了」算改口；「本科四年里」是学制不是年级；英文 PhD 优先、否定的那一处不吞后面的年级；量词「个」和项、门互通；成绩单重新识别后旧的「就用这份」撤掉（原来点到旧的会把旧成绩写回去）。第十七轮：「半小时」正则的平方级回溯、「3.7/4.0」里的「7/4」被当成分数，都修掉；对所有记忆辅助函数做了长输入扫描（最坏 50 毫秒）。记忆写入闸门改成按字段核对（server/evidence.py，不再逐条补正则）：模型按字段给结构化的值——年级 {stage, year}、年龄/入学年份/每周时间 {value, unit, per}——代码拿固定的表去原话里对：年级要有一处按表认出同一个（阶段, 第几年），没说阶段的只撑本科；数量要有同一个数、同量纲单位（时分、天周、月年可换算，「一小时二十分钟」算 80 分钟，「十九岁」「20周岁」「24级」都认）；学校查校名简称表（「东北大学」里的「北大」不算）、院系查培养方案别名表（「数学」这类学科词得跟着系/院/专业）、专业查培养方案卡片；兴趣/目标的值要是原话的一段（或常见缩写），不是原样就得有四字以上的公共片段当锚、收下也只算 inferred。一套分句范围规则管否定（含后置的「没兴趣」「没学过」，双重否定不算）、过去（以前、去年、「大一的时候」）、改口，年级/学校院系/数量还排除「想、打算」；其余字段照旧按支撑度，另外查数字、全是虚词的值不再算撑得住、和原话共有的片段全被否定就拒。旧字符串值走同一套核对（存原样），结构化的存规范写法（大二、19岁、每周10小时），形状不对拒 bad_type:value；对话提示改成要这些形状。原来的四个对抗例（「我不想碰机器学习」→兴趣、「我在北京大学读书」→清华、「second-year, not a graduate student」→研二、「我没学过Python」→会）都拒；第十八轮（Codex 复查这次 diff）：数量串夹带别的内容、否定检查换缩写、锚住的兴趣也查数字、一张培养方案卡上的几个专业分开、超大整数、入学年份可以是过去、抵掉的否定不再后置认回、「大二.」、年龄范围两头都留。第十九轮（Codex 独立语料 191 条该收、122 条该拒，c19f9de 上误拒 7、误收 88）：范围改按引文所在的整条消息算（模型只引「大四」时「我不是大四」的否定原来在引文外面）；加「别人的事」（室友、家人、老师、「别人给的建议」）和「假设」两种范围；改口越过「等下」「抱歉」这种插话；「现在」只截断过去、并列的下一分句接着否定、问出来的话题被下一句否定也算、后置否定认表情；学校院系专业要作为学籍出现、长名字里的短名字不算、附中出版社和「法院」不算；入学年份连着入学说、每周时间不算上课；结构化数量不认上下限、范围不挑一头、「每周」不能凭空补；中文小数点、分之、省略末位（两千六）、英文复合数、三段时长、「二三年入学」都认；旧格式数量串只能是这一个量；其余字段英文词整个出现、短值也算支撑度、和原话的说法双向一致、不许多出原话没有的内容；在册 key 规范化后认回原样（base:math_code）；改口词逐个从句首重扫的平方级去掉。现在 Codex 语料误拒 0、误收 3（只提到没表态的「差分隐私」、反义词「时间很多/很少」、能力自述的「独立完成」——契约里这类保留成 inferred 并标 needs_behavior_evidence）；自建语料旧闸门误拒 9、误收 37，新的 0/0。六万四千字的消息、引文重复上千处、十二条 op 一次校验约 0.5 秒。第二十轮（diff 复查）：值带换行不再抛异常、整段引文的否定检查线性、换缩写的片段保留值里的否定、「课题组」不是课、「读研二」是年级。第二十一轮（Codex 新写的独立语料 200 条该收、105 条该拒，87c75f9 上误拒 24、误收 85）：别人的事隔着逗号接着管、到「而我…」为止，「我和同学都…」不算，「替同学问的」「大四的学长」「是爸妈给我定的」算别人的；问句不是自述（问句里「我这个大四…」的自我介绍除外）；条件句的后件、「只是理想状态」算假设；「是讲座的题目」「这个词」只是提到；学校院系专业改成学籍说法的模板（名字前后紧挨着的词、或整句就是名字），「附近、门口、出版、选过课」不算；带单位的数量字符串和结构化值一样严格；入学年份紧挨着入学；「接触过、完成了」算以前；三字以上片段一处对不上就拒；三处长消息的平方级（重叠引文、逐个数量重扫分句、整段覆盖重数）去掉。现在两份 Codex 语料误拒都是 0，误收 2/122、10/105，剩下的是字面规则管不了的：反义词、谓语归属、能力字段里没依据的「精通/独立完成」（契约保留成 inferred 加标注）、旧格式光秃秃的数字。
- 影响文档：docs/DEPLOY.md、docs/CHANGELOG.md、docs/DIALOGUE_CONTRACT.md、.env.example
- 影响模块：server/evidence.py（新）、server/auth.py、server/budget.py（新）、server/userlock.py（新）、server/workpool.py（新）、server/envfile.py（新）、server/wechat.py、server/store.py、server/main.py、server/llm.py、server/memory.py、server/reading.py、server/workbench.py、server/transcript.py、server/dialogue.py、server/singleflight.py、web/js/app.js、web/js/chat.js、tools/backup_db.py、tools/verify/verify_receipt_render.js、server/tests/
- 决策来源：陈浩文（上线前让 Codex 按「真的会有人用」来挑错）
- 登记人：陈浩文

## [2026-10-10] [UX] 粒子首页收进 PR15 视觉引擎

- 变更内容：粒子首页改成 PR15 的发光粒子、每屏一色、散开再聚形、左右对调、钉住文字淡入、跳过与步进提示；文案仍用当前五栏那一版（先聊聊 / 给一个方向 / 交一份小作业），登录后仍是今日/作业/项目/研读/定位。界面 w64。
- 影响文档：docs/CHANGELOG.md、docs/DESIGN_SPEC.md
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-10] [MERGE] 合入 #15 前端优化，修一点就登录过期

- 变更内容：把 PR #15 的今日闭环进度、NBA 服务端措辞、项目「来源」页、骨架占位、评阅通过率条、方向抽屉入门读物合进当前五栏。六站大地图介绍页与已定五栏首页冲突，界面仍用粒子+五栏。登录 401 先重试再核对 `/api/auth/me`，不再一点就踢回登录。函数单实例并发提到 32。界面 w63。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html、tools/deploy/deploy_fc.py
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-10] [MERGE] 合入 PR #16 #17，修研读/定位 user not found

- 变更内容：把 PR #16（会话令牌、访客登录、微信登录可选、删号导出、QIYAN_DB）和 PR #17（流式对话总时限/封顶、关思考、线程池 128、health 报 ephemeral）合进当前五栏 + 粒子首页。PR #15 的六站介绍页与已定 chrome 冲突，界面不收；今日卡片 / 项目来源本地已有。研读先加载公开工具包，阅读卡才带 uid；云上临时库回收导致 user not found / 401 时清会话并请重新进入，不再把工具包显示成「加载失败」。界面 w62。
- 影响模块：server/auth.py、server/wechat.py、server/store.py、server/main.py、server/limits.py、server/llm.py、web/js/app.js、web/js/chat.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-09] [OPS] 补容器与测试环境，对齐部署课

- 变更内容：`ResearchGuide-main` 根目录增加 `Dockerfile` / `docker-compose.yml`。开发用 8100，测试用 8101。云上另部函数 `qiyan-test`，不当生产首页。生产仍是 GitHub Pages + 函数 `qiyan`。没有备案域名，不开 ECS。`docs/DEPLOY.md` 写清三套环境和回滚。
- 影响文档：docs/DEPLOY.md、docs/CHANGELOG.md
- 影响模块：Dockerfile、docker-compose.yml、tools/deploy/deploy_fc.py
- 决策来源：邬程灿（产品部署要求）
- 登记人：助手

## [2026-10-08] [OPS] 默认函数域名会下载网页，改走 OSS 站点打开

- 变更内容：`*.fcapp.run` 会在网关加 `Content-Disposition: attachment`，浏览器就下载 htm。页面改到 OSS 静态网站打开；接口仍走函数计算。前端加 `QIYAN_API` / `apiUrl`，同域时为空。函数计算放开 CORS。界面版本 w38。
- 影响文档：docs/CHANGELOG.md
- 影响模块：web/index.html、web/js/app.js、web/js/chat.js、server/main.py、tools/deploy/
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-08] [OPS] 杭州函数计算公网发布

- 变更内容：当前产品发布到阿里云杭州函数计算（`custom.debian12` + 匿名 HTTP）。按量 ECS 因帐号后付费门槛未开成，所以走按请求计费。公网地址 `https://qiyan-caxplsowco.cn-hangzhou.fcapp.run`。模型走函数环境变量里的 DeepSeek。SQLite 写 `/tmp`，实例回收会丢本地库。密钥只在本机 `.env` 和函数环境变量，zip / `.deploy.env` / `vendor_wheels` 不入库。`arxiv` 条目 URL 先算出 id 再拼，兼容镜像上的 Python 3.11。
- 影响文档：docs/CHANGELOG.md
- 影响模块：tools/deploy/、server/arxiv.py、server/store.py
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-07] [UX] 收掉 AI 口吻，五个房间首屏对准大一痛点

- 变更内容：用户可见文案去掉「问答画像 / 先聊五个问题 / 此刻最值得做的一件事 / AI Research Mentor」这类口吻。今日、作业、项目、研读、定位的页头改成大一会问的那一句。侧栏五栏、粒子首页、右上角画像和方向不动。页脚只留课名来源。品牌小字改成「本科生进科研」。不改 REST。
- 影响模块：web/index.html、web/js/app.js、web/js/chat.js、web/css/styles.css、docs/CHANGELOG.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-06] [UX] 对话和记录收进用户菜单，侧栏只留五栏

- 变更内容：回到备份那一版的粒子首页。侧栏改为今日 / 作业 / 项目 / 研读 / 定位。「任务」改称「作业」。对话、画像、记录不再占侧栏：右上角是当前方向和用户名。点方向进方向树；点用户名弹出画像管理，可切换 / 新建画像，并进入对话、核对或记录。第一次登录仍先进对话。不改 REST。
- 影响模块：web/index.html、web/js/app.js、web/js/chat.js、web/css/styles.css、docs/CHANGELOG.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-06] [UX] 侧栏按进度展开，今日只推一件事

- 变更内容：新用户进门不再并列八个工作区。登录文案改成「先聊几句，再给下一步」。已登录刷新后直接回今日，不再掉回六屏首页。侧栏按进度长出来：先是今日 / 对话；聊出事实后出现方向和记录；选定方向后出现任务；交过任务或对话派了任务后出现项目；有方向且开始做事之后才出现研读和定位。今日去掉「先聊五个问题」的过期口径，每日情报也等研读房间打开再出现。首页第一屏就能开始，不必滑完六屏；登录页不再把「连接模型」和进门并列。核对页改成和对话同一块页头，字段清单默认收起。方向页先给最多三个建议卡片，教程标签等确认方向后再出现。页脚版本戳缩短。不改 REST。
- 影响模块：web/js/app.js、web/index.html、web/css/styles.css、docs/CHANGELOG.md
- 决策来源：邬程灿（用户走查：入口太满、没有一条主流程）
- 登记人：助手

## [2026-10-05] [BUILD] 默认北大（不问学校）+ 说到院系简称时先查库

- 变更内容：用户给了一段实测对话——第一句「我是信管的大二学生」，系统答完一句评价后反问「你是哪个学校的？」；第二句「帮我查一下信管的专业方向」才去查库。两个问题都在这一版修掉。
  - **默认北大**：产品只服务北大学生，学校是默认前提而不是待填的空。`memory.COVERAGE_GROUPS` 的 `school` 改成 `Slot(presumed="北京大学")`（核对页显示为已填、标注默认），`LADDER_PROBE["identity"]` 去掉 `school`，系统提示词明确「默认他就是北大学生，不要问『你是哪个学校』」，他若自己说起别的学校则如实说知识库只覆盖北大培养方案。
  - **身份层漏判**：`LADDER_PROBE["identity"]` 原本只有 `grade/school/major/enroll_year`，**没有 `department`**——「信管大二」把年级和院系都答了，这层仍被判成空着，模型于是接着问一个已经答过的问题。补上 `department`。
  - **说到名字就先查**：新增 `tools.kb_env_facts(uid, message)`，组装环境包时用 `curriculum.mentions()` 找出这句话里出现的库内院系/专业名，**代码先把培养方案原文事实查好**放进 `env["lib_facts"]`（只读、有界：最多 2 个名字 × 4 张卡 × 6 门课，带来源页），并随 `_env_for_prompt` 进入两次模型调用。这是「代码组装环境、模型决定怎么用」，与按层召回记忆同一个模式。
  - 规则版（未配模型）也认这份事实：`_degraded_proposal` 在有 `lib_facts` 时直接报出该院系的专业名单，不再答一句与这句话无关的「先看看你想往哪个方向走」。
  - 提示词补一条引用纪律：**只写库内事实真的写了的东西**（实测模型把只出现在两个专业清单里的《数据库系统》说成「三个方向都有」）；并明确 `school` 只有在他说出北大以外的学校时才写。
- 影响文档：docs/DIALOGUE_CONTRACT.md §3.1.1 + §4（环境包新增 `lib_facts`）、docs/DEPARTMENT_KNOWLEDGE.md §4.6、docs/CHANGELOG.md
- 影响模块：server/memory.py、server/tools.py、server/dialogue.py、server/tests/test_dialogue_libfacts.py（新增 11 条）
- 验证：真模型跑用户那段对话——「我是信管的大二学生」→ 环境包已有 3 个专业名单，回复「你们系三个方向……这学期哪门课你会额外花时间」，**没有再问学校**；「帮我查一下信管的专业方向」→ 三专业 + 学分 + 核心必修课 + 来源页 p378/p387/p396 + 分流以教务为准；「哪个更适合做数据」→ 按必修清单比较且不再把《数据库系统》说成三门共有。三轮 `degraded=false`。
- 决策来源：用户实测反馈（默认北大；「信管」这种简称应该去查数据库，什么时候用库要更准）
- 登记人：助手

## [2026-10-05] [BUILD] 对话 agent 接上院系-专业知识库（问「信管有哪些专业分流」不再答「我没有这个工具」）

- 变更内容：产品内对话问「信管有哪些专业分流」，回复是「我手上没有能检索北大培养方案的工具」——知识库与 MCP 都在，只是没挂进对话的工具 registry。现在把 5 个知识库工具（`major_lookup` / `major_detail` / `match_transcript` / `minor_programs` / `course_lookup`）挂进 `server/tools.py`，**定义只有一份**（`mcp_curriculum.TOOLS`，MCP 与对话共用，`curriculum.py` 是唯一查询层）。
  - 查询层新增：按院系查（「信管」简称表 `DEPT_ALIAS` + 官方名 → 该院全部专业，这才是「有哪些分流」的答案）、`mentions()`（模型把整句当 query 时从原话里抠库内名字）、`codes_for_names()`（课名 → 课号确定性反查，因为北大成绩单只有课名）、未命中时的 `COVERAGE_NOTE`（医学部/深研院/软微不在库里，别把「库里没有」说成「北大没有」）。`mcp_curriculum` 拆出 `run_tool()` 供对话复用，MCP 的 `isError` 语义不变。
  - 对话层：系统提示词补「专业/分流/培养方案/必修课必须调工具，不许说没有工具」与 `tool_intent` 的形状；`_clean_proposal` 收敛字段名同义写法（`name`/`tool_name` → `tool`，`arguments` → `args`）——实测模型写成 `{"name": ...}`，导致整轮被判无效输出而降级；降级模板 `_tool_fallback_reply` 认知识库结果形状（`命中`/`院系排名`），不再误报「没有合适的结果」。
- 影响文档：docs/DIALOGUE_CONTRACT.md §5、docs/DEPARTMENT_KNOWLEDGE.md §4.6、docs/ARCHITECTURE.md §5
- 影响模块：server/curriculum.py、server/mcp_curriculum.py、server/tools.py、server/dialogue.py、server/tests/test_tools_curriculum.py（新增 20 条）
- 验证：真模型跑三问——「信管有哪些专业分流」→ 调 `major_lookup` 回三个专业 + 学分/核心课 + 「名额以教务为准」；「必修课与学分」→ 调 `major_detail` 出席 p396 的 16 门必修课；「学过的高数B/线代B/计算概论A像哪个院系」→ 调 `match_transcript`（课名反查 3 个课号）并如实说公共课认不出院系。三问均 `degraded=false`、`fabrication_risk=false`。
- 决策来源：用户实测反馈（对话不能调用数据库）
- 登记人：助手

## [2026-10-05] [BUILD] 院系-专业知识库（199 专业卡）+ 查询接口 + MCP 服务

- 变更内容：从《北大本科培养方案（2026）》文科卷/理科卷与《辅修双专业培养方案（2025）》抽出结构化知识库：`knowledge/curriculum/`（199 张专业认知卡、198 份培养方案、13k 课程、1077 门被必修课、课号前缀归属 43 个）与 `knowledge/minor/`（98 条辅修/双专业，含替代课程）。定位逐条由模型蒸馏成 ≤40 字并抽检。查询层 `server/curriculum.py`（只读 JSON、不联网、不调模型）：四层检索（主键 → 别名 → 子串 → 近似）、成绩单课号认院系/专业（与留一验证同一 F1 口径，返回「还缺哪几门」）、专业簇与同院兄弟专业、课号反查。接口 6 个（ARCHITECTURE §5）；同一套工具表另有 MCP 服务 `server/mcp_curriculum.py`（stdio 与 streamable-http 两种传输）。留一验证：院系级 Top-1 100%（三种口径）、专业级 Top-1 89%/91%、Top-3 99%。
- 影响文档：docs/DEPARTMENT_KNOWLEDGE.md、docs/ARCHITECTURE.md §5、docs/curriculum_validation.md、docs/curriculum_intros_review.md、docs/curriculum_minor_review.md、README.md
- 影响模块：knowledge/curriculum/、knowledge/minor/、tools/curriculum/（抽取与校验脚本）、server/curriculum.py、server/mcp_curriculum.py、server/main.py、server/tests/test_curriculum.py、server/tests/test_mcp_curriculum.py
- 决策来源：任务表（知识库 / 院系-专业认知）
- 登记人：助手

## [2026-10-05] [MERGE] 任务 2 对话 agent 合回开发线

- 变更内容：把 `feat/dialogue-agent`（任务 2：对话内核「模型决策、代码裁决」、记忆分层与三道闸门、成绩单与绩点、对话↔任务区、SSE、evaluation）合并到本轮开发线。冲突只出现在 7 个文件：`server/main.py`（import 取并集）、`server/store.py`（两边的表与函数都保留，`edges.ref` 走统一 `_MIGRATIONS`）、`web/index.html`（导航取并集：今日 / 定位 / 研读 / 对话 / 方向 / 任务 / 项目 / 记录，版本戳推到 w33）、`web/js/app.js`（**任务区统一到 `taskPanel`**：对话给的任务和树上任务共用一套面板，树上任务多传「路径步骤说明」与「填入演示示例」，完成后的去处由 `onDone` 注入）、`README.md` / `docs/ARCHITECTURE.md` / `docs/CHANGELOG.md`（两边内容取并集）。
- 影响模块：server/main.py、server/store.py、web/index.html、web/js/app.js、README.md、docs/ARCHITECTURE.md
- 说明：合并后 `画像` 视图由任务 2 的「对话 / 核对」两个标签取代（`case "onboarding"` 不再存在）。合并当时 `python -m pytest server/tests` 共 406 条：405 通过，唯一失败是 `test_perf.py::test_deadline_covers_slow_response_headers`（本机让 127.0.0.1 的慢响应头卡约 4.7 秒，断言 <1.5 秒）——同一条在合并前的 `3318243` 上也一样失败，与本次改动无关。（本文件更上面的条目改完后为 417 条，416 通过，唯一失败仍是这一条。）
- 决策来源：本轮开发线整合
- 登记人：助手

## [2026-10-04] [BUILD] 教程蒸馏改为流程树（主干 + 有据才分叉）

- 变更内容：`tutorial-distill` 不再强制 6 步直线。输出 `tutorial-tree-v2`：共用主干，仅当大纲把后面写成并行课/选修轨时才分叉；同一关的两种交卷方式写在 `task.alternatives`。每个节点必须有 `learn` / `task` / `resources`（原链接 + 这一页干什么）。样例：人工智能 `520.20`（卷积 | Transformer）、计算机软件 `520.40`（OS | 编译 | 数据库）。`preview.py` 打 mermaid 和节点卡片。其余 JSON 仍是旧 6 步，校验兼容，等确认后再重跑。未接入 `directions.js`。
- 影响模块：skills/tutorial-distill/、knowledge/tutorials/520.20.json、knowledge/tutorials/520.40.json、skills/README.md、knowledge/tutorials/README.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-03] [DOC] 产品设计提案 v2（HCI）
- 变更内容：新增 `docs/DESIGN_PROPOSAL.md`。判断：学东西越来越便宜，技能和知识不再是瓶颈，学生要赢靠的是用自己的边去差异化。所以主价值是定位与博弈层（§1D：边清单 → 竞争地图（需求 / 供给 / 势头，k≥5、按档、滞后）→ 生态位与定位陈述「我是能做 X 的人，因为 Y」→ 信号表 → 时机 → 冲 / 稳 / 保下注组合，并防羊群）；研究思维层（阅读卡 → 综合矩阵 → 问题阶梯 → 提案画布 → 最小测试）是降成本的引擎、也是边的原材料。节奏分层：每日 ≤10 分钟（arXiv 新论文分拣、雷达变化、一次定位微调），重活按周。双窗口：学生自己的 Agent 是手，启研是地图、标准和账本。IA 五标签：今日 / 定位 / 研读 / 机会 / 作品。按天排期，含验证计划（「Agent 单独 vs Agent + 启研」对照）。是提案，不改代码与契约；采纳哪些在群里定。
- 影响文档：docs/DESIGN_PROPOSAL.md（新增）、docs/README.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-10-03] [BUILD] 方向教程蒸馏 Skill（检索公开大纲，不编步骤）

- 变更内容：新增 `skills/tutorial-distill/`：输入一个方向，检索大学大纲/公开教材，蒸馏成 6 步并写入 `knowledge/tutorials/{id}.json`。不够 2 个独立来源就标 `insufficient`。附 `validate.py`。烟测 3 条：人工智能 `520.20`（MIT 6.390 + MML）、系统结构 `520.30`（MIT 6.004 + Berkeley 61C）、基础医学 `310`（OpenStax A&P 2e + MIT 7.01SC）。尚未接入 `directions.js`。
- 影响模块：skills/tutorial-distill/、knowledge/tutorials/、skills/README.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-03] [BUILD] 方向树与教程同一页切换

- 变更内容：方向区默认是一棵 GB/T 策展学科树（计算机 / 数学 / 心理 / 经济 / 基础医学），统计挂在数学下，医学只做基础医学；三级只留热门切口。同一页用按钮切到教程，顶部写当前方向名，教程随选中的方向变。原有数学、人工智能、认知、经济、统计、系统教程挂到最近的二级；每个二级另有自己的 6 步教程，系统结构不再共用机器学习树。基础医学补了 6 步路径（`docs/paths/基础医学.md`），后两步不预写论文篇名。后端方向目录增加 `med`，任务/项目仍走原有方向代码。
- 影响模块：web/js/directions.js、web/js/app.js、web/css/styles.css、web/index.html、server/planner.py、knowledge/build_paths.py、knowledge/paths.json、knowledge/direction_tree.json、docs/paths/基础医学.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-03] [FIX] 点老师名能打开老师页
- 变更内容：老师页原先叠在节点页下面（z-index 更低），看起来像点不进去。老师页提到节点页上面，并加「返回」。老师名改成真正的按钮。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-03] [FIX] 模型设置只许管理员改；DeepSeek 默认关思考、输出封顶
- 变更内容：`POST /api/llm/connect` 原来任何访客都能调用，可以换掉服务器的密钥，或把所有模型请求转到任意 https 地址；现在设了 `ADMIN_TOKEN` 就要带 `X-Admin-Token`，没设只许服务器本机。「连接模型」弹窗多一个可选的管理员口令。写 `.env` 改为只改三行，不再冲掉别的配置。默认模型改为 `deepseek-flash`（DeepSeek 的 /models 只剩它和 `deepseek-v4-pro`，`deepseek-chat` 已列入停用，目前仍被路由到 flash 非思考）；flash 默认开思考，对 DeepSeek 显式关掉（设了 `LLM_REASONING_EFFORT` 则不关）；每次请求 `max_tokens` 默认封顶 2000。
- 影响模块：server/main.py、server/llm.py、web/js/app.js、.env.example、server/tests/test_llm_guard.py
- 决策来源：陈浩文
- 登记人：助手

## [2026-10-03] [FIX] 性能与资源上限（Codex 审出的七处）
- 变更内容：①交成果的评阅（解压、规则、模型、写库）移出事件循环，进 2 个线程的评阅池，同时排队最多 6 个，满了回 503；②上传边收边数，超过 20 MB 立刻 413，不再先整包读进内存；③.docx 里的 word/document.xml 先看大小和压缩比、读时再截断（8 MB），每个文件解码后最多留 40 万字符；④README 里找文件路径改成线性扫描（64 KB 不到 1 毫秒，原来 16 KB 要一秒），结果与原正则一致；⑤并发的相同请求只打一次上游：arXiv 元数据 / 计数 / 全文、项目来源的 HTTP、北大课程检索（新增 `server/singleflight.py`），`discipline-map/server.py` 也加了十分钟缓存和并发去重，不再每次都起 uv；⑥项目检索改用全站共用的 8 线程池，排队排过截止时间的不再打上游、直接用快照，超时时还没开始的任务取消；⑦阅读卡、定位陈述的「最新一版」查询加带 version 的索引（2 万行从约 0.9 秒到约 20 毫秒）。
- 第二轮（Codex 复审又找出七处）：①相同的论文请求不再各占一个工作线程干等：论文正文、每日情报、交阅读卡改为在事件循环里共等同一个结果（`AsyncFlight`），四十个相同请求时 `/api/health` 照常返回；项目检索在提交任务前就合并相同来源，不再提交只会干等的任务，快来源不会被挤到超时；②外部请求有总时限和字节上限（arXiv、项目来源；新增 `server/limits.py`），慢慢滴数据的服务器拖不住；③上传和评阅分开计数，上传有 60 秒总时限，卡住的上传不再占光评阅名额；④竞争地图的组合稀有度改为两条批量查询（一千人从两千次查询到几次）；⑤一张卡里的引文定位和分节对同一篇论文只预处理一次；⑥arXiv、项目来源、课程检索的内存缓存过期即删、有条数上限；⑦「找项目」按回车不再绕过忙碌状态重复检索。
- 第三轮（Codex 再审六处）：①.docx 去标签改成线性扫描（正则不释放 GIL，放在线程里也会拖住整个服务；66 KB 畸形文件原来 2 秒）；②每日情报把共享的 arXiv 结果或失败原因作为参数交给后续步骤，arXiv 不通时不再每个请求各占一个线程重试，失败结果三十秒内复用；③外部请求的总时限覆盖连接、TLS 和响应头（看门狗到点关 socket），arXiv 限速锁最多被占 timeout 秒；④矩阵冲突按指标分组，每边列前 5 篇另给总数，不再逐对展开；⑤定位陈述的需求统计先按人取截止前最大版本再连回去（1.8 万行修订 3 毫秒）；⑥压缩包在打开前先从目录尾记录查条目总数（含文件夹）和中央目录大小，十万个空目录不再先建十万个对象。
- 第四轮（Codex 四审五处）：①模型调用也走同一套总时限和 2 MB 回复上限，卡住的模型服务不能无限占着评阅线程（超时不重试）；②域名解析放进小线程池限时等，socket 在连接之前就登记（登记的是副本，TLS 包装后照样能到点关掉），解析、连接、握手都在总时限内；③zip64 记录按 zipfile 的方式找（紧挨定位记录之前），前面拼接数据也能核对；有目录尾却核对不了的直接拒收；④竞争地图的需求统计先按陈述指向计数一次、成员判断用集合（一万人从约 0.4 秒到几毫秒）；⑤矩阵每行带完整的「是否在冲突里」标记，高亮不再只看每组前 5 篇的预览。
- 第五轮（Codex 五审五处）：①课程检索也改成在事件循环里共等一个结果，相同检索不再各占一个工作线程；②同一主机的域名解析共用正在进行的那一次，一个解析很慢的主机最多占一个解析线程；③压缩包里的纯文本只读开头（够截出上限即可），每个文件读完立刻截断再存，不再整份解压、整份解码攒到最后；④组合稀有度先把每个同学压成「和我重合的边」的位图、相同位图合并后再数组合（120 条边 × 5000 人在 0.5 秒内），只返回最少见的 20 组另给总数；⑤arXiv 只拿到摘要时记下重试时间：确实没有 HTML 版隔一周，临时失败（超时、断网、5xx）十分钟后再试，不再把半份论文永久缓存。
- 第六轮（Codex 六审四处）：①域名解析记着有几个请求在等，最后一个等的人超时走了就取消还在排队的解析，过期的活不再占住线程；②响应提前断开（收到的少于声明长度，或正文页尾没有 </html>）当作网络错误，不会把半份论文当成正文永久缓存；③到期的摘要版只重试正文、沿用已有的标题和摘要，刷新失败照样返回缓存并按临时失败退避十分钟；④组合稀有度改为每条边一个「哪些同学有它」的位集，一对边的人数用按位与后数 1 得到（每人 60 条重合边、5000 人在 0.5 秒内）。
- 第七轮（Codex 七审三处）：①notebook 不再用 json.loads 把输出全建成对象：结构简单的照旧解析，复杂的按记号线性扫描，只取每个单元的 source、只看 outputs 是否为空（十五万个输出对象时峰值约 8 MB，原来七八十 MB）；②同一篇论文的引文索引在并发提交时只建一次（缓存加并发合并，二十四张卡同时交只建一次）；③组合稀有度逐行读同学的边（只选需要的列、用元组），边读边置位，不再先攒几十万个行字典（5000 人 × 60 条边约 0.3 秒、峰值约 3 MB）。
- 第八轮（Codex 八审四处）：①②notebook 去掉自写扫描器（没闭合的字符串会让它退化成平方时间，还会放过语法错误），改回标准库 json 并加钩子：非单元对象解析完立刻换成占位、数字一律解析成 0，语法照旧完整校验，坏文件判不合格；十五万个输出对象约 0.1 秒、峰值约 10 MB；③交阅读卡前在事件循环里共等「取原文 + 建引文索引」，四十张卡同时交同一篇论文时评阅线程不再干等；④组合稀有度的规范化缓存最多记 4096 条，各不相同的描述不再全攒在内存里。
- 自查修复（十处）：
  - 论文缓存先写临时文件再原子替换，坏文件当作没有缓存；
  - 共享的活按种类分开线程池（arXiv、建索引、课程检索），等 arXiv 限速锁的不再拖住别的；
  - 模型调用改为「多久没数据」加 150 秒总时限，DeepSeek 排队时发的空行不再被当成卡住；
  - 上传改为「20 秒没数据」加 5 分钟总时限，慢网也能交完；
  - 共享的项目来源任务按所有等它的检索里最晚的截止时间算；
  - 引文索引按总字数（八百万字）留，几篇论文同时交卡时不会在评阅前被挤掉；
  - 性能测试的计时门槛加宽，模拟的阻塞拉长，在慢的 CI 机器上也稳；
  - 刚读过的论文在内存里留一分钟，一次交卡不再从磁盘读三次；
  - 一个看门狗线程管所有请求的截止时间（不再每个请求一个 Timer 线程），并加测试守住依赖的 http.client 内部细节；
  - 压缩包中央目录上限提到 1 MB，超了单独说「文件名太大」。
  - 另修一处：看门狗到点关掉 socket 后，没声明长度的响应会被当成正常读完，现在判为超时。
- 第九轮（Codex 九审三处）：①notebook 解析前按字节数限额（8 MB、约 15 万个 JSON 值），超了不解析、提示清空输出再交；钩子只留顶层和单元，带 source 键的输出也丢掉（限额以内最坏情况峰值约 30 MB）；②引文定位按「一个字加组合符号」逐段规范化并记原文位置，连字、全角字符不再让位置错开、把引言报成局限段；③每人最近一次检索结果改用两小时过期、最多 2048 人的缓存。
- 第十轮（Codex 十审三处）：①论文正文、每日情报、交卡在缓存命中时直接给，不进 arXiv 线程池排队（原来四个慢的取数占满线程时，已缓存的论文也得等）；②太大没解析的 notebook 不再按正则猜「有输出」，不给分，并提示把结果导出到 results/；③引文和论文的规范化走同一条逐字流程，每个产出的字都带原文位置（「İ」转小写是两个字、分开写的韩文字母会合并）。
- 影响模块：server/main.py、server/submission.py、server/arxiv.py、server/project_adapters.py、server/pku_adapter.py、server/projects.py、server/store.py、server/singleflight.py（新增）、server/tests/test_perf.py（新增）、discipline-map/server.py、server/limits.py（新增）、server/quotes.py、server/positioning.py、server/reading.py、server/llm.py、web/js/app.js、server/tests/test_projects.py
- 决策来源：陈浩文（Codex 审查）
- 登记人：助手

## [2026-10-03] [BUILD] 研读层与定位层的第一条纵切片（设计提案 §1C、§1D）
- 变更内容：新增两个工作区。「研读」：领域工具包「大模型评测与基准」（10 篇 arXiv 论文、7 个 GitHub 数据集、11 条逐字核对过的作者自述开放问题、7 个矩阵维度）；阅读卡工作台左边是 arXiv 正文（可按节跳转、选中原句一键引用），右边 7 栏，引文必须逐字出自原文、局限必须出自局限 / 讨论 / 结论段，「主张 / 假设 / 我会改什么」不能抄原文；可下载 AGENTS.md 交给学生自己的 Agent，并在决策日志里逐条表态；三张卡过线后出综合矩阵，空格、空列、方向相反的冲突由规则标出。「定位」：边清单（已证明的从账本自动导入，自述的自己加，含「信息源」一类）、竞争地图（需求只数有投入的人、滞后一周、k≥5、池子不够写「数据不足」；供给如实写「未知 · 去问」；势头用 arXiv 近 12 个月相对分类整体的增长，离线算好写进工具包；不提供冷门排行）、定位陈述「我是能做 X 的人，因为 Y」的规则检查（不打分）、冲 / 稳 / 保 下注组合（最多 3 个，集中风险提示），以及信息源地图（`knowledge/channels.json`：六个方向加进组信息共 90 个信息源，中文圈 / 英文圈并列，写明信号、具体偏差、频率与可达性，5 条标「未核实」；学生标「我常看」成为一条边，并看到自己那一圈之外的盲区）。学生的方向还没有工具包时如实说明，并指向信息源。「今日」多一块每日情报：分拣几篇当天 arXiv 新论文（留 / 过 + 一句理由）和至多一句定位微调。全部规则判定，不调用模型。
- 修复：arXiv HTML 正文只取 `<article>`，不再混进横幅和反馈弹窗；公式保留 TeX。
- 影响模块：server/arxiv.py、server/quotes.py、server/reading.py、server/positioning.py、server/kit_momentum.py、server/store.py、server/main.py、server/tests/、knowledge/kits/、knowledge/channels.json、web/js/app.js、web/css/styles.css、web/index.html
- 影响文档：docs/READING_POSITIONING.md（新增）、docs/ARCHITECTURE.md、docs/README.md
- 决策来源：陈浩文（依据设计提案 v2）
- 登记人：助手

## [2026-10-03] [BUILD] 当前学期公开课快照：每个节点本地检索，老师可点
- 变更内容：把 2026-2027-1 全院系公开课翻进 `knowledge/catalog/`（`courses.json` / `teachers.json` / `meta.json`）。检索先读这份快照，不再每次打教务。方向树上每个节点都用节点名做关键词查课，对不上就空着。老师名可点，看到本学期教了哪些课；简介只在 OpenAlex 对上北京大学任职时才写。生成脚本：`server/catalog_build.py`。
- 影响模块：server/catalog.py、server/catalog_build.py、server/pku_adapter.py、server/main.py、web/js/app.js、web/css/styles.css、knowledge/catalog/、docs/ARCHITECTURE.md
- 决策来源：邬程灿
- 登记人：助手

## [2026-10-02] [BUILD] 方向树画成「6 步主干 + 原有节点」（任务 4 路径进树）
- 变更内容：新增 `GET /api/paths`；前端启动时读路径，把数学、人工智能、认知、经济四棵树组成「6 步主干 + 原有节点」：主干竖排成一条粗线、带序号，原有概念节点按 `docs/paths/改树建议.md` §3/§4/§7/§8 的改动清单挂到对应步骤右侧（`web/js/app.js` 的 `CHAIN_ATTACH`），`FIELD_TREES` 原文不动；统计、系统不变。点主干看「先弄懂什么 / 做完怎样算过了」，并能直接「找能交出这一步的项目」；任务页标出当前节点属于第几步、这一步要交什么；「项目」页按树上的位置预选步骤。修复：第一次在方向区接过服务端已选方向时没有按已交任务对齐进度。
- 影响模块：server/main.py、web/js/app.js、web/css/styles.css、server/tests/、docs/DESIGN_SPEC.md、docs/TASK3_PROJECTS.md、docs/paths/README.md（一行）
- 决策来源：陈浩文（依据任务 4 改树建议）
- 登记人：助手

## [2026-10-02] [BUILD] 做完之后怎么接：今日、任务完成、项目评阅都落到下一件事
- 变更内容：今日的建议按「有项目没改完 → 选了项目没交 → 交过 3 次小任务该找项目 → 继续当前节点」排，每条一个主动作加一个备选；改项目时直接引用上次评阅的「下一步」。任务完成页在交够 3 次（或走完方向）后多一个「学完一块了，找个项目练手」。项目页每次评阅和上一版比多了几条；五条全做到标为「做完了」，给「找下一个项目（难一档）」。规则表见 TASK3_PROJECTS §8。
- 影响模块：web/js/app.js、web/css/styles.css、server/projects.py（五条全做到时状态记为 done）、server/tests/、docs/TASK3_PROJECTS.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-10-02] [BUILD] 整条链路的接口测试 + GitHub Actions
- 变更内容：新增 `server/tests/test_api_flow.py`：用真实 FastAPI 应用走一遍 登录 → 五问 → 核对（改一条、划一条）→ 选方向 → 节点任务 → 提交反馈 → 找项目（实时来源全部「连不上」，走快照）→ 交压缩包 → 记录 → 软删，外加未知用户 / 方向的拒绝。数据库放临时目录、不联网、不调模型。仓库根目录新增 `.github/workflows/tests.yml`：每个 PR 和 main 上的提交跑后端测试、`node --check` 前端、并检查 `knowledge/paths.json` 和 `docs/paths/` 是否一致。
- 影响模块：server/tests/、.github/workflows/、README.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-10-02] [FIX] paths.json 接上认知、经济路径；清掉 CHANGELOG 里残留的合并冲突标记
- 变更内容：#5 加了 `docs/paths/认知.md`、`经济.md`，但没有重新生成 `knowledge/paths.json`，main 上 `server/tests` 有一条测试失败。现在跑过 `knowledge/build_paths.py`，四个方向都有 6 步路径，「项目」页的认知、经济也按路径步骤选。`改树建议.md` §5 的项目形态是为数学、人工智能写的，生成时只给这两个方向带上。另外删掉本文件里 #3 合并时留下的一行 `=======`。检索加 12 秒总时限：慢来源（今天产业命题要 20–35 秒）先用快照、后台跑完写缓存，冷启动从 40 多秒降到约 17 秒，缓存热了约 5 秒。
- 影响模块：knowledge/paths.json、knowledge/build_paths.py、server/projects.py、server/project_adapters.py、server/tests/test_projects.py、docs/TASK3_PROJECTS.md、docs/CHANGELOG.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-10-01] [BUILD] 「项目」接上任务 4 的方向路径
- 变更内容：新增 `knowledge/paths.json`（由 `knowledge/build_paths.py` 从 `docs/paths/*.md` 生成，目前有数学、人工智能）。「项目」页对有路径的方向改为「你在路径的哪一步」，按这一步的过关标准找项目；检索接口多一个可选字段 `path_step`，`context` 多返回 `paths` 和默认步骤。没有路径的方向不变。甲补上认知、经济后跑一次生成脚本即可接上。登录页「五个工作区」改为六个。
- 影响模块：server/projects.py、server/main.py、skills/project-scout/SKILL.md、web/js/app.js、web/css/styles.css、knowledge/、docs/TASK3_PROJECTS.md、docs/ARCHITECTURE.md、docs/paths/README.md（一行）
- 决策来源：陈浩文
- 登记人：助手
## [2026-10-02] [CHORE] 交付清理：删密钥与用户数据、夹具改合成、补任务 2 交付文档

这一轮不是加功能，是**让这个文件夹能安全地发给别人**（原话：
「把该删的过程文件删掉，把 apikey 等敏感数据删掉，把我的用户数据删掉，
按照开发规范留一份文档介绍我的部分的修改情况，确保我能把整个文件夹压缩发给别人」）。

### 删掉的

| 项 | 是什么 |
| --- | --- |
| `ResearchGuide-main/.env` | **真实 DeepSeek 密钥**。从未进过 git（只跟踪 `.env.example`） |
| `ResearchGuide-main/server/data/` | `demo.db`：一个真实用户 id + 画像 + 成绩单 + 全部记忆 + 明文密钥 |
| `eval/last-report.json` | 构建产物 |
| `审计与方案/`（仓库外） | 60+ 过程文件：审计稿、方案草案、探针、一次性载荷 |
| `docs/paths` | 1 字节的垃圾文件 |

全量历史上扫过 `sk-[0-9a-f]{20,}`（`git rev-list --all` 的每个 blob）：**零命中**，
密钥只在上面那两个工作区文件里。

### 换成合成数据的

真实成绩单曾经出现在 5 个地方。全部替换，并**保持学分和成绩不变**，
所以数字断言继续成立（通过学分 10.0 / 绩点学分 8.0 / 绩点 3.8020）：

- `server/tests/test_transcript.py` 的 `SAMPLE` 和 `REAL`
- `server/tests/test_transcript_in_dialogue.py` 的 `REAL`
- `tools/verify/verify_served.py`（原来直接读一个 `user-transcript.txt`）
- `tools/verify/verify_transcript_sse.py`、`verify_receipt_render.js`
- `eval/dialogue_cases.json` 里用户的自述、`test_memory.py` 的画像夹具

理由不只是隐私：**测试夹具不该来自某一个人的数据**。合成夹具更通用，
也不会让下一个读代码的人以为这是某个真实用户。

> 顺带核实：「北京大学」在 `knowledge/`、`skills/pku-course/`、`discipline-map/`
> 里是**公开参考数据**（含一份国标 `gbt13745_source.txt`），不是个人数据，保留。

### 搬进来的

`审计与方案/` 里 8 个有长期价值的验收脚本搬进 `ResearchGuide-main/tools/verify/`，
并把写死的仓库外路径改成仓库内相对路径。

**为什么必须搬**：`test_render_layer.py` 以前找不到脚本就 `skip`，
而脚本在仓库外 → 别人 clone 下来这 4 条**永远是跳过的**。那等于没有这层保护。

### 新增文档

[`TASK2_DIALOGUE.md`](TASK2_DIALOGUE.md)：任务 2 交付说明（闭环、记忆六层与三道闸门、
九大类、成绩单与绩点、对话↔任务区、接口、跑法、边界）。
**取代** `审计与方案/05-任务2交付说明.md`（那份只覆盖内核、早于记忆层/成绩单/前端）。

其中第 12 节「我改过别人代码的地方」请重点看——含一处**尚未在群里定**的接口冲突：
任务 3 用 `stage=0..3`、任务 4 用 `stage=1..6`，同名字段两套语义，
建议拆成 `practice_stage` 和 `path_stage_id`。

### 影响文档 / 模块

`docs/README.md`（导航表 + 测试数 61→271 + 测试入口）、`server/memory.py`、
`docs/CHANGELOG.md` 一处示例、`server/tests/` 三个夹具文件、
`tools/verify/`（新增目录）、`.gitignore`（已覆盖 `.env` / `server/data/`，未改）。

### 验证

`python tools/verify/run_existing_tests.py` → **271 passed**；
4 个渲染层脚本全绿；`verify_guards_can_fail.py` 8 条回滚全部证明守卫会红。

### 决策来源

用户直接指令（本轮）。**登记人：任务 2。**

---

## [2026-10-02] [FEAT] 能力从成绩单确定性推导（transcript:*），取代模型凭一段话编判断

接着上一条做。用户批准的方向：「让能力结论自动从成绩单推出来」。

### 为什么这是 `base:code` 事故的正面修法

那起事故的根不是提示词写得不好，而是**能力这件事根本没有底稿**：
模型读到「Vibecoding」只能自己编一个判断，于是写下
`'修过计算概论B(Python)93…课程基础扎实（依赖AI生成）'`——
一半是真实课程，一半是它自己加的评语，混在一条 `value` 里存成一个 `declared` 事实。
事后谁也分不清哪部分有依据。

现在成绩单在 `enrollments` 表里，能力**可以**是推出来的。所以：

| | 来源 | 谁写 | 长什么样 |
| --- | --- | --- | --- |
| `capability:*` | declared / inferred | 模型或用户 | 「我会一点 Python，只会照教程抄」 |
| `transcript:*` | **derived** | **只有代码** | 「修过 3 门数学类课程，学分加权平均 89.5：概率统计 (B) 96、线性代数 (B) 88、高等数学 (B) (二) 86.5」 |

两个 namespace 分开是有意的——混在一个 key 上就又会分不清硬证据和自述。

### 两条设计红线

1. **不写判断，只写事实。** `value` 里只准出现「修过哪几门、考了多少、加权平均多少」。
   绝不出现「基础扎实」「能力偏弱」——那是我们的推断，写进「事实」就再也分不清了。
   难度判断交给读到这条事实的模型自己下。有测试逐词守着（`test_derivation_reports_facts_not_judgements`）。
2. **只统计通过的课。** 挂了的高等数学不能算「修过数学类课程」，那句话会误导人。

关键词只是**匹配模式**，只在用户真实录入的课程名上做子串匹配，匹配不上就不产出任何东西——
没有一条课程是我们编的。推导是幂等的、不依赖历史，同样的成绩单永远得到同样的结论。

挂到 capability 层（`LAYER_BUDGET` 4 → 6），并在层内排序给 derived 加 0.9 的权重：
坑位不够时**硬证据应该压过自述**。0.9 刻意小于 1.0，所以用户直接问到某条自述时它仍能排前面。

### 「用户是最终权威」需要三处让路（这一版最初全漏了）

`sync_transcript_facts` 会在成绩单变动后重新推导。第一版实现里有两个会**覆盖用户意志**的 bug，
都是写测试时逼出来的：

1. **用户删掉的会自己长回来。** `user_reject` 我按 fact id 记，但重新推导可能新建一行、
   id 就变了，于是拒绝失效。改成**按 key** 记（从 revision 反查 key）。
2. **用户改过的会被改回去。** 我为了「让路」把 user_edit 的 key 从 desired 里 pop 掉，
   结果**撤除循环**把它当成「推导不出来了」直接 retract——用户刚改的那条被删了。
   现在是独立的 `protected` 集合：受保护的 key 不加、不改、也不撤。

代价不对称：让路只少一条注入的结论（成绩单还在，`transcript.summary` 工具照样查得到）；
不让路的代价是他发现有一条东西删不掉、改不了。所以让路。

### 界面

- 记忆面板：`source=derived` 的卡单独样式，**摊开依据**（「依据：概率统计 (B) 96、线性代数 (B) 88」）。
  不给「改」只给「不算」——逐字修改一条机械推导出来的事实没有意义，
  用户对它唯一合理的表态是否掉它。那个「不算」会被记住。
- 核对页粘贴成绩单时先给**预览**（会记下哪几条结论），落库前就能发现我们读错了课。

### 验证

- 离线 163 通过（本轮 +2，推导相关共 15 条）
- 19/19 评测通过（registry doc 和 env packet 都变了，所以必须重跑）
- `verify_derived_render.js`：真实 chat.js 源码 + 真实 `/api/memory` 载荷，25 项断言全通
  （依据必须来自**该条自己的 evidence**，不是写死几个课程名对样例；含转义）
- `probe_derived_live.py`：live 跑真实对话。模型引用具体课程和分数，
  并给出**有分寸**的判断：「你目前修过的数学课集中在工科数学这一档，
  还没有实分析、抽象代数这类更硬的课……够用，偏统计这一侧比较强，但还没被真正难的数学检验过」，
  任务锚在概率统计 96 / 计算概论 92 上，且明确要求用真实数据。
  没有出现无依据的评语；没有试图去写 `transcript:*`。

- 影响文档：`docs/DIALOGUE_CONTRACT.md` §5.2（新增推导一节）
- 影响模块：`server/transcript.py`（推导规则）`server/memory.py`（`sync_transcript_facts`）
  `server/schemas.py`（新增 derived / user_edit 来源）`server/main.py` `server/dialogue.py`
  `web/js/chat.js` `web/js/app.js` `web/css/styles.css`
- 决策来源：用户批准「让能力结论自动从成绩单推出来」

---

## [2026-10-02] [FEAT] 成绩单单开一张表 + 树洞格式自动识别 + 绩点计算；核对页摊开九大类字段

用户指令：「成绩单可以单开一个表，同时，参考这个 GitHub 项目，加入树洞成绩粘贴格式自动识别+
绩点计算的功能」「同时建议把我给出来的这些空都给用户展示出来，就在核对也分列展示，
表示填好这些信息可以让科研导向更准确」。

### 1. 成绩单进独立表（`enrollments`）

一门课一条：`course / grade / credits / term / kind / status`。

**为什么不塞进 `facts` 的 key-value**：一个人 20~40 门课 × 四个字段，
塞进 `value` 会变成一坨文本——**正是 `base:code` 那起事故的形态**，
既撑不住引文校验（闸门②判不了哪部分有依据），也没法按学期/成绩/关键词查。
`facts` 里只放**从成绩单推出来的能力结论**，底稿留在表里。

画像隔离同步覆盖：`_dump_live` / `_clear_live` / `_RESTORE_COLS` 三处都加上了，
并加了 `test_enrollments_survive_a_portrait_roundtrip`
（`affects` 那一列就曾在硬编码列名里被静默丢掉，同一个坑不踩第二次）。

**写入语义是「快照」不是「流水」**：`replace` 整表替换，`append` 跳过完全重复的。
逐条追加会让重复粘贴的课把绩点算重。

### 2. 树洞 / 教务粘贴格式自动识别（`server/transcript.py`）

规则和公式**移植自**开源计算器
[PKUMuZi/pku-gpa-calculator](https://github.com/PKUMuZi/pku-gpa-calculator)（MIT）。
格式：`学分 / 「学分」 / 课程名 / (课程性质)* / 成绩`，学期标题形如 `25-26学年度1学期`
且**向下贯穿**到后面的课。

**验证方式是重点**：没有手写样例对着自己的假设测，而是
`审计与方案/verify_transcript_vs_reference.js` 从上游 HTML 里**抠出它真实的解析函数**、
拿它**自己的示例成绩单**跑出基准，再用 `verify_transcript_my.py` 跑我们的实现**逐条对**。
结果：**50 门课、8 个学期、总绩点 3.838950、均分 91.617308、通过学分 110、
计 GPA 学分 104 —— 全部一致**。这个脚本里没有一个字符是我猜的。

另外加了两件上游没有的：解析失败**说得出原因**（「没识别到学期标题」），
以及未知行计数上报，而不是静默丢数据。

**绩点**：`GPA(x) = 4 − 3(100−x)²/1600`。

**一处有意与上游不同**：低于 60 分我们记 0，上游不设下限——
上游写法在 50 分时会算出 `-0.6875` 的**负绩点**，累加进平均会拖低别的课。
负绩点没有意义，北大教务口径也是不及格记 0。
做成 `GPA_FLOOR_AT_ZERO` 开关，改回上游行为只需一个常量。

**汇总口径**：通过学分含「合格」；计 GPA 学分**只含百分制**，
`W`/`I`/`合格`/`不合格` 不进分母。把「合格」当 60 分算会把绩点算低。

### 3. 核对页摊开九大类字段（用户反馈）

`GET /api/me/coverage` + 核对页的 `coverageBoard()`：九大类各一列，
每格标已填（给值）/ 空（给「填了有什么用」），顶部「已填 N / M 项」。

设计取舍：**每个空格必须带一句 why**。空格本身是邀请，
但只列空格不说收益就变成一张逼人填的表——所以 `why` 写在
`memory.COVERAGE_GROUPS` 里，和字段定义放在一起，不散在前端。
已填的格子**不重复**显示 why，免得整页都是废话。

点空格跳去对话而不是就地开输入框：这些字段大多需要上下文
（成绩单要粘贴、方向要聊），摆一排输入框只会让人填一半就走。

### 4. `transcript.summary` 只读工具（P3 落地）

默认**只回汇总**，要具体课程必须带 `keyword`。
成绩单是 §4 优先级里的 P3，整份进上下文会挤掉真正决定判断的那几条，
还会逼模型自己在几十条里找——找错一条就会说出用户一眼看穿的话。
「底稿留在表里，结论才进上下文」和「不塞进 facts」是同一个决定的两面。

### 5. 修掉的 bug

- **`/api/me/transcript` 提交永远 400**：我先判 `courses` 为空、再从 `text` 重新解析，
  于是只传原文（最常见的情形）被当成「没给课程」。**单元测试没覆盖这条路径，
  是端到端跑真实成绩单抓到的。** 已把顺序改成先解析再判空。
- 我自己在测试里写错两处，也都是被测出来的：`线性代数 (B)` 在两个学期各出现一次
  （我用字典取值拿了后一条）、`缓考` 解析时归一成 `I`（和上游 `normalizeScore` 一致）。

**验收**：148 个离线测试通过（新增 35 条）；19/19 评测用例通过；
`审计与方案/verify_transcript_api.py` 走真实接口 9 项全通（含画像隔离往返）；
`审计与方案/verify_coverage_render.js` 用**真实 app.js 源码 + 真实载荷**
渲染字段清单，13 项断言全通（含 HTML 转义）——没有无头浏览器，所以写了 DOM shim。

- 影响文档：`docs/DIALOGUE_CONTRACT.md` §5（新增工具 + §5.1/§5.2 成绩单与格式）
- 影响模块：`server/transcript.py`（新）`server/store.py` `server/memory.py`
  `server/tools.py` `server/main.py` `web/js/app.js` `web/css/styles.css` `web/index.html`
- 决策来源：用户指令（单开表 + 参考开源计算器 + 核对页摊开字段）

---

## [2026-10-01] [FEAT] 记忆改成按「时间跨度」分层 + 写入三道闸门 + 用户可改可删

**起因**：用户手测第二轮的反馈。原话两条：一是「AI 记忆不是很结构化？以后数据量多了全量注入吗？
是不是可以分成几个部分去处理？」，二是「这里现在的区分标准太不专业了，注入上下文照样会出错，
先判断一下这种记忆业界常用的通则是什么，我们这里需要个性化修改哪些」。
另有一条追问：「还有从实践跨度角度的呢，比如手头正在做什么项目？以及怎么让AI知道
哪些可以写进记忆，哪些不需要进入记忆」。

**先对齐业界通则**（读了原始论文，不是凭印象）：
- **CoALA**（arXiv:2309.02427）：工作记忆 + 长期记忆分 **episodic / semantic / procedural**。
- **Generative Agents**（arXiv:2304.03442）：记忆流 + **reflection**（合成为更高层结论）
  + 检索按 **recency + importance + relevance** 三信号加权。
- **MemGPT**（arXiv:2310.08560）：分层记忆、在有限上下文里做数据搬运。

**我们和通用聊天记忆不一样的地方**（这决定了不能照抄）：
1. 我们是**画像**不是聊天记忆——每条记忆都要回答「它改变哪个决策」，所以排序依据是
   **决策影响**而不是语义相似度。
2. 主体是**会变的人，而变化本身就是信号**——所以 semantic 记忆要**按变化速度再切一刀**，
   这一刀同时决定了「什么时候该重新问」。
3. 诚实标准更高——必须硬区分「他说的 / 我们推的 / 做出来的」，推断不得当自述复用。
4. 结论的过期速度不一样。

**做了什么**：
- **记忆分层**（`memory.LAYERS`）：`identity 身份` / `practice 当前实践` / `capability 能力起点` /
  `experience 经历积累` / `interest 倾向` / `constraint 临时约束`。层同时管三件事：
  提问顺序、注入预算、什么时候重新问。`LAYER_BUDGET` 给出每层名额。
- **补了缺失的 key**：`school`（原来根本没有——「某某大学信息管理系」里的「某某大学」
  被静默丢掉了），以及 `current:course` / `current:project`（当前实践，`default_ttl_days=90`
  自动过期，避免半年前的项目被当成「他正在做的事」）。
- **召回从「平表取 top-8」改成「按层给预算」**：全局抢坑位时一条课程成绩可能把「他的方向」
  挤出去，而且没有任何信号告诉模型记忆被截断了。层内相关度**只认多字词命中**——
  原来那个「单字命中 0.05/字」在中文里等于噪声，一句话的每个字都会命中一堆无关事实。
- **写入三道闸门**（默认不写，要写就举证），出发点是**代价不对称**：
  漏写一条，下次再问一遍就行；写错一条，它会**静默污染之后的每一个决策**，而且没人会发现。
  1. **在册**：key 命中 registry，且允许该 source。
  2. **引文撑得住值**：`evidence_quote` 要支撑写进去的 value。算「value 的内容有多少能在
     引文里找到」：`≥0.8` 收；`0.5~0.8` 收但**降级为 `inferred`**；`<0.5` 拒。
     value 不足 8 字跳过（`AI`、`大二` 是规范化缩写，逐字要求会误杀）。
     阈值是拿真实例子校准的（`审计与方案/calibrate_support.py`，9/9 符合预期）：
     真实事故那条 **0.29**，正常值 **0.67–1.00**，分得开。
  3. **说得清改变哪个决策**：`affects` 必填且命中白名单（`task_difficulty` `task_kind`
     `direction_choice` `question_next` `course_pick` `pace` `feedback`）。**填不出来就没资格进记忆。**
- **中文 slug 放行 + key 规范化**：模型写 `current:选课数据大作业` 是很自然的，
  只放行 `[a-z0-9_]` 会把真实信息整条丢掉（同一个错犯过两次，第二次是中文）。
  `canon_key` 把空格/下划线统一成连字符、削掉首尾分隔符。注意 ASCII-only **从来没真正
  解决同义重复**（`ai`/`AI`/`人工智能` 照样三条），同义不同词靠 single 基数的 supersede 兜。
- **用户可看可改可删**（`GET /api/memory` 按层返回 + 复用已有的 `PATCH/DELETE /api/me/facts`）。
  改一条会**升级为 `user_edit`、置信度 1.0、留 revision、memory_version+1**；删是软删
  （置 `retracted`）不硬删。原来这两个接口是裸 `update_fact`，既不升级来源也不留 revision，
  用户改完的记忆和模型推断的在库里长得一样。对话界面右侧按层分列展示，每条能直接改删。
  - 没有新开一套接口——**刻意复用**：前面 `actions` 和 `tasks` 两套任务系统不连通
    就是新开一套的代价，不再犯。

**修的真实 bug**（都是这轮验证时暴露的）：
- `server/main.py` **没有 `import memory`**，新加的 `/api/memory` 直接 500
  （`py_compile` 查不出 `NameError`）。
- `store._load_live` 的 facts INSERT 是硬编码列名，**新加的 `affects` 没跟上**——
  切画像时这一列会被静默丢掉。已补列 + 加回归测试。

**prompt 同步改了两处**，否则模型不写 `affects` 会导致记忆全被静默丢掉：
- `_registry_doc()` 改成按层列 key，并在系统提示里解释「当前实践」是什么。
- `_PROPOSAL_RULES` 加了「这些该写，别漏」正面清单。**这一步是被评测逼出来的**：
  只写「默认不写」的负面清单，模型连「这周只有十分钟」这种典型临时约束都不写了
  （`rejected_ops` 为空，说明是它主动不写，不是被闸门拦的）——和之前
  `action_has_landing` 那次一样，单边规则会让模型变得不敢出手。

**提问阶梯**（用户反馈第 1 点：「我只说了个大二，怎么跳过学校先问课程了……不是写个硬规则用的，
只是从这个例子让你了解现在追问不自然」）：
- 把层的顺序直接当作阶梯：身份 → 当前实践 → 能力起点 → 经历积累 → 倾向 → 临时约束。
  跳级就是不自然——还没弄清他是谁，就问到了他的能力细节。
- 代码侧 `memory.ladder_state(uid)` 算出「哪层已经有数、最早的空白层是哪一层」，
  注入环境包。**刻意没做成硬闸门**：问题是一句自由文本，靠关键词判它「在问哪一层」很脆；
  本轮只有两次模型调用，没有余量再分类一次。所以设计是**代码给真相、模型定措辞、评测管结果**。
- 阶梯只管「你想了解他时问什么」，不是每轮都必须问。上下文在别处（追问概念、刚说急事）
  就顺着走——prompt 里明确写了这一条，防止它变成一张问不完的表。
- 补了两条评测：`ladder_asks_earliest_gap`（只说大二时必须问到身份层）、
  `ladder_no_skipping_to_capability`（不许跳去问课程/编程/数学）。

**又抓到两个自己埋的 bug**（都是「把真东西跑一遍」才暴露的，grep 关键字查不出来）：
- **`build_env` 里的 `limit=10` 把分层预算架空了。** 召回是按层序拉平的，
  这个总数会把靠后的层截掉——等于把平表抢坑位换了个地方犯。
  实测：9 条能力 + 9 条当前实践时，**倾向和临时约束双双消失**。
  现在总上限由各层名额之和决定，只留一个很大的 `RECALL_HARD_CAP` 防异常数据撑爆上下文。
  加了回归测试 `test_env_packet_keeps_tail_layers_even_when_early_layers_are_full`。
- **`layer_hint` 挂错了对象**：它描述的是「这一层」，却被塞进了每条 fact 里，
  前端读 `L.layer_hint` 取不到，那行说明永远不显示。
  这个是靠**把 chat.js 里真实的渲染函数抠出来、用最小 DOM 喂真实载荷跑一遍**才发现的
  （没有无头浏览器，所以写了个 DOM shim：`审计与方案/verify_ui_render.js`）。

**验收**：109 个离线测试通过；19 条评测用例 19/19 通过（新增
`vibecoding_is_not_invented_into_incompetence` 直接复现上面那条真实事故、
`current_practice_is_captured` 验当前实践落层）；
`审计与方案/verify_memory_layers.py` 走真实接口验分层/闸门/用户改删全通；
`审计与方案/verify_ui_render.js` 用真实 chat.js 源码 + 真实载荷验界面渲染全通。

- 影响文档：`docs/DIALOGUE_CONTRACT.md` §3（重写为分层 + 三道闸门）、§4（召回改分层预算）
- 影响模块：`server/memory.py` `server/store.py` `server/schemas.py` `server/main.py`
  `server/dialogue.py` `web/js/chat.js` `web/css/styles.css` `eval/run_eval.py`
  `eval/dialogue_cases.json`
- 决策来源：用户实测反馈第二轮（记忆结构化 / 业界通则 / 实践跨度 / 写入标准 / 分列展示）
- 登记人：任务2

---

## [2026-10-01] [FIX] 任务必须有依据 + 三个实测 bug（用户手测反馈）

- **产品问题（最重要）：还没了解就派任务。** 用户实测：只说了「信管大二、想走大数据」，
  系统就派「关掉讲义重写一段代码」——这句对任何信管大二学生都成立，等于没了解这个人。
  这正是产品原则二「个性化必须有依据」被违反。现在做成了**代码侧闸门**：
  提案里的 `next_action` 必须带 `based_on`（本画像生效事实的 id），一个都引不到就整个丢掉，
  本轮不给落点。`based_on` 同时返回给前端，行动卡上显示「依据你说的：…」，
  把「凭什么给我这个」摊开给用户看。prompt 里也加了判断法：把用户信息划掉换成任何一个
  北大大二学生，这句话还成立吗？成立就是套话，不许填。
  - **同时修了反向问题**：收紧之后模型一度连用户明确问「我这周该干什么」都不给任务，
    变成连续三轮换个说法问同一件事——这是同一种毛病（一直在指望用户回答），只是反过来了。
    现在规则是：只要手上有至少一条跟他有关的真实事实，就直接给任务并引用它；
    同一个问题最多连续问两轮，第三轮必须换成给任务或给可点选项。
- **修 bug 1：`/api/dialogue/history` 返回数据库原始行，导致刷新后按钮全废。**
  三个出口形状不一致——`turn` 返回规整形状（`action_id`），`history` 和 `action_event`
  返回原始行（`id`）。前端刷新后读 `a.action_id` 得到 `undefined`，
  点「就做这个」发的请求缺字段 → 422；而 `api()` 把 FastAPI 的 422 `detail`
  （数组）直接塞进 `Error`，toast 就显示成 `[object Object]`。两个现象同一个根因。
  现在统一走 `_public_action()` 一个出口，并加了「三个出口形状必须一致」的回归测试。
- **修 bug 2：`[object Object]` 本身。** `app.js` 的 `api()` 用 `new Error(data.detail)`，
  后端 `detail` 是数组/对象时 `String()` 就变成 `[object Object]`，把「缺 action_id」
  这种明确信息盖掉了，白白多花排查时间。现在统一格式化成能读的一句话。
- **修 bug 3：空消息把请求打成 500。** `/api/onboard/message` 转调内核前没校验空串，
  内核按契约抛 `ValueError`，直接冒成 ASGI 异常。现在返回 400。
- 影响模块：server/dialogue.py、server/store.py（actions 表补 `based_on` 列 + 迁移 +
  快照恢复列）、server/main.py、web/js/app.js、web/js/chat.js、web/css/styles.css、eval/
- 验证：离线测试 79 → 85 全过；真实模型评测 12 → **15 条用例 15/15 通过**
  （新增 `no_generic_task_early` / `task_carries_its_basis` / `no_nagging_after_action`）。
- 决策来源：用户手测反馈（任务 2）
- 登记人：助手

---

## [2026-10-01] [BUILD] 任务 2：对话内核改为「模型决策、代码裁决」的闭环
- 变更内容：
  - **架构反转**。原来 `planner.py` 用 S0/S1/S2 规则先决定动作、模型只改措辞；现在改成一轮最多两次模型调用：调用① 出结构化 `TurnProposal`（观察 / 记忆写入意图 / 工具意图 / 回复 / 下一步），代码校验并提交，需要外部信息时执行只读工具，再由调用② 带工具结果收口。代码从「决定做什么」退到「决定能不能做」。
  - **记忆治理**（新增 `server/memory.py`）：key registry 白名单（不在表内一律拒绝）、引文必须能在本画像消息里逐字找到、confidence 由代码按来源指定（declared 0.6 / inferred 0.4 / behavior 0.8）、模型不得自称 behavior、`experience:*` 只能由提交事件写入、`constraint:*` 必须带 `valid_until`、方向互斥（写新方向自动把旧的置 `superseded`）、同 key 同 value 幂等（重试不重复计数）。被拒的操作进 `rejected_ops`，前端如实告诉用户「有 N 条我没记下来」。
  - **画像完全隔离**（修 M5）：快照切换从只覆盖 facts/messages/onboard_state 扩到 tasks/submissions/projects/conversations/actions/events/fact_revisions/decisions。新建画像不再看到旧画像的任务。
  - **工具层**（新增 `server/tools.py`）：`course.search` / `project.search` / `project.review`，全部只读；工具失败原样返回 `ok=false`，由模型如实转述，不许编造结果顶上。
  - **接口**：新增 `POST /api/dialogue/turn`、`POST /api/dialogue/stream`（SSE，真实阶段推进 + 回复正文增量）、`POST /api/dialogue/action`、`GET /api/dialogue/history`。`/api/onboard/*` 保留原签名，配了模型时**冷启动第一句起**就由新内核接管并把结果翻译回旧响应形状（`onboard_state` 由内核同步维护，附加信息挂在 `dialogue` 字段上），没配模型时仍走老向导，离线 demo 不受影响。
  - **前端**：新增对话视图（`web/js/chat.js`），支持多轮、长回复、轻量 Markdown 渲染、生成中状态行（观察 → 决策 → 核对记忆 → 查资料 → 组织回复）、回复逐字流式呈现、下一步行动卡（接受 / 完成 / 先不做）、「它记住的」侧栏、这一轮记下了什么 / 没记下什么 / 查没查到的如实回执。侧栏「画像」改为「对话」。
  - **安全修复**：`POST /api/tasks/{tid}/submit` 增加归属校验。原来任何 uid 都能提交别人的任务（审计 P0，探针实测 HTTP 200），现在返回 403。
  - **顺手修掉的状态泄漏**：`planner._signals` 原来用排除列表（`draft/dismissed/deleted`）过滤事实，新增状态会静默漏进方向计分；改成白名单 `DECISION_STATUSES`。`facts` 表新增 `valid_until`，`UserFact` 同步。
  - **不编造事实的代码侧兜底**：结构化路径（写进记忆的事实）本来就由引文逐字校验兜住，但**正文里的具体名称**没有约束。实测发现：检索失败后模型会承认「没查成功」，接着照样列出 7 个课名当推荐清单——大一学生拿这个去选课会踩空。现在工具返回 `ok=false` 且正文出现 `《…》` 时，触发一次定向重写（只删名称、保留原意与结构）；重写无效则保留原文并在响应里标 `fabrication_risk: true`，进日志可统计，不假装没发生。这是「正常路径最多两次模型调用」的唯一例外，语义见 DIALOGUE_CONTRACT §9.1。**效果实测**：这条规则单靠 prompt 只能压到 4 次里约 3 次合规；加兜底后连测 4 次全过（日志里能看到 repair 真的被触发过一次）。
  - **`next_action` 与 `pending_action` 拆开**：原来模型决定不给落点时，代码会回退成「把当前还开着的行动返回」，导致一个字段混了两种含义——「这一轮该不该派任务」从响应里根本看不出来（用户说「先别推进了」，响应里却仍有任务）。现在 `next_action` 只表示本轮决定，`pending_action` 单独表示界面状态；`move=acknowledge_stop` 时开着的行动会被置 abandoned。
  - **记忆 key 正则放宽**：原来只放行 `[a-z0-9_]`，模型写 `interest:machine-learning` 会被整条拒掉——丢的是用户真实说的话。现在允许连字符与点。
  - **工具参数兜底**：模型漏传 `course.search` 的必填 `query` 时，退回用用户这句话当检索词，并在结果里标 `query_from: user_message`，不再白费一整轮。
  - **测试**：`server/tests/` 从 11 → 79 个，新增 `test_memory.py`(18)、`test_dialogue.py`(20)、`test_api_dialogue.py`(13)、`test_tools.py`(11)、`test_portrait_isolation.py`(4)，覆盖校验规则、两次调用契约、行动生命周期与语义拆分、不编造兜底、画像隔离、SSE、P0 归属。
  - **评测**：新增 `eval/dialogue_cases.json`（12 条多轮用例）+ `eval/run_eval.py`，断言只判可机器判定的性质（是否重问已知信息、用户要动作时有没有落点、检索失败是否如实且不点名、改口后旧事实是否失效、是否重复记忆、说停时是否收干净）。真实模型验收 **12/12**。
  - **文档**：新增 `docs/DIALOGUE_CONTRACT.md`（v1 冻结契约：TurnProposal / TurnReply schema、memory key registry、env packet、工具 registry、九条确定性校验、降级语义、验收）。
- 影响文档：`docs/DIALOGUE_CONTRACT.md`（新增）、`docs/ARCHITECTURE.md` §5、`docs/CHANGELOG.md`（本条）
- 影响模块：server/memory.py、server/tools.py、server/dialogue.py（新增）；server/store.py、server/schemas.py、server/planner.py、server/llm.py、server/main.py、web/js/chat.js、web/js/app.js、web/index.html、web/css/styles.css、eval/
- 决策来源：任务表（任务 2 · 交互与问卷 / 对话 agent + 记忆）
- 登记人：助手

---

## [2026-09-30] [DOC] 新增方向路径交付目录（任务 4 · 数学 / 人工智能）
- 变更内容：新增 `docs/paths/`：`README.md`（每步的固定字段 + 主链标注约定）、`数学.md`、`人工智能.md`、`改树建议.md`。两条路径各 6 步，每步含「为什么是这一步 / 先弄懂什么 / 做完怎样算过了 / 依据 / 对应现有树节点」；文中共 41 条外部链接于 2026-09-30 逐条请求核对，核不到的四条（AMS Notices、Papers with Code、Hugging Face、Tao 某篇旧文）在文末如实记录，未用替代链接补位。
- 影响文档：`docs/paths/`（新增）；甲的两个方向（认知、经济）待补
- 影响模块：暂无代码改动；`web/js/app.js` 的 `FIELD_TREES` 改不改、怎么改，等两人路径合并后再定（建议见 `docs/paths/改树建议.md`）
- 决策来源：陈旭 依据任务表（任务 4乙）
- 登记人：助手

## [2026-09-30] [BUILD] 任务 3 边学边练：公开来源检索项目、交压缩包、五条标准评阅
- 变更内容：新增侧栏「项目」。来源清单 `knowledge/project_sources.json`（24 个来源、72 条逐字核对过原文的样例）；7 个来源实时检索（和鲸、飞桨学习赛、天池学习赛、北大开放数据、科学数据银行、创新大赛产业命题、欧拉计划中文站），其余给快照和「去哪找」路线；查不到就空着。成果以 `.zip` 提交，只在内存里读、不执行，按五条标准评阅，规则判定是上限，模型引文必须在文件里逐字找得到；每次提交写回一条行为事实。模型的工作说明写在 `skills/project-scout/`、`skills/project-review/`，代码与 skill 分工见 `skills/README.md`。新增接口只加不改，见 ARCHITECTURE §5。
- 影响模块：server/projects.py、server/project_adapters.py、server/submission.py、server/skills.py、server/store.py（新增 projects 表）、server/main.py、web/、knowledge/、skills/、docs/TASK3_PROJECTS.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-09-30] [BUILD] 首页点线图重画 + 工作区统一样式 + 后端几处修复
- 变更内容：首页去掉 Three.js 点云（`web/vendor/three.module.js` 5.4 万行删除），改为 Canvas 2D 点线图：六张图各一种一眼能认的线稿（台阶与门、问答气泡、罗盘、秒表、打勾的提交、生长的树），点距一致、每图一个主色；每屏前 40% 停住读字，之后像一支笔按笔画顺序改画成下一张。工作区重写 `styles.css`（1739 行叠加覆盖 → 一套 token），品牌移入侧栏，页面统一左对齐；方向树改为整齐树布局，连线不再交叉；任务与反馈去掉卡片套卡片；今日按「没聊 → 没核对 → 没方向 → 当前节点」给唯一建议。修复：视图快速切换时两个页面叠在一起、中文输入法回车误发送、今日页不认服务端已选方向、SQLite 连接不关闭、课程检索每次都起子进程（加 10 分钟缓存）、学期缓存永不过期（改为 3 天）、规则反馈对任何 60 字以上提交都给满分、LLM 无重试无 JSON 模式。
- 影响模块：web/、server/store.py、server/pku_adapter.py、server/llm.py、server/workbench.py、.env.example、docs/DESIGN_SPEC.md
- 决策来源：陈浩文
- 登记人：助手

## [2026-09-28] [FIX] 方向建议按当前画像分开
- 变更内容：方向区的建议改由当前画像的兴趣事实计算，不再因为已经选过方向就消失。不同画像命中的方向不同，芯片上标「建议」，并写一句为什么贴近。每份画像各自记住当前方向和进度。
- 影响模块：server/planner.py、server/main.py、web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-28] [FIX] 刷新后保住节点进度，切换方向要确认
- 变更内容：方向和任务上的进度按已经生成过的节点任务恢复，刷新不再退回树根。点另一个领域只是预览，要点「确认切换方向」才会改当前方向，并从新树的起点重新开始。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-28] [FIX] 切换方向不再跳进任务，提交后留下反馈
- 变更内容：在方向区点另一个领域时留在树上，并记下这棵树为当前方向。任务区只显示这棵树上当前节点的题目，不再沿用那道固定的对比实验。提交后在当前页写出 AI 反馈，不再整页重画。
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-28] [BUILD] 今日和方向先画出来，任务跟当前节点走
- 变更内容：进入今日和方向时不再等模型写完才显示。今日的内容按顺序浮出，方向树从起点一层层出现。任务对应当前方向树上的节点，完成后才提示进入下一节点并安排下一阶段任务。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html、server/workbench.py、server/main.py
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-28] [BUILD] 接上 DeepSeek，方向树加深
- 变更内容：本地 `.env` 写入 DeepSeek 密钥后，对话、方向文案和任务反馈会走真实模型。方向区改为六个领域各自一棵四层左右的树，点节点弹出介绍，可沿分支继续点。工作区侧栏和面板收得更紧。
- 影响模块：server/llm.py、web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 粒子左暗改成渐变，鼠标只碰身边
- 变更内容：从屏幕右侧到左侧亮度慢慢降一点，左边的粒子还在，不会突然灭掉。鼠标恢复成只拨开身边一小圈，划一下不再把整张图扭开。
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页粒子让开文字，鼠标改成慢回的波纹
- 变更内容：散开时粒子主要往右走，左边文字区域的粒子压暗、缩小，避免挡住字。鼠标划过时粒子立刻分开，再像水波一样往外荡，大约三秒才慢慢合上。
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 多画像、黑底粒子，方向改成生长的树
- 变更内容：画像可以新建、切换、删除，只有一份时显示为画像一。应用页去掉说明性文案。首页改为黑底，粒子用白、蓝、绿发光；图形聚好后先停一截再散开，散开时粒子朝眼前冲过来再聚合成下一张。方向区用一条会分叉的树标出已经过的节点、当前节点和还可以去的节点。
- 影响模块：server/store.py、server/main.py、web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 进入后改为工作区，不再按 1 到 9 往下点
- 变更内容：登录和首页体验按钮之后进入「今日」。左侧是今日、画像、方向、任务、记录五个工作区，主区域只显示当前这一个。画像里的对话和核对、任务里的提交和反馈，都留在各自工作区内部。保存、切换、返回不再被一条全局步骤条串起来。
- 影响模块：web/index.html、web/js/app.js、web/css/styles.css
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页补上每步说明，图案更快散开
- 变更内容：六屏各补一段重点说明，小任务页写清二十分钟、留下结果、按提交说话。图案聚好后，大约滑两下就开始散开，不再在页首停很长一段。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页加长，体验按钮移到末尾
- 变更内容：「立即开始体验」只出现在最后一屏底部。每一屏加高，并用序号、刻度、清单这类版式把高度撑开。粒子在后半段滚动里慢慢散开，范围铺到整屏，再聚成下一张图。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页图案改回粒子勾线
- 变更内容：去掉实线轮廓和填色。六张图都由更密的粒子自己画出来，分别是人与门、侧脸对话、罗盘分岔、二十分钟的钟和清单、批改标记、树与上升节点。滚动时先散开再聚拢；粒子可增减并淡入淡出，颜色随图渐变。
- 影响模块：web/js/app.js、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页改为连续滚动，轮廓实线、粒子填色
- 变更内容：去掉整屏吸附。滚动时当前形状先散成带噪声的曲线云，再聚成下一步。每个图案用实线勾边，粒子填在轮廓里面。
- 影响模块：web/js/app.js、web/css/styles.css、web/index.html
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-27] [BUILD] 首页粒子改为 Three.js 点云
- 变更内容：首页六屏粒子从 2D 方点改为 WebGL `Points`（约 2 万颗）。轮廓仍对应六页主题；滚动时在预计算点云之间形变。运动带小幅噪声漂移，鼠标附近有斥力与涟漪。点是软边圆点，墨色里夹少量橙色。未加全屏泛光，避免纸色底上字发雾。Three.js 放在 `web/vendor/three.module.js`。
- 影响模块：web/js/app.js、web/index.html、web/vendor
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-24] [DOC] 项目基线 v1.0 建立
- 变更内容：完成旧 Demo 审计、产品重构方案、全套 docs（PRD/架构/路线/分工等 21 份）
- 当前状态：**产品功能描述为草案**，D1–D10 决策（见 PRODUCT_EXPLAINED §五）待 2026-09-25 脑暴会逐条拍板
- 影响模块：全部
- 决策来源：项目组
- 登记人：A

## [待脑暴] 以下条目 9/25 会后补录
- D1 产品名
- D2 Onboarding 形态
- D3 首批重点学科名单
- D4 微任务素材来源
- D5 账号形态
- D6 部署位置
- D7 反馈通知频率
- D8 LLM 选型
- D9 论文陪读版权边界
- D10 种子用户计划

## [2026-09-27] [BUILD] 页面连接模型 + 视觉提一档
- 变更内容：首页与右上角可填写 OpenAI 兼容接口，连通后写入本机 `.env`。开场白、方向卡「为什么是你」、下一步文案、任务反馈在连通后走模型，失败仍回退规则。首页增加点云、序号橙色游标、分页底色和胶囊导航。
- 影响模块：server/llm.py、main.py、onboarding.py、planner.py、web
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-26] [BUILD] 接入 OpenAI 兼容模型 + 首页视觉升级
- 变更内容：新增 `server/llm.py`，对话回应、自由文本事实归纳、NBA 文案、任务反馈在配置 `LLM_API_KEY` 后走真实模型，失败回退规则。首页增加加载页、顶部引导细线和序号色块。密钥放 `.env`，示例见 `.env.example`。
- 影响模块：server/llm.py、onboarding、planner、workbench、web
- 决策来源：邬程灿
- 登记人：助手

## [2026-09-25] [BUILD] W0 核心 Demo 交付 + 文档体系精简
- 变更内容：
  1. **W0 Thin Slice 全链路实装并验证**：昵称登录 → 5 轮 onboarding（选项卡+不知道）→ UM 确认页（可改可删）→ 3 张方向推荐卡（rationale 引用用户原话 + 北大真实课程 live 检索 + 入门读物）→ 20 分钟微任务 → 提交 → rubric 逐条反馈 → behavior 事实真实写回 UM → 进阶 NBA。技术：FastAPI（server/）+ 无构建静态前端（web/），课程检索经 uv 子进程调 skills/pku-course（原 pku-course-skill-main 原样迁入），LLM 三处按 NEXT_PRE §3 白名单 mock（数据结构真实）。
  2. **文档精简 25→5**：新增 DESIGN_SPEC（旧 Demo 视觉提炼为规范）、ARCHITECTURE（四份长文档浓缩合并）、TASK_ASSIGNMENTS（6 人 vibecoding+PR 分工）；删除 PRD/VISION/GAP/AUDIT/DOC_SYNC/SESSION_PROMPT/RISK/ROADMAP 等 20 份长文档与 ideas/想法类文件。
  3. **旧 Demo 处置**：discipline-map 网页删除（设计已提炼进 DESIGN_SPEC，搜课适配逻辑迁入 server/pku_adapter.py）；可复用资产迁入 skills/pku-course 与 knowledge/。
- 影响模块：全部
- 决策来源：别克扎提（产品 Owner）
- 登记人：别克扎提·拜别提
