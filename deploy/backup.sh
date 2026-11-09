#!/usr/bin/env bash
# Nightly Postgres backup (M26, #285; docs/hosting.md "Backups").
#   deploy/backup.sh                 # writes deploy/backups/vekro-<UTC time>.dump
#   BACKUP_DIR=/srv/backups KEEP_DAYS=30 deploy/backup.sh
# A custom-format dump, compressed, taken inside the postgres container (consistent snapshot, no
# downtime). Files older than KEEP_DAYS are removed. Copy the directory off the server as well.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
compose=(docker compose -f "$here/compose.yaml" --env-file "$here/.env")
backup_dir="${BACKUP_DIR:-$here/backups}"
keep_days="${KEEP_DAYS:-14}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/vekro-$stamp.dump"

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

"${compose[@]}" exec -T postgres pg_dump -U vekro -d vekro --format=custom --compress=9 >"$target.partial"
# An empty or truncated dump must never replace a good one.
if [ ! -s "$target.partial" ]; then
  rm -f "$target.partial"
  echo "backup failed: empty dump" >&2
  exit 1
fi
mv "$target.partial" "$target"
chmod 600 "$target"

find "$backup_dir" -name 'vekro-*.dump' -type f -mtime "+$keep_days" -delete
echo "backup written: $target ($(du -h "$target" | cut -f1))"
