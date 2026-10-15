# 启研：监控运维计划

> 对照课程《系统监控与运维》（2026 秋，马郓，10-19 中期汇报）与 Lab4（Prometheus / Grafana）。  
> 体验与缺陷清单见 [PRODUCT_ISSUES.md](PRODUCT_ISSUES.md)。按方向预存论文见 [PAPER_COLLECTION.md](PAPER_COLLECTION.md)。  
> 取证时间：2026-10-15。本机 `http://127.0.0.1:8100/`，生产 API `https://qiyan-caxplsowco.cn-hangzhou.fcapp.run`。  
> 本文给启研中期汇报用。Lab4 截止 10-18，仓库是 `pku-software/isse-labs`，不要和启研仓库混交。

---

## 1. 系统监控：指标选取与监控数据展示

课程要求：指标由应用**暴露**（Expose）→ Prometheus **刮取**成时间序列 → Grafana **展示** → Alertmanager **告警**。四种类型：Counter / Gauge / Histogram / Summary。

启研不是 Lab2 的 Flask `/hello`，指标按**生产事故手册**（DEPLOY F1–F8）和已开发模块来选，不抄通用 CPU 模板了事。

### 1.1 服务等级目标（SLO）

| SLO | 目标 | 窗口 | 量法 |
| --- | --- | --- | --- |
| 可用性 | 工作时间 99% | 7 天 | `qiyan_up == 1` 且 health 200 |
| 库持久 | 生产 `ephemeral == 0` | 持续 | `qiyan_db_ephemeral`，变 1 立即告警 |
| 前后端同版本 | Pages `wN` = health `checks.build` 里的 wN | 每次发版 | 探活脚本比对 |
| 探活延迟 | health p99 < 2s（含冷启动） | 24h | uptime + Histogram |
| 对话一轮 | p95 < 45s（含模型） | 上课周 | `qiyan_http_latency` 按 path |
| 5xx | 上课时段 < 1% | 1h | 5xx / 总请求（须先修好分路计数） |
| 论文库 | 工具包 10 篇 `paper_text` 在 | 发版后 10 分钟 | `qiyan_paper_text_rows` ≥ 10 |
| 方向目录 | 每个方向树节点目录 ≥ 20 篇（见入库计划） | 收集任务完成后 | `paper_dir` 按 node 计数 |

SLA：课程项目无对用户赔偿条款；组内约定工作时间 1 小时内响应探活红灯（DEPLOY §待确认 5）。

### 1.2 指标清单（已暴露 + 建议）

本机 **`GET /metrics`**（Prometheus 0.0.4 文本，无需新 pip 包）：

| 指标 | 类型 | 含义 | 对应课堂例子 |
| --- | --- | --- | --- |
| `qiyan_up` | Gauge | 进程活着 | 在线 |
| `qiyan_db_ephemeral` | Gauge | 1=临时盘 | **启研特有，比 CPU 更要紧** |
| `qiyan_llm_enabled` | Gauge | 兜底模型 | 功能是否降级 |
| `qiyan_users` | Gauge | 注册用户 | 发版前后对比（A5 验收） |
| `qiyan_sessions` / `_expired` | Gauge | 会话 | F1 被踢 |
| `qiyan_paper_text_rows` / `_bytes` | Gauge | 正文库 | 爬取/预热 |
| `qiyan_http_requests_total{path,code}` | Counter | 请求次数 | `hello_request_total` |
| `qiyan_http_errors_5xx_total` | Counter | 5xx | 错误数量 |
| `qiyan_http_latency_p95_seconds` | Gauge | 近 500 次 p95 | 延迟（进程内近似；完整分布见建议 Histogram） |
| `qiyan_process_uptime_seconds` | Gauge | 冷启动/回收 | FC 实例寿命 |

建议下一轮补 Histogram（真正 p95/p99）：`qiyan_http_latency_seconds_bucket{path,le}`；以及 `qiyan_llm_calls_total{tag}`（dialogue / paper-tutor / paper-quiz-grade / xlate）、`qiyan_llm_errors_total{tag}`。方向入库后加 `qiyan_paper_dir_nodes`、`qiyan_paper_dir_rows{node}`。现有 `llm_usage` 表和 `GET /api/admin/stats` 的 `today_star` / `xlate_today` / `top_uids` 已能做日级对账。

不要用的：health 里的 `model_pool.queued`（写死 0）。CPU/内存以函数计算控制台为准，应用里重复暴露意义不大。`/metrics` 生产环境必须加探活令牌或 IP 限制，否则会漏出用户规模和路径计数。

### 1.3 现在就能展示的数据（2026-10-15 实测）

**本机 health（演示库，非 ephemeral）：**

```json
{
  "ok": true,
  "llm": {"enabled": true, "model": "deepseek-chat"},
  "auth": {"wechat_login": false, "privacy_version": "2026-10-15"},
  "db": {"ephemeral": false},
  "checks": {"secret_set": false, "wechat_secure": false}
}
```

**生产 health：**

