# 启研：按方向收集论文并写入后端

> 目标：研读不再只靠用户现场搜索。方向树上**每一个节点**都要先检索、再把一定数目的论文元数据和正文存进 SQLite，打开研读/今日时优先读库。  
> 现状证据：2026-10-15 本机 `qiyan_paper_text_rows = 2`；唯一工具包是 `knowledge/kits/llm-eval.json`（10 篇）；`reading.warm_targets()` 只预热工具包 + 已读过的 id。  
> 相关：体验问题 [PRODUCT_ISSUES.md](PRODUCT_ISSUES.md) §6；监控 [MONITORING_OPS.md](MONITORING_OPS.md)。  
> 本文是计划，不是已完成的功能。A5 未挂盘前，收集结果只写本机 / NAS 上的 `QIYAN_DB`，**不要**往 ephemeral 生产函数里灌库。

---

## 1. 为什么要做

现在三条取数路径都是「用的时候再搜」：

| 入口 | 现在怎么取 | 缺什么 |
| --- | --- | --- |
| 研读左栏 | 只渲染 `llm-eval` 的 10 篇 | 心理 / 经济 / 医学 / 数学等方向没有推荐列表 |
| `GET /api/papers/search` | 当场打 arXiv，结果不按方向落库 | 下次换人、换会话再搜一遍；中文直接 400 |
| `GET /api/daily` | 按 `trail.dir` 映射话题，写入 `paper_pool(topic, day)` | 这是「当天新鲜度缓存」，过一天或换实例就没了；OSF（PsyArXiv）往往只有摘要 |

本机走查：社会心理今日能出现 6 篇 PsyArXiv，但 `paper_text` 仍只有 2 行。带读、阅读卡、标题中文行都要正文或 `paper_zh`；现场搜索扛不住 arXiv 3 秒锁和函数冷启动。

要求改成：**每个方向先搜够定额，写入后端；页面先读库，现场搜索只作补漏。**

---

## 2. 范围：什么叫「每个方向」

以 `web/js/directions.js` 的树为准，和 `knowledge/arxiv_topics.json` 的 `nodes` 一一对应（测试 `test_arxiv_topics_cover_every_tree_node` 已钉死）。**不是** `planner.DIRECTIONS` 那 7 个后端码。

共 **53** 个节点：

| 一级 | 节点数 | 来源 |
| --- | --- | --- |
| 520 计算机 | 16 | arXiv `cs.*` |
| 110 数学 / 统计 | 11 | arXiv `math.*` / `stat.*` |
| 190 心理学 | 8 | PsyArXiv（OSF），不是 arXiv |
| 790 经济学 | 9 | arXiv `econ.*`（金融 topic 有、树上暂无独立节点） |
| 310 基础医学 | 9 | arXiv `q-bio.*`（`thin` 名单，文献少，允许不足并记下缺口） |

每个节点通过 `nodes[id] → topics[id]` 得到检索式：`categories` + 英文关键词，或 `source: osf` + `provider` + `subjects`。

不在这棵树上的（社会学独立节点、金融 790.xx）本轮不强制；话题表里已有 `sociology` / `finance`，树加上之后用同一套任务就能收。

---

## 3. 定额（这次必须达到）

按节点计，不按 7 个大类糊弄：父节点和子节点各自有自己的目录。同一篇论文可以挂在多个节点（例如 `520.20` 与 `520.20.nlp`）。

| 层 | 每节点最低 | 存什么 | 用途 |
| --- | --- | --- | --- |
| A 目录 | **20** 篇元数据 | 标题、摘要、作者、发表日、url、来源、方向 id | 研读推荐列表、搜索先查库 |
| B 正文 | 目录里至少 **8** 篇 | `paper_text`，`pinned=1` | 打开就能带读，不打上游 |
| C 中文标题 | 目录 20 篇尽量都有 | `paper_zh` | 标题下那一行；没有就空着，不编 |

`thin` 节点（基础医学 9 个）：A 降为 **8**，B 降为 **4**；跑完仍不足要在收集报告里写原因（分类太窄 / 上游空），不算 silently skip。

工具包 `llm-eval` 的 10 篇另外 pin，不占 520.20 的 20 定额，但可以同时出现在 520.20 目录里。

