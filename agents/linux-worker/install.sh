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
  echo "请编辑其中的 LLM_API_KEY、LLM_MODEL、TEAM_INVITE_CODE 和 TARGET_ALLOWLIST。"
else
  chmod 0600 "$ENV_FILE"
  echo "保留已有配置 ${ENV_FILE}"
fi

sed -e "s|__USER__|${USER_NAME}|g" -e "s|__HOME__|${HOME_DIR}|g" -e "s|__ENV__|${ENV_FILE}|g" \
  "${SRC}/linux-worker.service.in" > "/etc/systemd/system/${UNIT}"
chmod 0644 "/etc/systemd/system/${UNIT}"

if [[ "$WITH_TOOLS" -eq 1 ]]; then
  "${SRC}/install-tools.sh"
fi

systemctl daemon-reload
systemctl enable "$UNIT"

if grep -Eq '^LLM_API_KEY=.+' "$ENV_FILE" && grep -Eq '^TEAM_INVITE_CODE=.+' "$ENV_FILE"; then
  systemctl restart "$UNIT"
  echo "已启动 ${UNIT}"
else
  echo "服务已安装但未启动：${ENV_FILE} 里的 LLM_API_KEY 或 TEAM_INVITE_CODE 仍为空。"
fi
