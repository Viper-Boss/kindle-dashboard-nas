#!/bin/sh
set -eu

# Run this script from anywhere on the source OECT. It exports the project,
# private runtime configuration, and both Docker images into one archive.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
STAMP=$(date '+%Y%m%d_%H%M%S')
WORK_DIR="${TMPDIR:-/tmp}/kindle-dashboard-migration-$STAMP"
BUNDLE_DIR="$WORK_DIR/kindle-dashboard-migration"
OUTPUT="${1:-$PROJECT_DIR/kindle-dashboard-private-$STAMP.tar.gz}"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null 2>&1 || { echo "错误：未找到 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "错误：未找到 docker compose"; exit 1; }

mkdir -p "$BUNDLE_DIR/project"

echo "[1/4] 检查并启动服务……"
cd "$PROJECT_DIR"
docker compose up -d

DASH_IMAGE=$(docker inspect kindle-dashboard --format '{{.Config.Image}}')
PROXY_IMAGE=$(docker inspect kindle-dashboard-proxy --format '{{.Config.Image}}')

echo "[2/4] 复制项目和私有配置……"
tar \
  --exclude='./migration' \
  --exclude='./__pycache__' \
  --exclude='./*.zip' \
  --exclude='./*.tar.gz' \
  --exclude='./app_backup_*' \
  --exclude='./docker-compose_backup_*' \
  -C "$PROJECT_DIR" -cf - . | tar -C "$BUNDLE_DIR/project" -xf -

echo "[3/4] 导出 Docker 镜像（可能需要几分钟）……"
docker save -o "$BUNDLE_DIR/images.tar" "$DASH_IMAGE" "$PROXY_IMAGE"

printf '%s\n' "$DASH_IMAGE" > "$BUNDLE_DIR/dashboard-image.txt"
printf '%s\n' "$PROXY_IMAGE" > "$BUNDLE_DIR/proxy-image.txt"
cp "$SCRIPT_DIR/import-migration.sh" "$BUNDLE_DIR/install.sh"
chmod +x "$BUNDLE_DIR/install.sh"

echo "[4/4] 生成迁移包……"
mkdir -p "$(dirname -- "$OUTPUT")"
tar -C "$WORK_DIR" -czf "$OUTPUT" kindle-dashboard-migration

echo
echo "完成：$OUTPUT"
echo "警告：此文件包含 API 密钥、机场配置等隐私数据，请勿公开分享。"

