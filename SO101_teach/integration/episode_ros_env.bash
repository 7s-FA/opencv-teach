# Source defines commands only; no device or job starts.
SO101_EPISODE_SCRIPT="${SO101_EPISODE_SCRIPT:-/home/ubuntu/OpenCV_teach/run_episode.sh}"
build_a() { "$SO101_EPISODE_SCRIPT" build_a "$@"; }
build_b() { "$SO101_EPISODE_SCRIPT" build_b "$@"; }
load_a() { "$SO101_EPISODE_SCRIPT" load_a "$@"; }
load_b() { "$SO101_EPISODE_SCRIPT" load_b "$@"; }
build_load_a() { "$SO101_EPISODE_SCRIPT" build_load_a "$@"; }
build_load_b() { "$SO101_EPISODE_SCRIPT" build_load_b "$@"; }
slide_build() { "$SO101_EPISODE_SCRIPT" slide_build "$@"; }
slide_load() { "$SO101_EPISODE_SCRIPT" slide_load "$@"; }
slide_status() { "$SO101_EPISODE_SCRIPT" slide_status; }
build_status() { "$SO101_EPISODE_SCRIPT" build_status; }
load_status() { "$SO101_EPISODE_SCRIPT" load_status; }
estop() { "$SO101_EPISODE_SCRIPT" estop; }
restart() { "$SO101_EPISODE_SCRIPT" restart; }
# Explicit reset now refers to the active assembly job, not terminal reset.
reset() { "$SO101_EPISODE_SCRIPT" reset; }
# Previous task names remain aliases; -- flags are unnecessary.
arm2_a() { build_a "$@"; }
arm2_b() { build_b "$@"; }
arm3_a() { load_a "$@"; }
arm3_b() { load_b "$@"; }
arm2_status() { build_status; }
arm3_status() { load_status; }
linear_f() { slide_build "$@"; }
linear_r() { slide_load "$@"; }
linear() { echo 'Use slide_build (100mm) or slide_load (1.5mm).' >&2; return 2; }
unset -f arm2_stop arm3_stop build_stop load_stop 2>/dev/null || true
# Read-only job state, detailed episode events, and Action service logs.
work_status() { python3 -B "$(dirname -- "$SO101_EPISODE_SCRIPT")/integration/work_monitor.py" --once "$@"; }
work_watch() { python3 -B "$(dirname -- "$SO101_EPISODE_SCRIPT")/integration/work_monitor.py" "$@"; }
work_logs() { journalctl -q -u workcell-actions.service -n 30 -f --no-pager; }
