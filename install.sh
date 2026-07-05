#!/usr/bin/env bash
# Linux setup for web-graph-crawler: venv, package, and Chromium.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

PYTHON="${PYTHON:-python3}"

echo "==> Creating virtual environment (.venv)"
"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading pip"
python -m pip install --upgrade pip

echo "==> Installing web-graph-crawler and dependencies"
pip install -e .

echo "==> Installing Chromium for Playwright"
# --with-deps pulls the required system libraries (needs sudo on most distros).
if ! python -m playwright install --with-deps chromium; then
    echo "    system-deps step failed; installing the browser only."
    echo "    If pages fail to launch, run: sudo python -m playwright install-deps chromium"
    python -m playwright install chromium
fi

cat <<'EOF'

Done.

  source .venv/bin/activate          # activate the environment
  web-graph-crawler                  # interactive mode (prompts for dorks)
  web-graph-crawler --dork 'site:example.com inurl:blog'
  web-graph-crawler --help           # all options
EOF
