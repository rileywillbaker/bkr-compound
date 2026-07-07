#!/usr/bin/env bash
# B-Quant database backup (Linux/VPS host). Keeps the last KEEP dumps.
# Schedule via cron, e.g.:  30 23 * * * /opt/bkr-compound/scripts/backup.sh
set -euo pipefail

OUT_DIR="${1:-$(dirname "$0")/../backups}"
KEEP="${KEEP:-14}"
mkdir -p "$OUT_DIR"

STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="$OUT_DIR/bquant-$STAMP.dump"

docker compose exec -T db pg_dump -U bquant -d bquant -Fc > "$FILE"
[ -s "$FILE" ] || { rm -f "$FILE"; echo "pg_dump produced empty file" >&2; exit 1; }
echo "wrote $FILE"

# Retention
ls -1t "$OUT_DIR"/bquant-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | xargs -r rm --

# Restore (reference):
#   docker compose exec -T db pg_restore -U bquant -d bquant --clean --if-exists < backups/bquant-<stamp>.dump
