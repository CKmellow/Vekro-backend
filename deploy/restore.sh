#!/usr/bin/env bash
# Restore a backup made by deploy/backup.sh (docs/hosting.md "Restore").
#   deploy/restore.sh deploy/backups/vekro-20261109T020000Z.dump            # into a scratch DB
#   deploy/restore.sh deploy/backups/vekro-20261109T020000Z.dump --replace  # over production
# Without --replace it restores into vekro_restore_check and prints row counts, so the nightly
# file can be proven good (the monthly drill) without touching production.
set -euo pipefail

dump="${1:?usage: restore.sh <file.dump> [--replace]}"
mode="${2:-}"
here="$(cd "$(dirname "$0")" && pwd)"
compose=(docker compose -f "$here/compose.yaml" --env-file "$here/.env")
psql=("${compose[@]}" exec -T postgres psql -U vekro -v ON_ERROR_STOP=1)

[ -s "$dump" ] || { echo "no such backup: $dump" >&2; exit 1; }

if [ "$mode" = "--replace" ]; then
  read -r -p "Replace the production database with $dump? Type REPLACE: " answer
  [ "$answer" = "REPLACE" ] || { echo "cancelled"; exit 1; }
  "${compose[@]}" stop api scheduler web
  target=vekro
  "${psql[@]}" -d postgres -c "DROP DATABASE IF EXISTS vekro WITH (FORCE);" -c "CREATE DATABASE vekro OWNER vekro;"
else
  target=vekro_restore_check
  "${psql[@]}" -d postgres -c "DROP DATABASE IF EXISTS $target;" -c "CREATE DATABASE $target OWNER vekro;"
fi

"${compose[@]}" exec -T postgres pg_restore -U vekro -d "$target" --no-owner --exit-on-error <"$dump"

"${psql[@]}" -d "$target" -c "SELECT version_num AS schema FROM alembic_version;" \
  -c "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM transactions) AS orders, (SELECT count(*) FROM ledger_entries) AS ledger_rows, (SELECT count(*) FROM anchor_events) AS anchor_events;"

if [ "$mode" = "--replace" ]; then
  "${compose[@]}" up -d
  echo "restored into production and restarted"
else
  "${psql[@]}" -d postgres -c "DROP DATABASE $target;"
  echo "restore check passed: $dump is readable"
fi
