# 第三方实现

- Telegram MTProto 实现：<https://github.com/alexbers/mtprotoproxy>，原项目 MIT 许可见 `mtproto/LICENSE`；本项目在主事件循环中增加 WhatsApp 转发服务。原版 pyaes 及其许可随源码保留。
- Mihomo：<https://github.com/MetaCubeX/mihomo>，GPL-3.0，作为独立进程运行。`mihomo/Dockerfile` 固定 v1.19.33 对应提交 `3f27ef39f8c96bbfbc8b0b9bb10f5c9ef60149d8` 和源码 SHA-256，`mihomo/patch-dependencies.sh` 记录依赖修补，保留上游 gVisor 功能。完整对应源码由公开上游提交和本仓库构建配方恢复；镜像内保留 `/LICENSE.mihomo`。
- WhatsApp 端口与上游协议依据官方实现：<https://github.com/WhatsApp/proxy>。本项目使用 Python 实现固定目标的 TCP 转发，并按官方聊天后端添加 PROXY v1 头。
- Python 依赖：Pillow、PySocks、PyCryptodome，分别遵循各自上游许可。

本项目新编写的实现采用 GNU Affero General Public License v3.0（AGPL-3.0-only），见根目录 LICENSE。原有第三方 MIT 文件保留其上游许可和版权声明。

- 后台内嵌字体 IBM Plex Sans（Fontsource 提供 WOFF2）：SIL Open Font License，原文见 docs/IBM-Plex-LICENSE.txt。

- Mihomo 流量钩子与统计接口位于 mihomo/traffic 和 mihomo/tools/patch-traffic，采用 AGPL-3.0-only；结合后的内核遵循 AGPLv3，原 GPLv3 许可保留。镜像包含 /LICENSE.mihomo 和 /LICENSE.kindle，通过固定上游源码与本仓库补丁可重建对应版本。