数量估算：53 × 20 ≈ 1060 条目录；非 thin 约 44 × 8 + 9 × 4 ≈ 388 篇正文。现有默认上限 `ARXIV_CACHE_MAX_FILES=2000`、`ARXIV_CACHE_MAX_BYTES=100MB`（`/tmp` 时），pin 之后不会被 LRU 踢掉。挂 NAS 后应把字节上限调到 ≥500MB。

---

## 4. 后端怎么存

现有三张表职责不够，不要把「方向目录」塞进 `paper_pool`（那是按 **topic + 当天日期** 的新鲜度缓存）。

### 4.1 新增 `paper_dir`（方向目录）

```
paper_dir (
  node_id    TEXT NOT NULL,   -- 190.20
  paper_id   TEXT NOT NULL,   -- 2310.17623 或 psyarxiv:xxxxx_v1
  topic_id   TEXT NOT NULL,   -- psy-soc
  rank       INTEGER NOT NULL, -- 1..N，检索时的相关序
  source     TEXT NOT NULL,    -- arxiv | psyarxiv | socarxiv
  title      TEXT NOT NULL,
  summary    TEXT NOT NULL DEFAULT '',
  authors    TEXT NOT NULL DEFAULT '[]',
  published  TEXT NOT NULL DEFAULT '',
  url        TEXT NOT NULL DEFAULT '',
  categories TEXT NOT NULL DEFAULT '[]',
  collected_at TEXT NOT NULL,
  PRIMARY KEY (node_id, paper_id)
)
CREATE INDEX idx_paper_dir_paper ON paper_dir(paper_id);
```

### 4.2 沿用

- `paper_text`：正文。目录里要带读的那 8 篇 `pinned=1`。OSF 没有 arXiv HTML 时：能抽 HTML/PDF 就写 `source=html|pdf`，否则 `source=abstract` 并如实标注，**仍计目录定额，不计正文定额**。
- `paper_zh`：标题机翻 + 关键词，走现有 `*xlate` 额度，失败留空。
- `paper_pool`：今日「最近新论文」继续用，不和经典目录混。

