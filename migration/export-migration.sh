#!/bin/sh
set -eu

# Run this script from anywhere on the source OECT. It exports the project,
# private runtime configuration, and all runtime Docker images into one archive.
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

umask 077
mkdir -p "$BUNDLE_DIR/project"

echo "[1/4] 检查并启动服务……"
cd "$PROJECT_DIR"
docker compose up -d

DASH_IMAGE=$(docker inspect kindle-dashboard --format '{{.Config.Image}}')
PROXY_IMAGE=$(docker inspect kindle-dashboard-proxy --format '{{.Config.Image}}')
MTPROTO_IMAGE=$(docker inspect nas-mtproto --format '{{.Config.Image}}' 2>/dev/null || true)
IMAGES="$DASH_IMAGE $PROXY_IMAGE"
if [ -n "$MTPROTO_IMAGE" ]; then
  IMAGES="$IMAGES $MTPROTO_IMAGE"
  printf '%s\n' "$MTPROTO_IMAGE" > "$WORK_DIR/mtproto-image.txt"
fi

echo "[2/4] 复制项目和私有配置……"
if [ -f "$PROJECT_DIR/data/traffic.sqlite3" ]; then
  docker exec kindle-dashboard python -c 'import traffic_stats; traffic_stats.collect_once(); traffic_stats.TrafficStore().backup("/data/migration-traffic-backup.sqlite3")'
fi
tar \
  --exclude='./migration' \
  --exclude='./data/traffic.sqlite3*' \
  --exclude='./data/auth.sqlite3*' \
  --exclude='./data/maintenance' \
  --exclude='./data/*-traffic-backup.sqlite3*' \
  --exclude='./security-staging' \
  --exclude='./.git' \
  --exclude='./__pycache__' \
  --exclude='./*.zip' \
  --exclude='./*.tar.gz' \
  --exclude='./app_backup_*' \
  --exclude='./docker-compose_backup_*' \
  -C "$PROJECT_DIR" -cf - . | tar -C "$BUNDLE_DIR/project" -xf -

if [ -f "$PROJECT_DIR/data/migration-traffic-backup.sqlite3" ]; then
  cp "$PROJECT_DIR/data/migration-traffic-backup.sqlite3" "$BUNDLE_DIR/project/data/traffic.sqlite3"
  rm -f "$PROJECT_DIR/data/migration-traffic-backup.sqlite3"
fi

echo "[3/4] 导出 Docker 镜像（可能需要几分钟）……"
# shellcheck disable=SC2086  # IMAGES 需要按空格拆分
docker save -o "$BUNDLE_DIR/images.tar" $IMAGES

printf '%s\n' "$DASH_IMAGE" > "$BUNDLE_DIR/dashboard-image.txt"
printf '%s\n' "$PROXY_IMAGE" > "$BUNDLE_DIR/proxy-image.txt"
[ -f "$WORK_DIR/mtproto-image.txt" ] && cp "$WORK_DIR/mtproto-image.txt" "$BUNDLE_DIR/mtproto-image.txt"
cp "$SCRIPT_DIR/import-migration.sh" "$BUNDLE_DIR/install.sh"
chmod +x "$BUNDLE_DIR/install.sh"

echo "[4/4] 生成迁移包……"
mkdir -p "$(dirname -- "$OUTPUT")"
tar -C "$WORK_DIR" -czf "$OUTPUT" kindle-dashboard-migration

echo
echo "完成：$OUTPUT"
echo "警告：此文件包含 API 密钥、机场配置等隐私数据，请勿公开分享。"

