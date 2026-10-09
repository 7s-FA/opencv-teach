#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends python3-venv python3-tk xvfb x11vnc xauth libgl1 libgl1-mesa-dri libglib2.0-0 fonts-noto-cjk
/usr/bin/python3 -m venv .native-venv
.native-venv/bin/python -m pip install -r SO101_teach/requirements-desktop.txt -r native-demo/requirements.txt
npm ci --prefix native-demo
