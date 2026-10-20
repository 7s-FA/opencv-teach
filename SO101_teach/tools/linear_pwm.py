"""RP1 hardware PWM for GPIO18 (PWM0 channel 2), 50 Hz L12-R pulses.

Kernel sysfs generates the pulses independently of Python/AI scheduling.
The service's privileged setup runs once before the unprivileged controller.
"""
import os
from pathlib import Path
import pwd
import sys
import time

PERIOD_NS = 20_000_000
CHANNEL = 2


def channel_path(root=Path('/sys/class/pwm')):
    chips=[]
    for chip in root.glob('pwmchip*'):
        compatible=chip/'device/of_node/compatible'
        # RP1 has TWO PWM controllers. PWM1 (@9c000) drives the cooling fan;
        # only PWM0 (@98000) can reach header GPIO18.
        if ((chip/'device/of_node').resolve().name=='pwm@98000' and compatible.exists()
                and b'raspberrypi,rp1-pwm' in compatible.read_bytes().split(b'\0')):
            chips.append(chip)
    if len(chips)!=1:raise RuntimeError('GPIO18 RP1 PWM 컨트롤러를 하나로 확인할 수 없습니다.')
    if int((chips[0]/'npwm').read_text())<=CHANNEL:raise RuntimeError('RP1 PWM 채널 2가 없습니다.')
    return chips[0]/f'pwm{CHANNEL}'


def enable_pwm0():
    """Enable the dormant PWM0 node through configfs, without reboot or pulses."""
    base=Path('/sys/firmware/devicetree/base')
    target=(base/'__symbols__/pwm0').read_text().rstrip('\0')
    node=base/target.lstrip('/')
    if node.name!='pwm@98000' or b'raspberrypi,rp1-pwm' not in (node/'compatible').read_bytes().split(b'\0'):
        raise RuntimeError('GPIO18 PWM0 device-tree 대상이 다릅니다.')
    if (node/'status').read_text().rstrip('\0')!='okay':
        overlay=Path('/sys/kernel/config/device-tree/overlays/so101-linear-pwm0')
        if overlay.exists():raise RuntimeError('기존 PWM0 overlay 적용 상태를 확인하세요.')
        overlay.mkdir()
        try:
            (overlay/'dtbo').write_bytes(Path('/usr/local/lib/so101-linear/pwm0.dtbo').read_bytes())
            if (overlay/'status').read_text().strip()!='applied':raise RuntimeError('PWM0 overlay 적용 실패')
        except Exception:
            overlay.rmdir()
            raise
    until=time.monotonic()+2
    while True:
        try:return channel_path()
        except RuntimeError:
            if time.monotonic()>until:raise
            time.sleep(.01)


def initialize_channel(path):
    # An unconfigured sysfs channel has period=0. The kernel rejects even a
    # duty=0/enable=0 write until a valid period has been supplied first.
    if int((path/'period').read_text())==0:
        (path/'period').write_text(str(PERIOD_NS))
    (path/'duty_cycle').write_text('0')
    (path/'enable').write_text('0')
    (path/'period').write_text(str(PERIOD_NS))
    (path/'polarity').write_text('normal')
    (path/'enable').write_text('1')


def setup():
    """Only run with the linear service stopped; never emit a position pulse."""
    path=enable_pwm0()
    pinctrl=list(Path('/sys/kernel/debug/pinctrl').glob('*-pinctrl-rp1'))
    if len(pinctrl)!=1:raise RuntimeError('RP1 pinctrl 경로를 확인할 수 없습니다.')
    if not path.exists():
        (path.parent/'export').write_text(str(CHANNEL))
        until=time.monotonic()+2
        while not path.exists():
            if time.monotonic()>until:raise TimeoutError('RP1 PWM export 시간 초과')
            time.sleep(.01)
    # A zero duty cycle with enabled PWM holds the inactive level, unlike the
    # unspecified output level of a disabled PWM. Do this before pin muxing.
    initialize_channel(path)
    (pinctrl[0]/'pinmux-select').write_text('gpio18 pwm0')
    pins=(pinctrl[0]/'pins').read_text().splitlines()
    if not any(line.startswith('pin 18 (gpio18)') and '(pwm0)' in line for line in pins):
        raise RuntimeError('GPIO18 하드웨어 PWM 기능 설정 실패')
    # The controller may change only the pulse width; period/pinmux stay root-owned.
    account=pwd.getpwnam('ubuntu')
    os.chown(path/'duty_cycle',account.pw_uid,account.pw_gid)
    os.chmod(path/'duty_cycle',0o600)
    print('GPIO18: RP1 PWM0 channel 2, 50 Hz, duty=0 (no motion)')


class HardwarePWM:
    def __init__(self,root=Path('/sys/class/pwm')):
        self.path=channel_path(root)
        if (int((self.path/'period').read_text())!=PERIOD_NS
                or (self.path/'polarity').read_text().strip()!='normal'
                or (self.path/'enable').read_text().strip()!='1'
                or int((self.path/'duty_cycle').read_text())!=0):
            raise RuntimeError('리니어 하드웨어 PWM 초기 상태가 잘못되었습니다.')
        self.active=False
    def write(self,pulse_us):
        if type(pulse_us) is not int or pulse_us!=0 and not 1000<=pulse_us<=2000:
            raise ValueError('리니어 PWM 범위 오류')
        # No per-frame reconfiguration or sleep: hardware repeats the same pulse.
        (self.path/'duty_cycle').write_text(str(pulse_us*1000))
        self.active=bool(pulse_us)
        return 0
    def close(self):
        if self.active:self.write(0)


if __name__=='__main__':
    if sys.argv[1:]==['setup']:setup()
    elif sys.argv[1:]==['stop']:
        path=channel_path()
        if path.exists() and int((path/'duty_cycle').read_text())!=0:
            (path/'duty_cycle').write_text('0')
    else:raise SystemExit('usage: linear_pwm.py setup|stop')
