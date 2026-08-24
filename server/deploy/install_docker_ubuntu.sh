#!/usr/bin/env bash
set -euo pipefail

if [[ "$(. /etc/os-release && echo "${ID}-${VERSION_ID}")" != "ubuntu-22.04" ]]; then
  echo "警告：此脚本面向 Ubuntu 22.04，当前系统为 $(. /etc/os-release && echo "${PRETTY_NAME}")。"
fi

# 腾讯云国内网络可能无法访问 download.docker.com，使用 Ubuntu 仓库版本。
sudo apt-get update
sudo apt-get install -y ca-certificates curl docker.io docker-compose-v2

# 腾讯云容器镜像加速；只在配置不存在时创建，避免覆盖已有 Docker 设置。
sudo install -m 0755 -d /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  printf '%s\n' '{"registry-mirrors":["https://mirror.ccs.tencentyun.com"]}' \
    | sudo tee /etc/docker/daemon.json >/dev/null
fi

sudo systemctl enable --now docker
sudo systemctl restart docker
sudo usermod -aG docker "${SUDO_USER:-$USER}"

sudo docker version
sudo docker compose version
echo "Docker 安装完成。请退出 SSH 后重新登录，使 docker 用户组权限生效。"
