# 局域网代理与管理后台
主页：http://192.168.120.200:8787/
管理后台：http://192.168.120.200:8787/admin
管理员用户名：admin；沿用现有管理员密码。

混合 HTTP / SOCKS5：192.168.120.200:7890
SOCKS5（支持远程 DNS）：192.168.120.200:7891
HTTP（支持 HTTPS CONNECT）：192.168.120.200:7892
Telegram MTProto：192.168.120.200:8443；添加链接和密钥显示在登录后的后台。

节点显示、搜索、延迟排序、逐节点/整组测速、手动/自动选择、规则/全局/直连模式、订阅更新、流量和活动连接管理已接入内核。
当前规则使用数据屏代理组；选择自动选择时内核自动选择线路。
节点选择由 Mihomo profile.store-selected 保存。
后台保存订阅或 YAML 时会保留局域网端口和控制接口配置，备份旧配置并校验重载；校验失败恢复原配置。
管理接口只在 Docker 内网可达，未向宿主机发布控制端口。
所有容器设置 restart: always。MTProto 通过 Mihomo SOCKS5 出站。

升级前备份：backups/proxy-upgrade-20261011-133434/
后续后台配置备份：proxy/backups/
程序：app.py、proxy_admin.py
配置：docker-compose.yml、proxy/config.yaml
Telegram 配置：mtproto/config.py
Telegram 客户端信息：proxy/telegram.json

本次验证：管理接口鉴权、节点显示/切换、逐节点/整组延迟、订阅更新、模式切换、配置保存重载、节点重启恢复、局域网 HTTP/HTTPS CONNECT/SOCKS5 外网访问、MTProto 完整认证握手。
浏览器视觉检查未完成，自动审批系统返回额度不足。

MTProto 来源：https://github.com/alexbers/mtprotoproxy （stable 分支）
控制 API 文档：https://wiki.metacubex.one/api/
