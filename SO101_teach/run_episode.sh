#!/usr/bin/env bash
set -Eeuo pipefail
SO101_EPISODE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SO101_EPISODE_ROOT"
# ROS is only a status output. There is no external command receiver.
if [[ -f /opt/ros/jazzy/setup.bash ]]; then
  set +u
  source /opt/ros/jazzy/setup.bash
  set -u
fi
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-40}"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec "${SO101_EPISODE_PYTHON:-/home/ubuntu/so101-player/.venv/bin/python}" -B integration/episode_cli.py "$@"
