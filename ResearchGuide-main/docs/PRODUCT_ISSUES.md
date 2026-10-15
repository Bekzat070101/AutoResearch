# 启研：产品体验问题（2026-10-15）

> 体验对象：本机 `http://127.0.0.1:8100/`（w80 + `server/data/demo.db`）以及生产页 https://chengcanwu.github.io/AutoResearch/ 、接口 `https://qiyan-caxplsowco.cn-hangzhou.fcapp.run`。  
> 取证：`scratchpad/walk_product.py`（93 项）、`scratchpad/fetch_pages.py`。  
> 已开发模块：首页、登录/注册/会话、「我的」、对话与方向 ranking、今日、研读/论文库/搜索/PDF、带读两页签、侧栏收起。定位与项目为占位。  
> 相关：监控运维见 [MONITORING_OPS.md](MONITORING_OPS.md)；按方向把论文搜进后端见 [PAPER_COLLECTION.md](PAPER_COLLECTION.md)。

下面只写**当前仍成立**的问题。request-10-14 已落地且本机走通的，不重复当新缺陷。  
走查方式：静态页面/脚本逐控件核对 + 本机真实 HTTP（含一轮真实 DeepSeek 对话、带读出题与作答）+ 生产 health/Pages。

---

## 1. 生产：前端 w80、接口仍是 w74（P0）

| 证据 | 结果 |
| --- | --- |
| Pages `https://chengcanwu.github.io/AutoResearch/` | 界面版本 **w80**，`QIYAN_API` 指向生产函数 |
| `GET $PUBLIC_URL/api/health` | `checks.build = "6344046 w74"`，`privacy_version = 2026-10-13` |
| `GET /api/directions/ranking` 未登录 | **404 Not Found**（w80 前端会调这条） |
| `GET /api/papers/search?q=sparse` | **502**，详情是「不是有效的 arXiv 编号」——旧路由把 `search` 当成论文 id |
| `GET /metrics` | **404**（w74 没有这条） |

A5 未挂盘：`db.ephemeral = true`。按 `docs/DEPLOY.md` **不准发生产函数**（发版会清 `/tmp` 库）。所以现在是「新页面打旧接口」：登录还能用，ranking / 搜索 / PDF 导入 / 模型判卷 / 探活指标这些新契约在现网上不可用。

---

## 2. 生产库在临时盘（P0）

- health：`db.ephemeral: true`
- `QIYAN_SECRET` 未设（`checks.secret_set: false`）
- 微信未配（`auth.wechat_login: false`；本机 `POST /api/auth/wechat/start` → **503**「微信登录还没开通」）
- 共同后果：实例回收或改环境变量 → 账号、会话、个人 DeepSeek、阅读记录一起丢。这是「半小时被踢 / 同名还能再注册」的根因，不是产品故意每次打开重登。

---

## 3. 登录、隐私、账号主权

本机走通：注册 200、重复注册 409、短密码 400「密码至少 8 个字符」、错密码 401、坏令牌 401、登出后 `/api/auth/me` 401。页面入口只有登录/注册（无「先看看」）。

仍在的问题：

| 问题 | 证据 |
| --- | --- |
| 隐私说明写了但从未渲染 | `PRIVACY_HTML` 只在 `web/js/app.js` 定义；全站没有第二处引用。注册直接 `consent: true`。新用户看不到「作答会发给模型」。 |
| 「我的」页不能导出 | 页脚写「『我的』页能导出」；后端 `GET /api/me/export` 本机 200；`accountPanel()` 只有「我的 DeepSeek / 退出 / 删除账号」，从不请求导出。隐私文案还写「『记录』页随时可以导出」，页名已经改成「我的」。 |
| 访客登录接口还在 | 本机 `POST /api/auth/login` 仍 200。页面不再当入口，但有人仍可打接口进访客号。 |
| 没有找回密码 | DEPLOY E8：忘了密码只能工单删号。 |
| 侧栏首屏「连接模型」 | `index.html` 默认文案仍是管理员共用密钥入口。登录后 JS 会改成「我的 DeepSeek」，但首屏/访客仍会把个人 key 和服务器 key 搞混。 |
| 本机 `QIYAN_SECRET` 未设 | health `secret_set: false`，个人 key 封装用开发兜底串。 |

---

## 4. 对话与方向

本机真实模型一轮（「我对认知心理学里的社会心理学很感兴趣，想做本科科研。」，约 20s 级）：

- 回复追问院系/年级，引用培养方案页码；`next_action` 为 `null`（不再派微任务）。
- `GET /api/directions/ranking` 第一条 **190.20**，`why` 为原话，规则、不调模型。
- 前端 `chooseDirection` 会 `PUT /api/projects/{portraitUuid}/trail`，body 为 `{data:{dir,code,...}}`。按这个写完后，今日方向是 **190.20 社会心理**，6 篇，话题 `psy-soc`。

仍在的问题：

