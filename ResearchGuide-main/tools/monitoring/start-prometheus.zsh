#!/bin/zsh
# 用法：./start-prometheus.zsh            只抓本机 API（127.0.0.1:8100）
#       ./start-prometheus.zsh --prod     同时抓生产；要先把 METRICS_TOKEN 写进 .runtime/metrics-token
set -eu
QIYAN_MON_DIR="${0:A:h}"
cd "$QIYAN_MON_DIR"
CFG=prometheus.yml
if [[ "${1:-}" == "--prod" ]]; then
  [[ -s .runtime/metrics-token ]] || { echo "缺 .runtime/metrics-token（内容就是生产函数的 METRICS_TOKEN，一行）"; exit 1; }
  CFG=prometheus-prod.yml
fi
[[ -x .tools/prometheus/prometheus ]] || { echo "没有 .tools/prometheus/prometheus：先跑 ./fetch-tools.zsh"; exit 1; }
mkdir -p .runtime/prometheus
.tools/prometheus/promtool check config "$CFG" >/dev/null
exec .tools/prometheus/prometheus \
  --config.file="$QIYAN_MON_DIR/$CFG" \
  --storage.tsdb.path="$QIYAN_MON_DIR/.runtime/prometheus" \
  --storage.tsdb.retention.time=30d \
  --web.listen-address=127.0.0.1:9090
