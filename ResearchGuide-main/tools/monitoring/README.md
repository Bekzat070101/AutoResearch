# tools/monitoring · Prometheus + Grafana 看启研

- 数据从哪来：`server/metrics.py` 的 `GET /metrics`（Prometheus 文本格式，无第三方依赖）。进程内计数（请求数/状态码、按路由模板的延迟直方图、在途数、5xx、SSE 流错误）+ 每次刮取时读库（用户数、会话、论文库、账号副本）。
- 谁采集：本目录的 Prometheus（`prometheus.yml` 抓本机 `127.0.0.1:8100`；`prometheus-prod.yml` 再抓生产，Bearer 读 `.runtime/metrics-token`）。规则 `alerts.yml`，单测 `tests/alerts.test.yml`。
- 谁展示：本目录的 Grafana，provisioning 自动装数据源（uid `qiyan-prometheus`）与仪表盘 `grafana/dashboards/qiyan-overview.json`（19 个面板）。匿名可看，不开注册。
- 契约测试：`server/tests/test_metrics_sync.py` 核对面板与告警引用的每个指标族都在 `/metrics` 里、数据源 uid 一致、直方图桶界；`.tools/` 里有 promtool 时还跑 `promtool check rules` + `test rules`。改指标名先改这三处再跑它。

## 跑起来（本机，不要 Docker）

1. 二进制放 `.tools/`（不入库）：`./fetch-tools.zsh`（按 OS/架构下载 Prometheus 3.15.0 + Grafana 13.2.4 并校验 sha256）；已有解好的一份就 `QIYAN_TOOLS_FROM=<那份>/.tools ./fetch-tools.zsh` 软链过来。
2. 起 API：`cd ResearchGuide-main && LLM_API_KEY= uv run --no-project --offline --with fastapi --with uvicorn --with pydantic --with cryptography python server/main.py`（127.0.0.1:8100；`METRICS_TOKEN` 不设时 `/metrics` 只放行本机）。
3. `./start-prometheus.zsh` → http://127.0.0.1:9090/targets 里 `qiyan` 应为 UP。
4. `./start-grafana.zsh` → http://127.0.0.1:3000/d/qiyan-overview 。
5. 要线：`uv run --no-project --offline python tools/monitoring/smoke_traffic.py --seconds 120 --rps 3`（只建一个访客号，不调模型）。
- 运行数据都在 `.runtime/`（Prometheus TSDB 30 天、Grafana sqlite/日志）；删掉即重置。

## 抓生产

- 生产函数上设 `METRICS_TOKEN`（≥ 32 随机字节；`docs/DEPLOY.md`）。本机把同一串写进 `.runtime/metrics-token`（一行，不入库），`./start-prometheus.zsh --prod`。
- 目标域名在 `prometheus-prod.yml` 里，和 `.github/workflows/uptime.yml` 的 `QIYAN_URL` 保持一致。60 秒抓一次、超时 25 秒（冷启动）。
- `promtool check config prometheus-prod.yml` 需要 token 文件存在，否则报 `stat .runtime/metrics-token`；这是预期。

## 告警（alerts.yml）怎么读

- `QiyanDown`：2 分钟抓不到 → 进程没起来 / 网不通 / token 错。
- `QiyanDbNotReady`：`qiyan_db_ready == 0` 1 分钟 → NAS 没挂上，`/api/*` 在回 503 `db_not_ready`。
- `QiyanDbEphemeral`：生产 `qiyan_db_ephemeral == 1` → 库在临时盘，立刻按 DEPLOY.md 换到 NAS。
- `QiyanHigh5xxRatio`：10 分钟 5xx 比例 > 5% 且请求 ≥ 20，持续 5 分钟。
- `QiyanRegisteredUsersDrop`：注册用户比 15 分钟内峰值少 20% 以上且峰值 ≥ 10 → 换库/覆盖（10-18 那种），立即。
- `QiyanAccountSnapshotStale`：账号副本 3 天没写 → 没人登录或 dump 一直失败（看 health `db.account_store.last_error`）。
- `QiyanScrapeErrors` / `QiyanStreamErrors` / `QiyanDialogueSlow`：读库失败、SSE 错误 > 3 次/15 分钟、对话 p95 > 60 秒持续 10 分钟。
- 通知通道（钉钉/企微）由 Alertmanager 接，本目录不含；Alertmanager 配好后在 `prometheus*.yml` 加 `alerting.alertmanagers`。

## 指标名（面板和规则只用这些）

- 进程/环境：`qiyan_up`、`qiyan_process_uptime_seconds`（单调时钟，health `checks.uptime_seconds` 同源）、`qiyan_db_ephemeral`、`qiyan_db_ready`、`qiyan_llm_enabled`。
- 流量：`qiyan_http_requests_total{path,code}`（path 是路由模板，上限 200 个，溢出归 `other`；未匹配路由归 `unknown`）、`qiyan_http_inflight`、`qiyan_http_errors_5xx_total`、`qiyan_stream_errors_total`。
- 延迟：`qiyan_http_latency_seconds_{bucket,sum,count}{path}`，桶界 0.05/0.1/0.25/0.5/1/2/5/10/30/45/60/120/300；`qiyan_http_latency_p95_seconds` 是 legacy 全站近 500 次。
- 库：`qiyan_users{kind="registered|guest"}`（无标签的那条是总数）、`qiyan_sessions`、`qiyan_sessions_expired`、`qiyan_paper_text_rows{source}`、`qiyan_paper_text_pinned`、`qiyan_paper_text_bytes`、`qiyan_paper_dir_rows`、`qiyan_paper_dir_nodes`、`qiyan_scrape_errors_total`。
- 账号副本：`qiyan_account_snapshot_users`、`qiyan_account_snapshot_age_seconds`（-1 = 本进程没写过）。
