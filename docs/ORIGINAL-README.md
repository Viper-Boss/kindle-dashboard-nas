# Kindle Dashboard NAS

给越狱 Kindle 提供墨水屏友好的 `/dashboard.png`。

默认服务地址：

```text
http://192.168.120.200:8787/dashboard.png
```

## 飞牛 NAS 部署

1. 把本目录上传到飞牛 NAS，例如：

```text
/vol1/1000/docker/kindle-dashboard-nas
```

2. 复制环境变量文件：

```bash
cp .env.example .env
```

3. 启动：

```bash
docker compose up -d --build
```

已有旧版时，覆盖 `app.py`、`.env.example`、`README.md` 后，在原有 `.env` 中加入：

```bash
OPENAI_ADMIN_KEY=你的OpenAI管理员Key
ANTHROPIC_ADMIN_KEY=你的Anthropic管理员Key
```

不要把真实 Key 写进 `.env.example`。更新代码后执行一次 `docker compose up -d --build`。

4. 浏览器测试：

```text
http://192.168.120.200:8787/dashboard.png
http://192.168.120.200:8787/data.json
```

5. Kindle 上打开 KUAL：

```text
Kindle Dashboard → Preview dashboard once
```

成功后再点：

```text
Kindle Dashboard → Start dashboard loop
```

## 网页管理后台

先在 `.env` 设置后台账号：

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请设置一个足够长的随机密码
```

重建容器后访问：

```text
http://192.168.120.200:8787/admin
```

后台可以直接修改 OpenAI/Anthropic Admin Key、token 预算、刷新时间、行情节点顺序、HTTP 代理地址和面板备注。配置保存在 `/data/dashboard_config.json`，保存后立即生效，无需重启主容器。Key 不会回显到网页。

不要把 8787 端口映射到公网；后台仅供家庭局域网使用。

## 机场代理（可选）

不建议给整个 NAS 安装全局 VPN。本项目提供可选 Mihomo sidecar，只代理数据屏的外部 HTTP/HTTPS 请求：

1. 打开网页后台，把机场提供的 Clash/Mihomo **订阅链接**粘贴到“机场配置”并保存；后台会自动生成 YAML。
2. 如果机场只提供普通订阅而非 Clash/Mihomo 格式，请在机场后台切换或复制“Clash / Mihomo 订阅链接”。高级用户仍可直接粘贴完整 YAML。
3. 在项目目录启动代理容器：

```bash
docker compose up -d
```

4. 回到后台，将“HTTP 代理地址”设为：

```text
http://mihomo:7890
```

Mihomo 现在默认随 Compose 启动。数据屏会等待代理端口健康后再启动；开机早期行情失败时每 15 秒重试，不再缓存空白行情 5 分钟。首次启动或替换机场 YAML 后，在飞牛 Docker 页面重启 `kindle-dashboard-proxy`。代理端口没有映射到宿主机，只能由本 Compose 项目内的容器访问；`allow-lan: true` 仅允许同项目容器访问，不开放 NAS 主机端口。

## 可配置项

- `CODEX_3H_PERCENT` / `CODEX_5H_PERCENT` / `CODEX_TODAY_PERCENT` / `CODEX_WEEK_PERCENT`：Codex 多周期额度条。
- `CLAUDE_3H_PERCENT` / `CLAUDE_5H_PERCENT` / `CLAUDE_TODAY_PERCENT` / `CLAUDE_WEEK_PERCENT`：Claude Code 多周期额度条。
- `OPENAI_ADMIN_KEY`：读取 OpenAI 组织 API 的真实 token 用量。
- `ANTHROPIC_ADMIN_KEY`：读取 Anthropic 组织 API 的真实 token 用量。
- `STATE_FILE`：Kindle 状态回传文件，默认 `/data/kindle_state.json`。
- `CODEX_USAGE_FILE` / `CLAUDE_USAGE_FILE`：可选的实时额度 JSON 文件；每次刷新面板都会重新读取，不需要重启容器。

注意：这里展示的是两个组织 API 的真实 token 用量，不是 ChatGPT/Codex 或 Claude Pro/Max 的订阅剩余额度。普通模型调用 Key 没有组织用量报告权限，必须使用 Admin Key。

币价节点按 `OKX → CryptoCompare → CoinGecko` 自动切换。前一个节点无法访问或数据缺失时，会自动尝试下一个；全部失败时保留进程内最近一次成功数据。

## 当前面板样式

- 左上角：`Kindle数据屏`。
- 右上角：日期、星期几、时间。
- 上方两个小方块：BTC/EUR 和 ETH/EUR 价格、24h 涨跌幅、折线图。
- 中间：Codex 3小时、5小时、今日、本周额度。
- 下方：Claude Code 3小时、5小时、今日、本周额度。
- 最底部：`信息获取时间：YYYY年M月D日HH:MM:SS`。

额度条可以在 `.env` 中这样设置：

```bash
CODEX_3H_PERCENT=82
CODEX_5H_PERCENT=72
CODEX_TODAY_PERCENT=58
CODEX_WEEK_PERCENT=41

CLAUDE_3H_PERCENT=22
CLAUDE_5H_PERCENT=35
CLAUDE_TODAY_PERCENT=46
CLAUDE_WEEK_PERCENT=63
```

更推荐使用实时 JSON 文件。把下面文件放进 NAS 项目的 `data` 目录：

```json
{
  "text": "账号额度自动同步",
  "quotas": {
    "3h": {"percent": 82, "text": "剩余 82%"},
    "5h": {"percent": 72, "text": "剩余 72%"},
    "today": {"percent": 58, "text": "今日剩余 58%"},
    "week": {"percent": 41, "text": "本周剩余 41%"}
  }
}
```

分别保存为 `data/codex_usage.json` 和 `data/claude_usage.json`。之后只要外部脚本定时覆盖这两个文件，面板会自动读取新值；Docker 容器不需要重启。百分比是已用比例，范围为 0–100。

## Kindle 电量显示

NAS 本身不知道 Kindle 电量；需要 Kindle 在刷新前把电量回传给 NAS：

```text
http://192.168.120.200:8787/update?kindle_battery=87
```

回传后，面板会显示 `Kindle电量：87%`。