```json
{
  "ok": true,
  "llm": {"enabled": true, "model": "deepseek-flash"},
  "auth": {"privacy_version": "2026-10-13"},
  "db": {"ephemeral": true},
  "checks": {"secret_set": false, "build": "6344046 w74"}
}
```

**本机 `/metrics`（走查之后，进程 uptime ~349s）：**

```
qiyan_up 1
qiyan_db_ephemeral 0
qiyan_llm_enabled 1
qiyan_users 8
qiyan_sessions 11
qiyan_sessions_expired 0
qiyan_paper_text_rows 2
qiyan_paper_text_bytes 79991
qiyan_http_errors_5xx_total 1
qiyan_http_latency_p95_seconds 2.0308
qiyan_http_requests_total{path="/api/dialogue/turn",code="200"} 1
qiyan_http_requests_total{path="/api/daily",code="200"} 1
qiyan_http_requests_total{path="/api/papers/search",code="200"} 2
qiyan_http_requests_total{path="/api/papers/search",code="400"} 1
qiyan_http_requests_total{path="/api/tutor/2009.03300/turn",code="200"} 1
qiyan_http_requests_total{path="/metrics",code="200"} 2
```

从这张表能直接读出课堂要的四件事：有没有人访问（各 path 计数）、延迟是否变慢（p95≈2s，对话另计）、会不会被打爆（本机 5xx 计数）、错误是否增加。同时读出启研特有的事故信号：正文库只有 2 篇、生产 ephemeral 无法在现网刮到。

**本机走查时延：** 注册 156ms；对话一轮真实模型成功；今日 6 篇；MMLU 正文 html；带读 start 451ms、turn 113ms（命中规则/短判，不是长生成）；非法 id 400。

**GitHub Actions：** `uptime` 每 15 分钟；`tests` 在 w80 push 时已绿。Pages 探活用页面含 `build-stamp`。

**管理员统计**（需 `X-Admin-Token`）：`/api/admin/stats` → users、sessions、expired_sessions、arxiv/paper_text 字节、xlate_today、ephemeral。这是挂盘前后「用户数不变」的验收口。

### 1.4 展示怎么摆（Grafana）

1. 本机：Prometheus scrape `http://127.0.0.1:8100/metrics`，间隔 15s。  
2. 导入 `docs/grafana/qiyan-overview.json`（存活、ephemeral、模型、用户、p95、按 code 的 QPS、5xx、论文库、会话）。  
3. 生产：函数计算 **不能**让公网随便刮 `/metrics`（会把路径计数和用户规模漏出去）。做法：  
   - 控制台 SLS 日志查询（已有 JSON 行：`llm`、`warn: paper_cache_write`、`event: user_deleted`）；  
   - GitHub uptime 红/绿；  
   - 内网或带 `X-Admin-Token` 的 stats；  
   - 挂盘并给 `/metrics` 加 IP 限制或独立 probe 令牌后再刮。

汇报课上建议投影三张图，不必等 Grafana 上云：

1. 生产 health：`ephemeral: true`（问题）  
2. 本机 `/metrics` 文本（指标已暴露，且能看见 users / paper_text / 各 API 计数）  
3. GitHub Actions uptime 最近几次绿勾；并口头说明 Pages w80 ≠ API w74  

### 1.5 告警规则（建议）

| 条件 | 级别 | 动作 |
| --- | --- | --- |
| `qiyan_db_ephemeral == 1` 且环境=生产 | P0 | 冻结发版；走 A5 |
| health 连续 2 次失败 | P0 | `ALERT_WEBHOOK`（钉钉，待确认） |
| Pages `wN` ≠ API build | P1 | 只发前端或只发函数时必亮 |
| 5xx 率 > 5% / 10min | P1 | 看 SLS，F4 打满 / 模型失败 |
| `qiyan_users` 发版后骤降 | P0 | F1 库丢了 |
| `paper_text_rows` 发版后 < 10 | P2 | 预热失败 |
| 某方向 `paper_dir` < 20 | P2 | 入库任务未跑完或来源失败 |

uptime 工作流已存在；把 `secrets.ALERT_WEBHOOK` 和 `secrets.QIYAN_URL` 配上，失败才会打到群。未配时只在 Actions 红。建议追加：health 里 `ephemeral` 必须为 false 才算探活成功；Pages 戳与 `checks.build` 比对。

---

## 2. 系统运维：已解决 / 待解决 / 改进机会

课堂：「基于监控数据已解决或待解决的问题，或发现的改进机会。」DevOps：配置即代码、小步发布、回滚、质量贯穿运行。

### 2.1 已用监控/探活解决过的

