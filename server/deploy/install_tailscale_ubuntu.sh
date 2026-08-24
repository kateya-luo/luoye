#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
  echo "请使用普通 ubuntu 用户执行，本脚本会在需要时调用 sudo。" >&2
  exit 1
fi

curl -fsSL https://tailscale.com/install.sh | sh
sudo systemctl enable --now tailscaled

echo
echo "Tailscale 已安装。下一步执行："
echo "  sudo tailscale up --ssh"
echo "登录后查看私网地址："
echo "  tailscale ip -4"
