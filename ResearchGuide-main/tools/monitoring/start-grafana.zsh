#!/bin/zsh
# 打开 http://127.0.0.1:3000/ ：匿名可看，仪表盘「启研 · 服务总览」由 provisioning 自动装好。
set -eu
export QIYAN_MON_DIR="${0:A:h}"
cd "$QIYAN_MON_DIR"
[[ -x .tools/grafana/bin/grafana ]] || { echo "没有 .tools/grafana/bin/grafana：先跑 ./fetch-tools.zsh"; exit 1; }
mkdir -p .runtime/grafana/{data,logs,plugins}
mkdir -p grafana/provisioning/{plugins,alerting}
exec .tools/grafana/bin/grafana server \
  --homepath "$QIYAN_MON_DIR/.tools/grafana" \
  --config "$QIYAN_MON_DIR/grafana.ini"
