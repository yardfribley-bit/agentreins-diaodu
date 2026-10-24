#!/bin/bash
# 在只出网的 Ubuntu 虚拟机上安装一个或多个 Linux 执行端。
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 运行：sudo ./install.sh [--name 实例名] [--with-tools]" >&2
  exit 1
fi

NAME="worker"
WITH_TOOLS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:-}"
      shift 2
      ;;
    --with-tools)
      WITH_TOOLS=1
      shift
      ;;
    *)
      echo "未知参数：$1" >&2
      echo "用法：sudo ./install.sh [--name 实例名] [--with-tools]" >&2
      exit 1
      ;;
  esac
done

if [[ ! "$NAME" =~ ^[a-z][a-z0-9-]{0,20}$ ]]; then
  echo "实例名只能是小写字母、数字和连字符，且以字母开头（最多 21 字符）" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="lw-${NAME}"
HOME_DIR="/var/lib/${USER_NAME}"
ENV_DIR="/etc/linux-worker"
ENV_FILE="${ENV_DIR}/${NAME}.env"
UNIT="linux-worker-${NAME}.service"

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir "$HOME_DIR" --create-home --shell /usr/sbin/nologin "$USER_NAME"
fi
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "${HOME_DIR}/workspace"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 0750 "${HOME_DIR}/workspace/tmp"

install -d -m 0755 /opt/linux-worker
rm -rf /opt/linux-worker/linux_worker
cp -a "${SRC}/linux_worker" /opt/linux-worker/linux_worker
find /opt/linux-worker/linux_worker -type d -exec chmod 0755 {} \;
find /opt/linux-worker/linux_worker -type f -exec chmod 0644 {} \;

install -d -m 0755 "$ENV_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  sed -e "s|__HOME__|${HOME_DIR}|g" -e "s|__NAME__|${NAME}|g" \
    "${SRC}/linux-worker.env.example" > "$ENV_FILE"
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  echo "已写入 ${ENV_FILE}"
  echo "请编辑其中的 LLM_API_KEY、LLM_MODEL、TEAM_INVITE_CODE 或 TOPIC_INVITE_CODE，以及 TARGET_ALLOWLIST。"
else
  chmod 0600 "$ENV_FILE"
  echo "保留已有配置 ${ENV_FILE}"
fi

# The cn-bj2 test failed on the original example values. Rewrite only those
# exact lines so an existing VM picks up the new defaults on reinstall.
ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os
from pathlib import Path
path = Path(os.environ["ENV_FILE"])
text = path.read_text(encoding="utf-8")
old_search = "SEARCH_URL=https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"
new_search = "SEARCH_URL=https://cn.bing.com/search?q={query}"
updated = text.replace(old_search, new_search)
lines = []
for line in updated.splitlines(keepends=True):
    if line.strip() == "MAX_STEPS=16":
        line = "MAX_STEPS=40" + ("\n" if line.endswith("\n") else "")
    lines.append(line)
new = "".join(lines)
if new != text:
    path.write_text(new, encoding="utf-8")
    os.chmod(path, 0o600)
    print("已把旧的 DuckDuckGo 搜索地址或 MAX_STEPS=16 更新为当前默认值")
PY

sed -e "s|__USER__|${USER_NAME}|g" -e "s|__HOME__|${HOME_DIR}|g" -e "s|__ENV__|${ENV_FILE}|g" \
  "${SRC}/linux-worker.service.in" > "/etc/systemd/system/${UNIT}"
chmod 0644 "/etc/systemd/system/${UNIT}"

if [[ "$WITH_TOOLS" -eq 1 ]]; then
  "${SRC}/install-tools.sh"
fi

systemctl daemon-reload
systemctl enable "$UNIT"

key_set=0
team_set=0
topic_set=0
if grep -Eq '^LLM_API_KEY=.+' "$ENV_FILE"; then key_set=1; fi
if grep -Eq '^TEAM_INVITE_CODE=.+' "$ENV_FILE"; then team_set=1; fi
if grep -Eq '^TOPIC_INVITE_CODE=.+' "$ENV_FILE"; then topic_set=1; fi
if [[ "$key_set" -eq 1 && ( "$team_set" -eq 1 || "$topic_set" -eq 1 ) ]]; then
  systemctl restart "$UNIT"
  echo "已启动 ${UNIT}"
else
  echo "服务已安装但未启动：${ENV_FILE} 里的 LLM_API_KEY 仍为空，或团队邀请与 Topic 码都为空。"
fi
