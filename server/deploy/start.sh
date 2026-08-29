#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${SCRIPT_DIR}"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "已从 .env.example 创建 deploy/.env。"
  echo "请先替换 TEST_ACCOUNT_PASSWORD、AUTH_SECRET 和 DEVICE_API_SECRET 占位符，再重新运行。" >&2
  exit 2
fi

read_env_value() {
  sed -n "s/^${1}=//p" .env | tail -n 1
}

for key in TEST_ACCOUNT_PASSWORD AUTH_SECRET DEVICE_API_SECRET; do
  value="$(read_env_value "${key}")"
  if [[ -z "${value}" || "${value}" == CHANGE_ME* ]]; then
    echo "拒绝启动：deploy/.env 中的 ${key} 仍为空或占位符。" >&2
    exit 2
  fi
  minimum_length=32
  [[ "${key}" == "TEST_ACCOUNT_PASSWORD" ]] && minimum_length=12
  if (( ${#value} < minimum_length )); then
    echo "拒绝启动：deploy/.env 中的 ${key} 长度少于 ${minimum_length} 字符。" >&2
    exit 2
  fi
done

mkdir -p \
  "${PROJECT_DIR}/server/data/transcripts" \
  "${PROJECT_DIR}/server/data/summaries" \
  "${PROJECT_DIR}/server/data/audio_cache"

docker compose --profile real-asr up -d --build
docker compose ps
bash "${SCRIPT_DIR}/check.sh"
echo "启动完成：局域网请访问 http://192.168.31.183/"
