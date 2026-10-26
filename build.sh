#!/usr/bin/env bash
# Installs dependencies, runs the end-to-end test, and builds the package.
#   ./build.sh            test locally (no tunnel) and build dist/
#   ./build.sh --tunnel   also test over a real public HTTPS tunnel, like ChatGPT uses
#   ./build.sh --install  install the porthole command from this folder
set -euo pipefail
cd "$(dirname "$0")"
uv sync -q
if [[ "${1:-}" == "--tunnel" ]]; then uv run python tests/e2e.py; else uv run python tests/e2e.py --local; fi
uv build -q && echo "Built $(ls dist/*.whl | tail -1)"
[[ "${1:-}" == "--install" ]] && uv tool install --force --quiet . && echo "Installed: $(command -v porthole)" || true
