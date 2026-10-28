#!/usr/bin/env bash
set -Eeuo pipefail
SO101_MONITOR_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SO101_MONITOR_ROOT/run.sh" --work-monitor "$@"
