# 流量历史与组件升级

后台新增「流量统计」「组件升级」，沿用现有管理员登录。

## 每小时自动选择

通用代理的 URLTest 组及其引用的 provider health-check 都设为 interval: 3600、lazy: false、timeout: 5000、expected-status: 204；tolerance: 0。空闲时也会测试，结果更新后按可用节点延迟选路。已经建立的连接不会为此强制断开。Telegram、WhatsApp 独立 Selector 继续保持已验证的固定线路。

## 流量统计

- 按 Asia/Shanghai 自然日统计上传和下载；支持按日、按月、按年和客户端筛选，客户端备注、趋势及客户端 CSV 导出。
- 自启用起记录，不能恢复启用前的数据。SQLite 文件位于 data/traffic.sqlite3，长期保留日汇总；运行时计数只保留最近 35 天。
- Mihomo 传输计数钩子保留短连接，按传输发生的日期计数。TG/WhatsApp 在原有转发进程内按原始来源 IP 计数；内核中的转发容器流量被排除，避免重复。
- 每 10 秒累计写入 SQLite，按来源、进程 epoch、日期和客户端去重。容器重建保留数据库，正常关闭和升级前执行保存。突然断电可能损失尚未保存的一小段计数。
- 这是代理数据量，不是 NAS 网卡计费；TG/WhatsApp 包含客户端协议握手，通用代理为内核传输计数。路由器 NAT 隐藏来源时，多个设备会合并为同一 IP。IPv4 映射地址会归一化。
- 页首「全站今日、本月、今年」不受客户端筛选影响，趋势、服务和客户端列表均遵循筛选条件。
- 私有数据不提交 GitHub；迁移导出使用 SQLite 在线备份，而不直接打包正在变化的 WAL 数据库。

## 安装 NAS 升级服务

在项目目录使用 root 执行：

```sh
sudo python3 scripts/install-maintenance.py --project "$PWD" --current-commit <已部署的40位提交SHA>
```

要求 systemd、Docker Compose、出站访问 GitHub/镜像源和足够磁盘。维护服务为 kindle-maintenance.service；私有回退及构建状态存放 /var/lib/kindle-dashboard-nas，权限 700。Web 容器没有 Docker socket，仅能提交固定的 upgrade/rollback 请求。

「组件升级」显示后台、Mihomo、Python、Pillow、PyCryptodome、PySocks 的实际版本。手动检查读取固定仓库 Viper-Boss/kindle-dashboard-nas 的 main；升级该仓库锁定的组件组合，不会用不兼容的官方 latest 覆盖定制内核。

自动同步默认启用，每小时检查 main。仅部署对应提交的 security.yml 成功且已出现的 CodeQL 任务全部成功的版本。先下载并逐文件核对 Git blob，拉取锁定基础镜像，构建三个候选镜像，验证配置、程序导入与 Kindle 图像渲染，并用 Trivy 检查可修补漏洞。检查失败保持正式版本；应用阶段失败自动恢复上一版源码和镜像。升级会短暂重建代理，客户端需重连。

升级前备份源码、私有配置、SQLite 和镜像引用；部署保留现有 .env、订阅、密钥、代理配置及流量历史。手动回退保留当前历史数据并暂停自动同步，避免马上再次升级。重新开启可在页面操作。日志仅管理员可读，不接受任意命令、地址或文件路径。

迁移后请按上述命令在新 NAS 重新安装宿主机服务；迁移包不携带 systemd 安装状态。历史数据库也应定期通过私有迁移包另存。
