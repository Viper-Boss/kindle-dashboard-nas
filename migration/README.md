# Kindle 数据屏私有迁移工具

这个工具会把 Dashboard、Mihomo、Docker 镜像以及当前真实配置打成一个离线迁移包。

## 一、在旧 OECT 上导出

把整个最新版项目覆盖到旧 OECT 的项目目录后执行：

```sh
cd "/vol2/1000/My Data/kindle-dashboard-nas"
chmod +x migration/export-migration.sh migration/import-migration.sh
./migration/export-migration.sh
```

项目目录中会生成：

```text
kindle-dashboard-private_日期时间.tar.gz
```

此文件包含 `.env`、后台设置、机场节点和 API 密钥等私密信息，请只在自己的设备之间传输。

## 二、在新 OECT 上导入

把生成的 `tar.gz` 上传到新机器，进入所在目录后执行：

```sh
tar -xzf kindle-dashboard-private_日期时间.tar.gz
cd kindle-dashboard-migration
chmod +x install.sh
./install.sh
```

默认安装到：

```text
/vol2/1000/My Data/kindle-dashboard-nas
```

若新机器目录不同，可把目标目录作为参数：

```sh
./install.sh "/你的/目标目录/kindle-dashboard-nas"
```

安装脚本如果发现目标目录已存在，会先将旧目录改名备份，不会直接删除。

## 三、IP 地址变化

服务端通常无需修改。若新 OECT 的局域网 IP 与 `192.168.120.200` 不同，需要修改 Kindle：

```text
/mnt/us/dashboard/local/env.sh
```

将其中服务器地址改为新 IP，然后重新启动 Dashboard 循环。

