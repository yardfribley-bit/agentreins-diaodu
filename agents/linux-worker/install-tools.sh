#!/bin/bash
# 安装 Ubuntu/Debian 仓库里的基础侦察与诊断工具。
# 不安装漏洞利用框架、木马或口令爆破套件。网络访问仍受 TARGET_ALLOWLIST 约束。
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 运行" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "只支持 apt-get（Ubuntu/Debian）" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  nmap \
  curl \
  dnsutils \
  whois \
  netcat-openbsd \
  iproute2 \
  jq \
  ca-certificates
echo "已安装 nmap curl dnsutils whois netcat-openbsd iproute2 jq ca-certificates"