| 问题 | 监控信号 | 处理 |
| --- | --- | --- |
| 发版清账号 | health `db.ephemeral` | deploy_fc 在 `/tmp` 时拒绝，除非 `--allow-ephemeral`；本轮**没发生产** |
| 多实例把会话拆两半 | 微信回调 + 网页轮询对不上 | `reserved_concurrency=1` |
| 论文写盘 500 | 10-13「爬取出了状况」 | 写盘失败 warn、非法 id 400、正文进 `paper_text` |
| 探活没有 | 人工看日志不现实（课件原话） | `.github/workflows/uptime.yml` 15 分钟 |
| 前端缓存错位 | CSS/JS 版本不一致 | 四个 `?v=` 与页脚同一号，测试钉死 |
| Pages 发布失败 | 2026-10-14 第一次 timeout | 再触发 pages/builds，第二次 27s 成功，现网 w80 |
| 应用没有 `/metrics` | Lab4 / 中期汇报要求 Expose | 本机已加；Grafana JSON 入库。**未发生产** |

### 2.2 待解决（按优先级）

1. **A5 挂 NAS**，`QIYAN_DB=/mnt/nas/qiyan.db`，health `ephemeral=false`，发两次版用户数不变。没做完不准把生产写成已完成。  
2. **前后端一起发**：挂盘后 `deploy_fc.py` + 已发布的 Pages w80；privacy 升到 2026-10-15。否则 ranking/搜索/PDF 继续 404/502。  
3. **把 `/metrics` 随函数发出去**（加探活令牌），Prometheus 刮生产。现在只有本机。  
4. **钉钉 webhook、RAM 子账号、OSS 7 天备份、SLS**（DEPLOY 待确认 5–7）。uptime 增加 ephemeral 断言和版本比对。  
5. 修 health `model_pool` 或删掉；修 5xx 总数与 `{path,code}` 不同步。  
6. 注册页先渲染 `PRIVACY_HTML` 再 `consent: true`。「我的」页接上 `/api/me/export`。  
7. 侧栏「连接模型」改成「管理员」或仅管理员可见。  
8. `choose` 与 `trail.dir`：丢失 trail 时不要用 7 码把 190.20 塌成 190.15（闸门单）。  
9. **按方向预存论文**：本机 `paper_text_rows=2`，且只有 `llm-eval` 工具包。每个方向树节点检索并入库，见 [PAPER_COLLECTION.md](PAPER_COLLECTION.md)。  
10. 定位/项目占位期间，考虑对未开发 API 返回 501，减少无效负载；删或停用死代码 `renderPosition` / `renderProjects`。

产品侧完整清单见 [PRODUCT_ISSUES.md](PRODUCT_ISSUES.md)。

### 2.3 改进机会（监控会推动的）

- **冷启动**：uptime 超时 20s；微信 5s。把 health 的 `process_uptime_seconds` 画出来，就能看见实例被回收的节奏，决定要不要最低实例数（花钱）。  
- **模型成本**：按 `tag` 计数后，上课周才能决定要不要把 `LLM_DAILY_TOTAL` 当硬闸（14 的 5000）。现在 lean budget 主要管 `*xlate`。  
- **今日取数**：给 `/api/daily` 单独 Histogram；冷话题「每请求最多 1 次」已经做了，缺的是线上是否真的 1 次。入库完成后今日应优先读目录，现场检索只补缺口。  
- **Pages 构建时长**：第一次 11 分钟超时，应把 `pages-build-deployment` 耗时纳入看板。  
- **带读质量**：标题当原句也能过线，应用 `paper-quiz-grade` 失败率/过线率做质量指标，不只做延迟。  
- **配置即代码**：`.env.example`、deploy 脚本、uptime 已在 git；Grafana JSON 已入库。生产环境变量仍在控制台手填，挂盘后应把「该有的键」写成检查项（health.checks 已有 secret/wechat/build）。

### 2.4 与课程工具链的对应（启研实际用的）

| 课堂举例 | 启研 |
| --- | --- |
| Prometheus scrape `/metrics` | 本机已有；生产待发版 |
| Grafana | `docs/grafana/qiyan-overview.json` |
| CI | GitHub Actions `tests.yml` |
| 容器 | `docker compose` 开发/测试 |
| 编排 K8s | 不用；阿里云函数计算单实例 |
| IaC Terraform | 未上；`deploy_fc.py` 当发布脚本 |
| Datadog/Kibana | 用 SLS + Actions，不新开 SaaS |
| Lab4 截止 10-18 | Lab 在 `pku-software/isse-labs`；**本文是给启研中期汇报（10-19）的产品监控方案** |

---

## 3. 本机验收命令

```
# 服务活着、库不在临时盘（本机 demo.db）
curl -fsS http://127.0.0.1:8100/api/health
curl -fsS http://127.0.0.1:8100/metrics

# 生产：现在应看到 ephemeral=true、build 含 w74、/metrics 404
curl -fsS https://qiyan-caxplsowco.cn-hangzhou.fcapp.run/api/health
curl -s -o NUL -w "%{http_code}" https://qiyan-caxplsowco.cn-hangzhou.fcapp.run/metrics

# 挂盘后才应通过
curl -fsS $PUBLIC_URL/api/health | python -c "import json,sys; d=json.load(sys.stdin); assert d['db']['ephemeral'] is False"
```

Prometheus 本机 `prometheus.yml` 片段：

```yaml
scrape_configs:
  - job_name: qiyan
    scrape_interval: 15s
    static_configs:
      - targets: ["127.0.0.1:8100"]
```
