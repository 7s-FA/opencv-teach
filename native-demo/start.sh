#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
SO101_DEMO_RUNTIME="${XDG_CACHE_HOME:-$HOME/.cache}/so101-native-demo"
mkdir -p "$SO101_DEMO_RUNTIME"
if [[ "${1:-}" == --background ]]; then
  if curl --silent --fail http://127.0.0.1:6080/health >/dev/null; then
    echo '원본 앱 실행 중: http://127.0.0.1:6080'
    exit 0
  fi
  nohup .native-venv/bin/python native-demo/server.py > "$SO101_DEMO_RUNTIME/server.log" 2>&1 < /dev/null &
  echo "http://127.0.0.1:6080 — 원본 앱을 준비하고 있습니다."
else
  exec .native-venv/bin/python native-demo/server.py
fi
