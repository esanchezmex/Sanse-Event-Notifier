#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${REPO_ROOT}/logs"

mkdir -p "${LOG_DIR}"
cd "${REPO_ROOT}"

exec /usr/bin/env python3 "scraper/diariodesanse_scraper.py" "$@"
