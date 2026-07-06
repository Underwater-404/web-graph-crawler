#!/usr/bin/env bash
# One-time setup on the VPS: render settings.yml with a fresh secret, then start
# SearXNG + Tor. Re-run any time to apply changes (settings.yml is kept).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f settings.yml ]; then
    secret="$(openssl rand -hex 32)"
    sed "s/__SEARXNG_SECRET__/${secret}/" settings.yml.template > settings.yml
    echo "==> Generated settings.yml with a fresh secret_key (gitignored)."
fi

# docker compose (v2) or docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi

$COMPOSE up -d
echo
echo "SearXNG is up on the VPS at 127.0.0.1:8080 (not public)."
echo
echo "From your workstation:"
echo "  ssh -fN -L 8080:127.0.0.1:8080 <vps-user>@<vps-host>"
echo "  export SEARXNG_URL=http://127.0.0.1:8080"
echo "  web-graph-crawler --search-provider searxng --dorks dorks.txt --out data/links.csv"
