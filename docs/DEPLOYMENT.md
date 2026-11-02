# 部署与维护

## 首次部署

需要 Docker、Docker Compose 和 Python 3。项目目录必须允许 Docker 读取源码、写入 `proxy/` 和 `data/`。飞牛 NAS 的 ACL 与普通 Linux 文件模式可能不同，容器日志出现权限错误时检查绑定目录权限。

```bash
cp .env.example .env
sudo python3 scripts/bootstrap.py
# 编辑 .env，确认 NAS_BIND_IP、NAS_LAN_HOST 和管理员密码
docker compose up -d --build
```

后台入口是 `http://NAS地址:8787/admin`，默认用户名 `admin`，密码在本机 `.env` 中。第一次启动的代理模板使用 DIRECT；进入后台添加 Clash/Mihomo 订阅后才会有可选节点。不要把真实 `.env`、订阅链接和密钥加入 Git。

## 端口

| 端口 | 协议 | 用途 | 默认范围 |
|---|---|---|---|
| 8787 | TCP | Kindle 数据屏与管理后台 | NAS 绑定地址 |
| 7890 | TCP | HTTP/SOCKS5 混合代理 | NAS 绑定地址，内核限制局域网 |
| 7891 | TCP/UDP | SOCKS5 | NAS 绑定地址，内核限制局域网 |
| 7892 | TCP | HTTP/HTTPS CONNECT | NAS 绑定地址，内核限制局域网 |
| 8443 | TCP | Telegram MTProto | NAS 绑定地址，需要客户端密钥 |
| 5222 | TCP | WhatsApp 聊天 | `WHATSAPP_BIND_IP` |
| 7777 | TCP | WhatsApp 媒体 | `WHATSAPP_BIND_IP` |

控制接口 9090 和 WhatsApp 状态接口 8766 只在 Docker 网络内使用，不对宿主机发布。

在 1Panel v2.2.4、iptables 后端上可通过本地 agent 添加规则并保留数据库备注：

```bash
sudo python3 scripts/configure_1panel_firewall.py
# 如明确需要 WhatsApp 外网访问：
sudo python3 scripts/configure_1panel_firewall.py --public-whatsapp
```

脚本在修改前备份规则和 agent 数据库；不会停用防火墙或重启 Docker。不同 1Panel 版本的 API 可能不同，先检查脚本和接口。

## WhatsApp 使用

进入 WhatsApp → 设置 → 存储和数据 → 代理 → 设置代理。主机填 NAS 的局域网 IP；在高级设置中将聊天端口设为 `5222`、媒体端口设为 `7777`。无需用户名和密码。它是 WhatsApp 固定目标 TCP 转发，不是 HTTP 或 SOCKS 入口；不支持代理语音/视频通话。

后台的 `WhatsApp代理` 节点组可以独立切换线路，不影响原先的数据屏/TG 节点。聊天流量转发到官方 `g.whatsapp.net:5222` 并添加 PROXY v1 头；媒体流量转发到官方 `whatsapp.net:443`。两个服务共用现有 MTProto Python 进程及其事件循环，均通过 Mihomo SOCKS5 出站。

外网 IPv4 访问需要可用的公网 IP，以及路由器把 TCP 5222、7777 转发到 NAS 相同端口；有 CGNAT 时单独放行服务器防火墙不够。IPv6 需要在 Compose 中发布 IPv6 端口，例如添加 `"[::]:5222:5222/tcp"`、`"[::]:7777:7777/tcp"`，并检查服务器及路由器 IPv6 防火墙。后台可填写外网域名或 IP，仅用于显示连接信息。

## 升级、日志与回退

修改前备份 `app.py`、`proxy_admin.py`、`docker-compose.yml`、`mtproto/`、`.env`、`proxy/` 和 `data/`。其中包含凭据的备份只应保存在可信本地位置，不能推送 GitHub。

```bash
docker compose config --quiet
docker compose up -d --build
docker compose logs --tail 80 kindle-dashboard mtproto mihomo
python3 -m unittest discover -s tests -v
```

后台保存代理配置会生成 `proxy/backups/` 备份，并调用内核校验重载；失败会恢复旧配置。节点选择通过 Mihomo `profile.store-selected` 保存。

原有包含私密数据的迁移功能保留在 `migration/`，使用前阅读其说明。仓库不包含个人数据屏状态、账户 Key、代理订阅、生成的 MTProto 密钥或历史私密压缩包。
