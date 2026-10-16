#!/usr/bin/env bash
set -Eeuo pipefail
SO101_TEST_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SO101_TEST_NEW_LOGS=()
for SO101_TEST_LOG in "$SO101_TEST_ROOT/MUJOCO_LOG.TXT" "$SO101_TEST_ROOT/../MUJOCO_LOG.TXT"; do
  [[ -e "$SO101_TEST_LOG" ]] || SO101_TEST_NEW_LOGS+=("$SO101_TEST_LOG")
done
cleanup() {
  local status=$?
  for log in "${SO101_TEST_NEW_LOGS[@]}"; do
    if [[ -f "$log" && ! -L "$log" ]]; then
      if [[ "$status" -ne 0 ]]; then tail -40 "$log" >&2; fi
      rm -f -- "$log"
    fi
  done
  exit "$status"
}
trap cleanup EXIT
cd "$SO101_TEST_ROOT"
unset PYTHONPATH PYTHONHOME
# Render offscreen without hidden GLFW windows; allow an explicit backend override.
export MUJOCO_GL="${MUJOCO_GL:-egl}"
"${SO101_TEACH_PYTHON:-python3}" tools/pi_runtime.py local
if [[ $# -eq 0 ]]; then
  "${SO101_TEACH_PYTHON:-python3}" tools/test_suite.py
else
  "${SO101_TEACH_PYTHON:-python3}" tools/test_runner.py "$@" -v
fi
