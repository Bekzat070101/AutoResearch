#!/bin/zsh
# 把 Prometheus 与 Grafana 的官方二进制放到 .tools/（不入库）。按本机 OS/架构下载并校验 sha256。
# 已经有一份解好的（比如课程 lab4 目录里的 .tools）就不用下：QIYAN_TOOLS_FROM=/path/to/.tools ./fetch-tools.zsh 直接软链过来。
set -eu
QIYAN_MON_DIR="${0:A:h}"
cd "$QIYAN_MON_DIR"
PROM_VER="${PROM_VER:-3.15.0}"
GRAFANA_VER="${GRAFANA_VER:-13.2.4}"
GRAFANA_BUILD="${GRAFANA_BUILD:-38252365904}"   # dl.grafana.com 的包名带这串构建号；换版本时在发布页抄
mkdir -p .tools/downloads
if [[ -n "${QIYAN_TOOLS_FROM:-}" ]]; then
  for d in prometheus grafana; do
    [[ -d "$QIYAN_TOOLS_FROM/$d" ]] || { echo "缺 $QIYAN_TOOLS_FROM/$d"; exit 1; }
    rm -rf ".tools/$d"; ln -s "$QIYAN_TOOLS_FROM/$d" ".tools/$d"
  done
  echo "已软链 $QIYAN_TOOLS_FROM 的 prometheus/ 与 grafana/"; exit 0
fi
case "$(uname -s)" in Darwin) os=darwin;; Linux) os=linux;; *) echo "只支持 macOS / Linux；Windows 请手动解压到 .tools/"; exit 1;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64;; x86_64|amd64) arch=amd64;; *) echo "未知架构 $(uname -m)"; exit 1;; esac
P="prometheus-${PROM_VER}.${os}-${arch}.tar.gz"
G="grafana_${GRAFANA_VER}_${GRAFANA_BUILD}_${os}_${arch}.tar.gz"
cd .tools/downloads
[[ -s "$P" ]] || curl -fL --retry 2 -C - -o "$P" "https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/${P}"
curl -fL --retry 2 -o "prometheus-${PROM_VER}.sha256sums.txt" "https://github.com/prometheus/prometheus/releases/download/v${PROM_VER}/sha256sums.txt"
grep " ${P}\$" "prometheus-${PROM_VER}.sha256sums.txt" | shasum -a 256 -c -
[[ -s "$G" ]] || curl -fL --retry 2 -C - -o "$G" "https://dl.grafana.com/grafana/release/${GRAFANA_VER}/${G}"
curl -fL --retry 2 -o "${G}.sha256" "https://dl.grafana.com/grafana/release/${GRAFANA_VER}/${G}.sha256"
echo "$(cat "${G}.sha256" | awk '{print $1}')  ${G}" | shasum -a 256 -c -
cd ..
rm -rf prometheus grafana
mkdir -p prometheus grafana
tar -xzf "downloads/$P" -C prometheus --strip-components=1
tar -xzf "downloads/$G" -C grafana --strip-components=1
echo "ok: $(prometheus/prometheus --version 2>&1 | head -1); $(grafana/bin/grafana --version 2>&1 | head -1)"
