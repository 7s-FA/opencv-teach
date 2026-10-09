#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=/home/ubuntu/OpenCV_teach
[[ $EUID -eq 0 ]] || { echo 'sudo로 실행해 주세요.' >&2; exit 1; }
[[ -e "$ROOT/data/episode-cli.lock" ]] || install -o ubuntu -g ubuntu -m 600 /dev/null "$ROOT/data/episode-cli.lock"
exec 9<>"$ROOT/data/episode-cli.lock"
flock -n 9 || { echo '현재 조립대 작업을 마친 뒤 서비스를 갱신하세요.' >&2; exit 1; }
/usr/bin/python3 - <<'PY'
from pathlib import Path
import json,time
folder=Path('/home/ubuntu/.local/state/arm3-linear-controller')
p=folder/'last-command.json'
if p.exists() and time.time()-json.loads(p.read_text()).get('recorded_at',0)<10:
    raise SystemExit('최근 리니어 이동 명령이 있습니다. 정지한 뒤 다시 실행하세요.')
p=folder/'motion.json'
if p.exists() and json.loads(p.read_text()).get('phase') in ('MOVING','PAUSED','STARTING'):
    raise SystemExit('리니어 이동 또는 비상정지 작업을 마친 뒤 갱신하세요.')
PY
install -m 644 "$ROOT/integration/workcell-actions.service" /etc/systemd/system/workcell-actions.service
systemctl daemon-reload
systemctl restart arm3-linear-controller.service
systemctl enable workcell-actions.service
systemctl restart workcell-actions.service
systemctl is-active arm3-linear-controller.service workcell-actions.service
