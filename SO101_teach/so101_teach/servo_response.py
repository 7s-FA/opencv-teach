"""Explicit, torque-OFF maintenance for shoulder/elbow P gains; never runs at connect.

20 is an experimental increment from LeRobot's 16, not an official SO-101 preset.
No position, torque, homing, range, I/D or protection writes are admitted.
"""
import argparse
from collections import Counter
import time
from .devices import new_bus, ensure_available, ReadOnlyViolation
from .domain import JOINTS, ROOT, atomic_json, load_profile

TARGETS = ('shoulder_lift', 'elbow_flex')
REGISTERS = ('Torque_Enable', 'Operating_Mode', 'P_Coefficient', 'I_Coefficient',
             'D_Coefficient', 'CW_Dead_Zone', 'CCW_Dead_Zone', 'Minimum_Startup_Force',
             'Max_Torque_Limit', 'Torque_Limit', 'Protection_Current', 'Overload_Torque',
             'Lock', 'Homing_Offset', 'Min_Position_Limit', 'Max_Position_Limit', 'Goal_Position')


class ResponseGate:
    def __init__(self, calibration):
        self.ids = {n: calibration.motors[n].id for n in TARGETS}
        self.permit = None

    def allow(self, name, register, value):
        if name not in self.ids or type(value) is not int:
            raise ReadOnlyViolation('어깨·팔꿈치 P 조정만 허용합니다.')
        if register == 'P_Coefficient' and value in (16, 20):
            address = 21
        elif register == 'Lock' and value in (0, 1):
            address = 55
        else:
            raise ReadOnlyViolation('P 또는 EEPROM 잠금 외 쓰기는 허용하지 않습니다.')
        self.permit = (self.ids[name], address, value)

    def install(self, port, counts):
        original = port.writePort

        def guarded(packet):
            if (len(packet) < 6 or bytes(packet[:2]) != b'\xff\xff'
                    or len(packet) != packet[3] + 4 or sum(packet[2:]) % 256 != 255):
                self.permit = None
                raise ReadOnlyViolation('서보 설정 패킷 오류')
            op = packet[4]
            if op not in (1, 2, 0x82):
                permitted, self.permit = self.permit, None
                if op != 3 or len(packet) != 8 or permitted != tuple(packet[i] for i in (2, 5, 6)):
                    raise ReadOnlyViolation('승인되지 않은 서보 설정 쓰기')
            counts[op] += 1
            return original(packet)

        port.writePort = guarded


def read_settings(bus):
    return {n: {r: bus.read(r, n, normalize=False) for r in REGISTERS} for n in JOINTS}


def apply_response(bus, calibration, gate, p, report, save):
    if type(p) is not int or p not in (16, 20):
        raise ValueError('이번 비교는 P=16 또는 20만 사용합니다.')
    before = read_settings(bus)
    report.update(before=before, requested_p=p, state='checked')
    for n, row in before.items():
        m = calibration.motors[n]
        if row['Torque_Enable'] != 0 or row['Operating_Mode'] != 0:
            raise RuntimeError(n + ': 토크 OFF·위치 모드에서만 변경할 수 있습니다.')
        if (row['Homing_Offset'], row['Min_Position_Limit'], row['Max_Position_Limit']) != (m.homing, m.low, m.high):
            raise RuntimeError(n + ': 현재 보정 JSON과 모터 설정이 다릅니다.')
    for n in TARGETS:
        if before[n]['P_Coefficient'] not in (16, 20) or before[n]['Lock'] != 1:
            raise RuntimeError(n + ': 기존 P 또는 EEPROM 잠금 상태를 확인하세요.')
        if (before[n]['I_Coefficient'], before[n]['D_Coefficient']) != (0, 32):
            raise RuntimeError(n + ': 비교 기준 I=0, D=32와 다릅니다.')
    save(report)  # Persist originals BEFORE the first write.

    def write(n, register, value):
        if any(bus.read('Torque_Enable', joint, normalize=False) != 0 for joint in JOINTS):
            raise RuntimeError('설정 중 토크 ON 감지')
        gate.allow(n, register, value)
        try:
            bus.write(register, n, value, normalize=False, num_retry=0)
        finally:
            gate.permit = None
        if bus.read(register, n, normalize=False) != value:
            raise RuntimeError(n + ': ' + register + ' 저장 확인 실패')

    touched = []
    try:
        for n in TARGETS:
            if before[n]['P_Coefficient'] == p:
                continue
            touched.append(n)  # Include ambiguous/failed unlock ACKs in cleanup.
            write(n, 'Lock', 0)
            write(n, 'P_Coefficient', p)
            write(n, 'Lock', 1)
        after = read_settings(bus)
        expected = {n: dict(row) for n, row in before.items()}
        for n in TARGETS:
            expected[n]['P_Coefficient'] = p
        if after != expected:
            raise RuntimeError('P 이외 설정 변경 또는 저장 불일치')
        report.update(after=after, state='applied', motion_tested=False)
        save(report)
    except BaseException as exc:
        errors = []
        for n in reversed(touched):
            try:
                write(n, 'Lock', 0)
                write(n, 'P_Coefficient', before[n]['P_Coefficient'])
            except Exception as failure:
                errors.append(n + ': ' + str(failure))
            finally:
                try:
                    write(n, 'Lock', 1)
                except Exception as failure:
                    errors.append(n + ' 잠금: ' + str(failure))
        try:
            report['after'] = read_settings(bus)
            if report['after'] != before:
                errors.append('이전 설정 완전 복원 미확인')
        except Exception as failure:
            errors.append(str(failure))
        report.update(state='rollback_failed' if errors else 'rolled_back', error=str(exc), rollback_errors=errors)
        save(report)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description='SO-101 팔로워 어깨·팔꿈치 P 유지 응답 조정 (이동 없음)')
    parser.add_argument('--p', type=int, choices=(16, 20), required=True)
    args = parser.parse_args()
    profile, calibration, _ = load_profile()
    ensure_available(profile['port'])
    counts = Counter()
    gate = ResponseGate(calibration)
    bus = new_bus(profile['port'], calibration, counts, gate_installer=gate.install)
    path = ROOT / 'data/diagnostics' / f'servo-response-{time.time_ns()}.json'
    report = {'port': profile['port'], 'calibration_sha256': calibration.sha256, 'started_at': time.time()}
    try:
        bus.connect()
        apply_response(bus, calibration, gate, args.p, report, lambda r: atomic_json(path, r))
        print(f'어깨·팔꿈치 P={args.p}, I=0, D=32 저장 확인. 토크 OFF. 기록: {path}')
    finally:
        bus.port_handler.closePort()  # SDK disconnect may issue torque writes.
        report.update(finished_at=time.time(), writes=counts.get(3, 0))
        atomic_json(path, report)


if __name__ == '__main__':
    main()
