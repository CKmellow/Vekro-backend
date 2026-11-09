#!/usr/bin/env bash
# Roll out new images on the VM (M27, #288). The deploy workflows of both repositories run it over
# SSH after publishing an image; it also works by hand:
#
#   deploy/deploy.sh api <tag>               # ghcr.io/ckmellow/vekro-api:<tag>
#   deploy/deploy.sh web <tag>               # ghcr.io/ckmellow/vekro-web:<tag>
#   deploy/deploy.sh both <api-tag> <web-tag>
#
# Order: pull the new images; run the migrations with the new API image while the old API keeps
# serving (migrations are additive, docs/hosting.md section 9); then restart the services. If the
# API or the web app is not healthy within HEALTH_TIMEOUT seconds (default 180), the previous images
# are put back and the script exits 1. A migration is never undone automatically.
#
# The chosen images are written to deploy/.env (VEKRO_API_IMAGE, VEKRO_WEB_IMAGE), so backup.sh,
# restore.sh and a plain `docker compose up -d` keep using them. One rollout at a time (flock).
set -euo pipefail

cd "$(dirname "$0")/.."
env_file=deploy/.env
compose=(docker compose -f deploy/compose.yaml --env-file "$env_file")
api_repo=${VEKRO_API_REPO:-ghcr.io/ckmellow/vekro-api}
web_repo=${VEKRO_WEB_REPO:-ghcr.io/ckmellow/vekro-web}
health_timeout=${HEALTH_TIMEOUT:-180}

die() {
  echo "deploy: $*" >&2
  exit 2
}
usage() { die "usage: deploy/deploy.sh api <tag> | web <tag> | both <api-tag> <web-tag>"; }

valid_tag() { [[ $1 =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; }

current() { grep -E "^$1=" "$env_file" | tail -n 1 | cut -d= -f2- || true; }

set_image() {
  local key=$1 value=$2
  if grep -qE "^$key=" "$env_file"; then
    sed -i "s#^$key=.*#$key=$value#" "$env_file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$env_file"
  fi
}

healthy() {
  local deadline=$((SECONDS + health_timeout)) api_id state
  while ((SECONDS < deadline)); do
    api_id=$("${compose[@]}" ps -q api)
    state=$(docker inspect -f '{{.State.Health.Status}}' "$api_id" 2>/dev/null || echo missing)
    if [[ $state == healthy ]] &&
      "${compose[@]}" exec -T web wget -qO- http://127.0.0.1:8080/api/health 2>/dev/null | grep -q '"ok"'; then
      return 0
    fi
    sleep 5
  done
  return 1
}

[[ -f $env_file ]] || die "$env_file is missing (docs/hosting.md section 6)"
target=${1:-}
case $target in
  api | web) if [[ $# -ne 2 ]] || ! valid_tag "$2"; then usage; fi ;;
  both) if [[ $# -ne 3 ]] || ! valid_tag "$2" || ! valid_tag "$3"; then usage; fi ;;
  *) usage ;;
esac

exec 9>deploy/.deploy.lock
flock -w 900 9 || die "another rollout is still running"

previous_api=$(current VEKRO_API_IMAGE)
previous_web=$(current VEKRO_WEB_IMAGE)
case $target in
  api) set_image VEKRO_API_IMAGE "$api_repo:$2" ;;
  web) set_image VEKRO_WEB_IMAGE "$web_repo:$2" ;;
  both)
    set_image VEKRO_API_IMAGE "$api_repo:$2"
    set_image VEKRO_WEB_IMAGE "$web_repo:$3"
    ;;
esac
echo "deploy: api=$(current VEKRO_API_IMAGE) web=$(current VEKRO_WEB_IMAGE)"

rollback() {
  echo "deploy: rolling back to api=${previous_api:-<none>} web=${previous_web:-<none>}" >&2
  if [[ -n $previous_api ]]; then set_image VEKRO_API_IMAGE "$previous_api"; fi
  if [[ -n $previous_web ]]; then set_image VEKRO_WEB_IMAGE "$previous_web"; fi
  "${compose[@]}" up -d --no-build --remove-orphans || true
  exit 1
}

"${compose[@]}" pull --quiet migrate api scheduler web || rollback
"${compose[@]}" up -d --no-build --wait postgres || rollback
echo "deploy: migrating (the running API keeps serving)"
"${compose[@]}" run --rm --no-deps migrate || rollback
"${compose[@]}" up -d --no-build --remove-orphans || rollback
if ! healthy; then
  echo "deploy: not healthy after ${health_timeout}s" >&2
  "${compose[@]}" logs --tail 40 api web >&2 || true
  rollback
fi
docker image prune -f >/dev/null || true
echo "deploy: done"
