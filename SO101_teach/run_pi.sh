#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec /home/ubuntu/so101-player/.venv/bin/python -B -m so101_teach.remote_server --serve
