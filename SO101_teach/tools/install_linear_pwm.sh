#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'sudo로 실행해 주세요.' >&2; exit 1; }
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT=/home/ubuntu/OpenCV_teach
SERVICE=arm3-linear-controller.service
exec 9<>"$ROOT/data/episode-cli.lock"
flock -n 9 || { echo '진행 중인 조립대 작업을 마친 뒤 갱신하세요.' >&2; exit 1; }
/usr/bin/python3 - <<'PY'
from pathlib import Path
import json,time
folder=Path('/home/ubuntu/.local/state/arm3-linear-controller')
for name in ('motion.json','last-command.json'):
    p=folder/name
    if not p.exists():continue
    value=json.loads(p.read_text())
    if value.get('phase') in ('MOVING','STARTING','PAUSED') or time.time()-value.get('recorded_at',0)<10:
        raise SystemExit('리니어 이동이 끝난 뒤 갱신하세요.')
PY
BACKUP="/home/ubuntu/OpenCV_teach/verification/linear-hardware-pwm-$(date +%Y%m%d-%H%M%S)"
install -d "$BACKUP" /usr/local/lib/so101-linear /etc/systemd/system/"$SERVICE".d
for name in linear_controller_pi5.py linear_motion.py linear_pwm.py; do
  [[ ! -e /home/ubuntu/project/"$name" ]] || cp -a /home/ubuntu/project/"$name" "$BACKUP/"
done
[[ ! -e /etc/systemd/system/"$SERVICE".d/hardware-pwm.conf ]] || cp -a /etc/systemd/system/"$SERVICE".d/hardware-pwm.conf "$BACKUP/"
systemctl stop "$SERVICE"
for name in linear_controller_pi5.py linear_motion.py linear_pwm.py; do
  install -o ubuntu -g ubuntu -m 644 "$SOURCE/$name" /home/ubuntu/project/"$name"
done
# The only root-executed source stays root-owned.
install -o root -g root -m 644 "$SOURCE/linear_pwm.py" /usr/local/lib/so101-linear/linear_pwm.py
/usr/bin/dtc -@ -I dts -O dtb "$SOURCE/linear_pwm0.dts" -o /usr/local/lib/so101-linear/pwm0.dtbo
cat > /etc/systemd/system/"$SERVICE".d/hardware-pwm.conf <<'EOF'
[Service]
ExecStartPre=+/usr/bin/python3 /usr/local/lib/so101-linear/linear_pwm.py setup
ExecStopPost=+/usr/bin/python3 /usr/local/lib/so101-linear/linear_pwm.py stop
EOF
systemctl daemon-reload
systemctl start "$SERVICE"
systemctl is-active --quiet "$SERVICE"
echo "하드웨어 PWM 적용 완료. 백업: $BACKUP"
