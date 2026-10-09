#!/usr/bin/env bash
set -Eeuo pipefail
SO101_APP_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SO101_PYTHON="${SO101_TEACH_PYTHON:-python3}"
cd "$SO101_APP_ROOT"
unset PYTHONPATH PYTHONHOME
exec "$SO101_PYTHON" -m so101_teach "$@"
