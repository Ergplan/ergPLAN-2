#!/usr/bin/env bash
# Remote access for the Mac mini server through a Cloudflare tunnel (docker/compose.remote.yaml).
#   tools/dev/remote.sh on      start the tunnel, point the app at its https address, print the address
#   tools/dev/remote.sh off     stop the tunnel and go back to the address on the local network
#   tools/dev/remote.sh url     print the address in use
# With CLOUDFLARE_TUNNEL_TOKEN and REMOTE_URL (https://ergplan.yourcompany.in) in .env the address is
# fixed; without them it is a random trycloudflare.com address that changes at every start.
# While remote access is on, everyone signs in at the https address (the sign-in cookie is https-only).
set -euo pipefail
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "no .env here: run the installer first" >&2; exit 1; }
C=(docker compose --env-file .env -f docker/compose.yaml -f docker/compose.prod.yaml -f docker/compose.mini.yaml -f docker/compose.remote.yaml)
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
put() {  # put KEY VALUE: replace or add a line in .env, keeping its mode
  local tmp; tmp="$(mktemp)"
  grep -vE "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env; rm -f "$tmp"
}
case "${1:-}" in
  on)
    [ -n "$(get LAN_URL)" ] || put LAN_URL "$(get ERGPLAN_PUBLIC_URL)"
    if [ -n "$(get CLOUDFLARE_TUNNEL_TOKEN)" ]; then
      url="$(get REMOTE_URL)"
      [ -n "$url" ] || { echo "set REMOTE_URL=https://<your tunnel's hostname> in .env" >&2; exit 1; }
      TUNNEL_COMMAND="tunnel --no-autoupdate run" "${C[@]}" up -d tunnel
    else
      "${C[@]}" up -d --force-recreate tunnel
      url=""
      for _ in $(seq 1 45); do
        url="$("${C[@]}" logs tunnel 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
        [ -n "$url" ] && break; sleep 2
      done
      [ -n "$url" ] || { echo "the tunnel gave no address; see: tools/dev/remote.sh logs" >&2; exit 1; }
    fi
    put ERGPLAN_PUBLIC_URL "$url"
    "${C[@]}" up -d api worker web >/dev/null
    echo "Remote access is on: $url"
    echo "Sign-in links are sent for this address. To let someone in now:"
    echo "  tools/dev/mini.sh exec api python -m ergplan.platform.signin someone@company.in"
    ;;
  off)
    "${C[@]}" rm -sf tunnel >/dev/null 2>&1 || true
    lan="$(get LAN_URL)"; [ -n "$lan" ] && put ERGPLAN_PUBLIC_URL "$lan"
    "${C[@]}" up -d api worker web >/dev/null
    echo "Remote access is off. Local address: $(get ERGPLAN_PUBLIC_URL)"
    ;;
  url) get ERGPLAN_PUBLIC_URL ;;
  logs) "${C[@]}" logs --tail 40 tunnel ;;
  *) sed -n 2,8p "$0"; exit 1 ;;
esac