- `POST /api/directions/choose` 只收 7 个后端码。社会心理与认知心理都会写成 `direction:psy`。今日**真正**认 `trail.dir`。若只 choose、不写 trail（或 portrait id 用 `"default"` 而不是活跃画像 uuid），就会回落到 190.15「心理学」。走查里同时打到了 `/api/projects/default/trail` 和 uuid trail——「闸门另开单」的现场形态还在。
- `GET /api/directions/recommend` 在对话 + ranking 有条目之后仍是 `cards: []`。对话侧栏「查看方向树」可能是空的。
- 对话栏在进入工作区前 `hidden`（设计如此）；未 `entered` 时侧栏整栏也隐藏。`POST /api/onboard/enter` 本机 200。

---

## 5. 今日

本机有方向后 `GET /api/daily`：`kit: null`、6 篇、PsyArXiv（如 `psyarxiv:uemvp_v3`），未知 kit_id 的 prefs 回 400。符合 12。今日页已去掉「定位微调」。

仍在的问题：

- 现网今日若打到 w74 接口，会回到工具包模式。
- 冷话题现取会碰到 arXiv 3 秒锁，本机有时要数秒；没有单独的「今日取数超时」指标。
- 标题机翻依赖服务器密钥与 `*xlate` 额度；没配或超额时标题下中文行空着。
- 今日列表来自按话题的**现场检索缓存**（`paper_pool`，按 topic+day），不是按方向预存的论文库。换方向或缓存过期就要再打 arXiv / OSF。见 [PAPER_COLLECTION.md](PAPER_COLLECTION.md)。

---

## 6. 研读 / 论文 / 带读 / 阅读卡

本机走通：工具包 10 篇有 `title_zh`；英文搜索 `sparse attention` 200；中文搜索 **400**「arXiv 只认英文关键词」；非法 id 400；MMLU 正文 `source=html` 且 `lib_at=db`；坏 PDF 400「读不了这份 PDF」；带读 GET `can_grade:true`；start `voice=llm`；用论文标题作答一题 **200、判过线**。

仍在的问题：

- **现网搜索/上传不存在**（见 §1）。
- `GET /api/papers/{id}` 的 `title_zh` 只读 `paper_zh` 表，不合并工具包手写中文。打开 MMLU 接口是 `title_zh:""`，要靠前端读 kit。
- 带读 start 回包 `total: 8`（站/题计划），14 书写的「出题 ≤6」要靠共享题库压次数，首篇仍可能显得题多。
- 把论文**标题**当成「主张原句」也能过线（本机 turn `pass: true`）。判卷对「从摘要复制一句」偏松。
- 本机 `qiyan_paper_text_rows = 2`。工具包声明 10 篇，正文库只有 2 行——预热/挂库不完整，冷启动仍会打 arXiv。
- 研读左栏「经典文献推荐」只来自唯一工具包 `llm-eval`。心理学、社会学、经济、医学等方向**没有**预存论文；用户只能现场英文搜索。搜索结果也不写入方向目录。
- 定位、项目侧栏是「待开发」占位，`setView` 走 `renderPositionSoon` / `renderProjectsSoon`。但 `renderPosition()` / `renderProjects()` 整页代码仍在；`/api/edges`、`/api/projects/context` 登录后仍 200。用户点进去是空态，接口还在耗模型/库。

---

## 7. 可观测性缺口（体验时直接碰到）

- 本机 **`GET /metrics` 200**，Prometheus 0.0.4 文本。**生产 404**，尚未随函数发出。
- `/api/health` 的 `model_pool.queued/running` **写死 0**，不能当排队真相。
- 走查结束后 `qiyan_http_errors_5xx_total = 1`，但按 path 的 counter **没有**任何 `code="5xx"`。错误总数和分路计数不同步，看板会误报。
- GitHub `product/.github/workflows/uptime.yml` 每 15 分钟探 health + Pages；`ALERT_WEBHOOK` 未设时失败只在 Actions 变红。uptime **不**探 `/metrics`，也 **不**断言 `ephemeral == false`。
- 2026-10-14 Pages 发布第一次 **deploy timeout**，第二次才成功。没有「Pages 构建时长」看板。

细节与看板见 [MONITORING_OPS.md](MONITORING_OPS.md)。

---

## 8. 明确不做 / 占位（不是回归）

定位、项目、全文翻译、手机端、提示点开高亮、`direction:` 写入闸门、金融/社会学树节点、切站瑕疵、找回密码：会上裁定下一版或不立项。监控计划里把它们标成「产品范围外」，不当成 SLO 违约。

---

## 9. 本机已走通、不要当成缺陷

登录注册与会话、对话不再派微任务、ranking 规则出 190.20、今日无 kit 且按 trail.dir 取 PsyArXiv、研读三栏与搜索拒中文、论文库字段、坏 PDF 400、带读两页签与 textarea、侧栏收起、nav 五项、定位/项目「待开发」文案、本机库 `ephemeral: false`、本机 `/metrics` 已暴露。