### 4.3 接口（建议，保持 REST 加字段不改旧语义）

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/papers/dir?node=190.20` | 读该方向目录，登录用户；无 node 则用 `trail.dir` |
| POST | `/api/admin/papers/collect?node=&quota=20&fulltext=8` | 管理员跑收集；`node` 空则全树 |
| 现有 | `POST /api/admin/papers/warm` | 改为：warm 对象 = 工具包 ∪ `paper_dir` 里未正文的 pin 候选 |
| 现有 | `GET /api/papers/search` | **先查 `paper_dir` / 标题**，不足再现场 arXiv；命中库的带 `lib=html|abstract` |

研读页：当前 `trail.dir` 有目录时，左栏「经典文献推荐」用目录，而不是写死 `llm-eval`。工具包仍可作为「有阅读卡/矩阵」的那一支；无工具包的方向只提供打开正文 + 带读，不交卡（与现网「非 kit 不交卡」一致）。

---

## 5. 收集怎么跑

### 5.1 检索

对每个 `node_id`：

1. 取 `topics = nodes[node_id]`（通常 1 个，少数 2 个）。  
2. 若 `source == osf`：`osf.recent(provider, subjects, max_results=40)`。  
3. 否则：`arxiv.recent(categories, en, max_results=40)`。  
4. 去重（同一 `paper_id`）、丢掉无标题的。  
5. 写入 `paper_dir`，`rank` 按接口返回顺序。已有行只更新 `collected_at`，不降低 rank。  
6. 数量不足定额：换一组更宽的父节点关键词再搜一次（例如 `190.20` 不够就加 `psy` 的英文词），仍不足记入报告。

遵守现有锁：arXiv 请求间隔 ≥3 秒；OSF 同样串行。全树 53 次检索大约 3–5 分钟；正文 388 篇 × 3 秒下限约 **20 分钟**，失败重试会更长。必须可断点续跑。

### 5.2 正文

对每个节点目录按 rank 取前 8（thin 前 4）：

- arXiv id：走 `arxiv.fulltext`，成功则 `put_paper_text(..., pinned=True)`。  
- OSF id：能拿到全文再 pin；只能摘要则目录保留、正文计数不加。  
- 失败（无 HTML、429、解析空）跳过，顺延下一篇，直到凑满或目录耗尽。

### 5.3 中文标题

对目录中 `paper_zh` 为空的前 20 篇走现有翻译记账。额度不够就停，不阻塞 A/B。

### 5.4 命令与进度

本机脚本（建议路径 `server/paper_collect.py`，风格对齐 `paper_warm.py`）：

```
python server/paper_collect.py --all --quota 20 --fulltext 8
python server/paper_collect.py --node 190.20 --quota 20 --fulltext 8
python server/paper_collect.py --report
```

`--report` 打印每个 node：目录数、正文集、摘要-only、失败原因。验收：非 thin 节点目录 ≥20 且正文 ≥8；thin ≥8 / ≥4。

管理员接口与脚本共用同一函数，方便发版后在 NAS 库上补跑。日志打 JSON 行：`event: paper_collect`，便于和现有 SLS 对上。

### 5.5 和今日、搜索的分工

- **目录 `paper_dir`**：相对稳定的「这个方向可以读的一批」；每周或发版后增量即可。  
- **今日 `paper_pool`**：只要最近 30 天的新预印本；仍可现场补 1 篇冷话题。有目录后，今日优先从目录里筛 `published` 新的，现场检索降为后备。  
- **搜索**：本地目录标题/摘要匹配 → 再 arXiv。中文仍 400（产品已定），不要为入库放开中文检索式。

---

## 6. 实施顺序

1. 建 `paper_dir` 表 + store 读写 + 测试（空树、重复写入、OSF id）。  
2. 收集脚本：只做 A（元数据），先跑通 190.20 / 520.20 / 110.67 三个对照节点。  
3. 把 A 铺到全部 53 节点，产出 `--report`。  
4. B 正文 pin；扩展 `warm_targets()`。  
5. C 标题翻译（受 `*xlate` 额度限制）。  
6. `GET /api/papers/dir`；研读左栏按 `trail.dir` 读目录。  
7. 搜索改为库优先。  
8. 指标：`qiyan_paper_dir_rows`、按 node 的缺口；admin stats 增加「不足 20 的节点列表」。  
9. **只在 `ephemeral=false` 的库上跑全量。** 生产仍是 `/tmp` 时禁止 collect。

不要一上来给每个方向手写第二份 kit JSON。kit 仍是「有阅读卡维度和开放问题、需要学长核对」的高质量包；目录是自动检索的保底存量。

---

## 7. 风险与约束

| 风险 | 处理 |
| --- | --- |
| arXiv / OSF 限流 | 全局 3 秒锁、可续跑、失败写入 report 不装成功 |
| 医学节点文献少 | `thin` 降定额，列缺口，不编造 |
| 正文把库撑爆 | pin 只限定额；摘要-only 不进 `paper_text`；NAS 上调 `ARXIV_CACHE_MAX_BYTES` |
| 版权 | 只存公开预印本（arXiv / OSF）HTML 或作者 PDF；用户自己导入的仍只在 `user_papers` |
| 检索跑偏 | 只用 `arxiv_topics.json` 已审核的英文词和分类，不让模型现场造 query |
| 和生产抢实例 | collect 是离线任务，走 admin 或本机脚本，不在用户请求里同步搜 20 篇 |
| 发版丢库 | 见监控计划：`ephemeral=true` 时拒绝 collect |

---

## 8. 完成怎么验收

对每个 `nodes` 里的 id：

```
python server/paper_collect.py --report
```

- 非 thin：`dir_n >= 20` 且 `html_or_pdf >= 8`  
- thin：`dir_n >= 8` 且 `html_or_pdf >= 4` 或 report 写明上游空  
- `GET /api/papers/dir?node=190.20` 返回 ≥20 条，不打 arXiv（断网或 cached_only 仍 200）  
- 研读在 `trail.dir=190.20` 时左栏不是空的、也不是 llm-eval 那 10 篇  
- `qiyan_paper_text_rows` 明显高于 2（量级约数百，pin 后不回落）  
- 测试：每个树节点在 `paper_dir` 里达到定额；`test_arxiv_topics_cover_every_tree_node` 继续保证节点集合不漂

未跑完收集任务前，不要把「研读已覆盖全方向」写进产品完成态。
