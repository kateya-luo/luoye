# Tailscale 私网部署

Tailscale 在服务器、电脑和手机之间建立加密 WireGuard 私网。Clear Meeting 客户端可以直接连接服务器的 `100.x.x.x` 地址，不依赖域名、ICP备案或公网 WebSocket。

## 1. 云服务器安装

```bash
cd /home/ubuntu/ai-recorder-system/deploy
chmod +x install_tailscale_ubuntu.sh
./install_tailscale_ubuntu.sh
sudo tailscale up --ssh
tailscale ip -4
```

浏览器会显示登录链接。使用自己的 Tailscale 账号登录后，记下服务器的 `100.x.x.x` 地址。

## 2. 电脑和 Android 加入同一 Tailnet

- Windows：安装 Tailscale，登录与服务器相同的账号。
- Android：从官方应用商店安装 Tailscale，登录同一账号并开启连接。
- 客户端服务器地址填写 `http://服务器的100.x.x.x`。

## 3. 可选：停止公网暴露 Web 页面

先确认电脑和手机都能通过 Tailscale 地址打开页面，再修改 `deploy/.env`：

```dotenv
HTTP_BIND=服务器的100.x.x.x
```

重新创建 Nginx：

```bash
sudo docker compose --profile real-asr up -d --force-recreate nginx
```

此后公网 IP 的 80 端口不再提供页面，只能从 Tailnet 内访问。访问密码仍然保留，形成“私网 + 应用密码”两层保护。
