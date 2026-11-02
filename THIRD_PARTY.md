# 第三方实现

- Telegram MTProto 实现：<https://github.com/alexbers/mtprotoproxy>，原项目 MIT 许可见 `mtproto/LICENSE`；本项目在主事件循环中增加 WhatsApp 转发服务。原版 pyaes 及其许可随源码保留。
- Mihomo：<https://github.com/MetaCubeX/mihomo>，作为独立 Docker 镜像运行，未将其源代码复制到本仓库。
- WhatsApp 端口与上游协议依据官方实现：<https://github.com/WhatsApp/proxy>。本项目使用 Python 实现固定目标的 TCP 转发，并按官方聊天后端添加 PROXY v1 头。
- Python 依赖：Pillow、PySocks、PyCryptodome，分别遵循各自上游许可。

本项目新编写的实现采用 GNU Affero General Public License v3.0（AGPL-3.0-only），见根目录 LICENSE。原有第三方 MIT 文件保留其上游许可和版权声明。
