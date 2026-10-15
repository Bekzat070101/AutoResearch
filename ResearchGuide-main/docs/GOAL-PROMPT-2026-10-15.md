# /goal 提示词（对照三份 10-15 文档优化产品）

新开一轮对话，把下面从 `/goal` 到最后一段**整段**发出去。三份方案是权威，不要另起方案。

```
/goal 对照下面三份已经写好的方案，在当前产品上一次做完「能在本机落地的优化」。不要另起方案，不要把生产发版写成已完成。

产品代码：C:\Users\Lenovo\Desktop\Self-Evolved-Agent\product\ResearchGuide-main
方案目录（权威，冲突时以这三份正文为准，不要用走查记忆代替）：
  docs/PRODUCT_ISSUES.md        ← 2026-10-15 产品体验问题（做哪些、哪些已走通、哪些明确不做）
  docs/MONITORING_OPS.md        ← 系统监控指标/展示 + 系统运维已解决/待解决/改进
  docs/PAPER_COLLECTION.md      ← 方向树每个节点检索定额论文写入后端（不是现场搜索）
副本（与 docs 应保持同文）：C:\Users\Lenovo\Desktop\Self-Evolved-Agent\运维信息\
  启研-产品体验问题.md
  启研-监控运维计划.md
  启研-方向论文入库计划.md
本文只是提示词，不是第四份方案。

开工读法（必须按这个顺序读完再改代码，禁止跳读后凭记忆开工）：
1. 通读 PRODUCT_ISSUES.md 全文：先建立「仍成立的问题 / 已走通不要回退 / §8 明确不做」。
2. 通读 MONITORING_OPS.md 全文：指标、SLO、告警、待解决 1–10、改进机会。本轮代码能做的做掉；需要挂盘/控制台的只写步骤和验收命令。
3. 通读 PAPER_COLLECTION.md 全文（含表结构、定额、脚本、接口、验收 §8）。这是本轮最大一块，按文中「实施顺序」做，不要先手写 53 份 kit JSON。
4. 对照当前工作树重定位行号后再改。三份里的旧行号、走查时的 uid、metrics 快照都可能过期。

三块各自的目标状态（细节以各文件正文为准，这里只防漏）：

———————— 一、产品体验（PRODUCT_ISSUES）————————
必须做（本机可证）：
- 注册/登录路径真正渲染 PRIVACY_HTML，用户看见后再 consent；禁止再静默 consent: true。隐私文案里「记录页」改成「我的」。升 PRIVACY_VERSION 仅当正文变了，且用当天日期，digest 与 test_auth 一起改。
- 「我的」页接上 GET /api/me/export（下载 JSON），页脚「能导出」必须为真。
- 侧栏默认文案不要「连接模型」：登录用户走「我的 DeepSeek」；管理员入口只留登录页已有的「服务器共用模型（管理员）」，或仅管理员可见。
- GET /api/papers/{id} 的 title_zh：库没有时合并工具包手写中文，打开 MMLU 不能再是空串而页面靠自己猜。
- 带读：标题原文不能当「主张原句」过线；能压显示题数时不要一上来 total:8 显得题很多（14 的出题 ≤6 靠题库，本轮至少不要把标题判过）。
- GET /api/directions/recommend 在 ranking 已有条目时不应长期 cards:[]；对话「查看方向树」要能看到按规则排出的方向（不调模型、不叫 AI 推测）。
- 前端 chooseDirection 已 await PUT trail（portrait uuid + {data:{dir,code}}）必须保住。后端 daily 以 trail.dir 为准：已有 190.20 时不准塌成 190.15。API 若只收到 choose code=psy、没有 trail，允许没有细节点，但不要假装已经选了「心理学」细类。
- 占位页：定位/项目继续「待开发」。占位页不要再打 /api/edges、/api/projects/context。死代码 renderPosition/renderProjects 停用或删掉调用路径，不要在占位导航里复活整页。
- health.model_pool.queued/running：改成真实值，或删掉这两个假字段（只留能当真的）。不要继续写死 0。

明确不做 / 不回退（PRODUCT_ISSUES §8、§9 与 request-10-14）：
- 定位、项目功能本身、全文翻译、手机端、提示点开高亮、direction: 写入闸门整单、金融/社会学树节点、切站瑕疵、找回密码。
- 不要加回「先看看」；POST /api/auth/login 访客接口保留（测试还在），页面不当入口。
- 不要恢复每次打开重新登录。
- 不要为了演示去发生产函数。A5 NAS 仍待确认。

———————— 二、监控运维（MONITORING_OPS）————————
必须做（代码/仓库内）：
- 保住本机 GET /metrics（Prometheus 文本）。补 qiyan_paper_dir_rows（或按 node 的缺口）并写入 docs/grafana/qiyan-overview.json。
- 修 qiyan_http_errors_5xx_total 与 qiyan_http_requests_total{path,code} 不同步：有 5xx 就必须能在分路 counter 里看到对应 code。
- .github/workflows/uptime.yml：继续探 health + Pages；增加「记录 ephemeral 与 Pages wN vs checks.build」——已知生产 ephemeral=true、w74≠w80，探活不要改成一直红而假装挂盘已完成。写成额外一步打印/注解，或仅在 secrets 打开 QIYAN_REQUIRE_PERSISTENT 时才 fail。ALERT_WEBHOOK 未设时保持「只在 Actions 红」。
- admin stats 增加论文目录缺口（不足定额的 node 列表），便于验收入库。

需要挂盘/控制台、本轮只文档化不准假装完成：
- A5 挂 NAS、生产 ephemeral=false、前后端一起发到函数、生产刮 /metrics、钉钉 webhook、RAM、OSS 7 天备份。
- 禁止 deploy_fc.py 打生产；禁止 --allow-ephemeral。Pages 若因前端改动需要同步，可以发 gh-pages，但必须在 CHANGELOG/DEPLOY 写明：现网 API 仍是 w74，新页面打旧接口的 P0 未解除。

———————— 三、按方向入库（PAPER_COLLECTION）————————
这是本轮主交付。严格按该文定额、表、脚本、接口、验收做。
- 新增 paper_dir，不要把方向目录塞进 paper_pool（那是 topic+day 新鲜度缓存）。
- 范围：directions.js 树 ∩ arxiv_topics.json nodes，共 53 节点，不是 planner 7 码。金融/社会学树节点仍不做。
- 每节点：目录 ≥20 篇元数据；其中至少 8 篇 paper_text pinned。thin（基础医学 9 节点）目录 ≥8、正文 ≥4；不足必须在 report 写原因，不准 silently skip、不准编造论文。
- llm-eval 10 篇另外 pin，不占 520.20 的 20。
- 脚本 server/paper_collect.py：--node / --all / --report；可断点续跑；arXiv/OSF 间隔 ≥3 秒；日志 event: paper_collect。admin POST /api/admin/papers/collect 与脚本共用函数。
- GET /api/papers/dir?node=；无 node 用 trail.dir。
- warm_targets = 工具包 ∪ paper_dir 里未正文的 pin 候选。
- GET /api/papers/search：先查目录/标题，不足再现场 arXiv；中文仍 400。
- 研读左栏：trail.dir 有目录时用目录，不是写死 llm-eval。无工具包的方向：能打开正文+带读，不交阅读卡。
- 今日：有目录后优先从目录筛较新的；paper_pool 现场检索只补缺口；GET /api/daily 仍然不带 kit。
- 只在 ephemeral=false 的库上跑全量 collect。对本机 demo.db（或你指定的 QIYAN_DB）必须真跑，用 --report 当验收证据。禁止往生产 /tmp 灌库。
- 不要为 53 个方向各写一份 kit JSON。

实施顺序（同一时间推进一块，前一块可证再下一块）：
1. 监控小修：metrics 5xx 同步、model_pool 真假、grafana/uptime 文档化检查。
2. 体验小修：隐私展示、导出、侧栏文案、title_zh 合并、占位页不再打未开发 API、recommend 空列表、带读标题不过线。
3. paper_dir 表 + store + 测试。
4. collect 脚本：先 190.20 / 520.20 / 110.67 三个对照节点跑通 A，再 --all 铺元数据，再 B 正文，再 C 翻译（额度不够就停，不阻塞 A/B）。
5. GET /api/papers/dir、研读左栏、搜索库优先、daily 目录后备、指标与 admin stats。
6. 全量 --report 对照 PAPER_COLLECTION §8。

必须保住的产品不变式（与 request-10-14 / 当前 w80 一致）：
- 首页进入只留登录/注册；会话 30 天滑动；失效有提示不静默落首页
- 用户名+密码；每人一把 DeepSeek 在 user_secrets，禁止把个人 key 写进 os.environ["LLM_API_KEY"]
- 一份 SQLite，按 user_id 隔离
- 医学只做基础医学；统计挂数学下；不编课程/论文/项目/老师/数字
- REST 只加不改不删已有路径和字段形状（方案写明的新路由、新字段、非法 id 400 除外）
- 令牌 Authorization: Bearer
- 当前方向 = 活跃画像 trail.dir；今日永远不带 kit
- 改 web/ 必须把 index.html 每一个 ?v= 和页脚 <b>wN</b> 改成同一号；现是 w80，第一次改前端从 w81 起
- 改了行为或文档，在 docs/CHANGELOG.md 顶上追加；决策来源写邬程灿（体验问题/监控计划/论文入库 2026-10-15）
- 不提交 .env / .deploy.env / server/data/ / 论文正文 JSON；不强推；日志和回包不出现完整 API key、不出现密码
- Windows 测试：在 product/ResearchGuide-main 下 python tools/verify/run_existing_tests.py（不要直接 pytest server/tests）。前端再跑 node --check web/js/*.js
- 只做 PC

每块做完必须自检，缺一条不准进下一块：
- 用对应方案文件每一条对照代码：做的有实现，不做的确认没做。
- 用 PAPER_COLLECTION「实施顺序」每个步骤对照工作树。
- 用各文件「验收」取证（测试输出、路由、页面文案、本机接口、--report）。间接搜索命中不算过。
- 会红的测试按新契约改；必须仍绿的不许改坏；新表/新路由要有测试。

整份怎样算做完：
- PRODUCT_ISSUES 里「仍在的问题」除 §1§2 生产 P0（挂盘才能解）和 §8 明确不做之外，本机都有证据。
- MONITORING_OPS 待解决 5–10 中能写代码的已落地；1–4 仍标明待确认，且本轮未发生产函数。
- PAPER_COLLECTION §8：本机 --report 对非 thin 节点 dir_n≥20 且 html_or_pdf≥8（thin 按降定额或写明上游空）；GET /api/papers/dir?node=190.20 不打 arXiv 也能返回目录；研读在 trail.dir=190.20 时左栏不是 llm-eval 那 10 篇；qiyan_paper_text_rows 明显高于 2 且 pin 后不回落。
- run_existing_tests.py 全绿；版本戳和全部 ?v= 同一号；旧 REST 形状未擅自改。
任一未证不得标 complete。生产 ephemeral 仍为 true 时，不准把「线上研读已覆盖全方向」或「生产监控已刮到 /metrics」写成已完成。
```
