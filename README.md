# Kindle Dashboard NAS

Kindle 数据屏、局域网代理、Telegram MTProto 和 WhatsApp 代理的一体化 NAS 项目。

- 数据屏：行情、账号用量与 Kindle 状态渲染，保留原始 JSON 和图片接口。
- 同一个管理后台：节点显示、独立策略组、切换、延迟测试、订阅更新、模式和活动连接管理。
- Mihomo 提供 HTTP、HTTPS CONNECT 和 SOCKS5 出站代理。
- Telegram MTProto 与 WhatsApp 聊天/媒体转发共用一个 Python 进程，不需要额外管理软件。
- WhatsApp 有独立节点选择组、运行状态与局域网/外网地址显示。
- 包含 Docker Compose、首次启动脚本、1Panel 防火墙脚本、迁移工具和网络集成测试。

## 开始使用

```bash
cp .env.example .env
sudo python3 scripts/bootstrap.py
# 编辑 .env 中的 NAS 地址等配置；管理员密码会自动生成
# 首次部署默认 DIRECT，可在后台配置自己的订阅

docker compose up -d --build
```

管理后台：`http://NAS地址:8787/admin`。用户名默认 `admin`，密码保存在本机 `.env`。

完整说明：[部署与维护](docs/DEPLOYMENT.md)。原始数据屏使用说明：[原始 README](docs/ORIGINAL-README.md)。第三方来源与许可：[THIRD_PARTY.md](THIRD_PARTY.md)。

## 目录

| 文件 | 用途 |
|---|---|
| `app.py` | 数据屏渲染、后台页面和配置保存 |
| `proxy_admin.py` | 代理控制 API、节点管理页面和 WhatsApp 状态 |
| `mtproto/mtprotoproxy.py` | Telegram MTProto 实现及共享事件循环入口 |
| `mtproto/whatsapp_proxy.py` | 固定目标 WhatsApp 聊天/媒体转发 |
| `proxy/config.example.yaml` | 不含订阅和密钥的 Mihomo 初始模板 |
| `scripts/` | 本地配置初始化、1Panel 防火墙配置 |
| `migration/` | 本地私密备份与迁移工具 |
| `tests/` | SOCKS5、双向转发、聊天头和资源清理的网络集成测试 |

仓库只保存实现和安全模板；实际 `.env`、订阅、密钥、数据、日志和私密备份不上传。首次启动脚本会在本地生成独立密钥，重复运行保留已有凭据。

## 许可

本项目采用 [GNU AGPL-3.0](LICENSE)（SPDX: AGPL-3.0-only）。第三方实现保留各自原有许可，详见 THIRD_PARTY.md。

## 管理界面

后台采用深色侧边栏加浅色工作区，分为运行概览（状态横幅、实时流量）、代理节点（延迟条卡片）、连接服务（一键复制地址）、活动连接和系统设置；手机上自动变为底部标签栏。设计依据和验证范围见 [后台设计说明](docs/ADMIN-DESIGN.md)。
