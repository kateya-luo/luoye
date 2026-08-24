#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "== ClearMeeting v0.14.4 容器状态 =="
docker compose ps

running_services="$(docker compose ps --status running --services)"
for service in server nginx; do
  if ! grep -qx "${service}" <<<"${running_services}"; then
    echo "失败：${service} 未运行" >&2
    exit 1
  fi
done

bash "${SCRIPT_DIR}/verify_api_v2.sh" "${1:-http://127.0.0.1}"
