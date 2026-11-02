#!/bin/sh
set -eu

# This file becomes install.sh inside the exported private bundle.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEFAULT_TARGET="/vol2/1000/My Data/kindle-dashboard-nas"
TARGET_DIR="${1:-$DEFAULT_TARGET}"

command -v docker >/dev/null 2>&1 || { echo "错误：未找到 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "错误：未找到 docker compose"; exit 1; }
test -f "$SCRIPT_DIR/images.tar" || { echo "错误：迁移包缺少 images.tar"; exit 1; }
test -f "$SCRIPT_DIR/project/docker-compose.yml" || { echo "错误：迁移包缺少项目文件"; exit 1; }

echo "安装目录：$TARGET_DIR"
if [ -e "$TARGET_DIR" ]; then
  BACKUP="${TARGET_DIR}_backup_$(date '+%Y%m%d_%H%M%S')"
  echo "发现旧目录，先备份到：$BACKUP"
  mv "$TARGET_DIR" "$BACKUP"
fi

echo "[1/4] 恢复项目和配置……"
mkdir -p "$TARGET_DIR"
tar -C "$SCRIPT_DIR/project" -cf - . | tar -C "$TARGET_DIR" -xf -

echo "[2/4] 导入 Docker 镜像……"
docker load -i "$SCRIPT_DIR/images.tar"

echo "[3/4] 启动容器……"
cd "$TARGET_DIR"
docker compose up -d --no-build

echo "[4/4] 等待健康检查……"
i=0
while [ "$i" -lt 36 ]; do
  if wget -q -T 3 -O /dev/null http://127.0.0.1:8787/dashboard.png 2>/dev/null; then
    echo
    echo "迁移成功。后台：http://本机IP:8787/admin"
    echo "数据屏：http://本机IP:8787/dashboard.png"
    exit 0
  fi
  i=$((i + 1))
  sleep 5
done

echo "容器已启动，但 3 分钟内未通过网页检查。请运行："
echo "  cd \"$TARGET_DIR\""
echo "  docker compose ps"
echo "  docker compose logs --tail=100"
exit 1

