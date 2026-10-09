"""Preset-only linear motion timing. No position feedback is claimed."""
import time
import uuid

PRESETS = {'build': 100.0, 'load': 1.5}


class LinearMotion:
    def __init__(self, output, save, restored=None, clock=time.monotonic):
        self.output, self.save, self.clock = output, save, clock
        self.instance = uuid.uuid4().hex
        self.target = None
        self.phase = 'UNKNOWN'
        self.deadline = None
        self.owner = None
        self.owner_at = 0.
        self.error = None
        if restored and restored.get('target_mm') in PRESETS.values():
            self.target = restored['target_mm']
            if restored.get('phase') == 'TIMED_COMPLETE':
                self.phase = restored['phase']

    def status(self):
        position = ('FORWARD' if self.target == 100. else 'RETRACTED') if self.phase == 'TIMED_COMPLETE' else 'INTERMEDIATE' if (self.phase in ('MOVING', 'PAUSED', 'STARTING') or self.phase == 'UNKNOWN' and self.target is not None) else 'UNKNOWN'
        return {'instance': self.instance, 'phase': self.phase, 'position_state': position, 'target_mm': self.target,
                'position_measured': False, 'pause_supported': False, 'error': self.error, 'owned': self.owner is not None}

    def persist(self):
        self.save(self.status())

    def claim(self, owner):
        self.tick()
        if not isinstance(owner, str) or not owner or len(owner) > 80:
            raise ValueError('invalid owner')
        if self.phase == 'MOVING' and self.owner != owner:
            raise ValueError('LINEAR_BUSY: 진행 중인 목표 완료를 기다리세요.')
        if self.owner and self.owner != owner:
            raise ValueError('LINEAR_BUSY')
        self.owner, self.owner_at = owner, self.clock()
        return self.status()

    def verify(self, owner):
        if self.owner != owner:
            raise ValueError('LINEAR_BUSY: 사용권을 다시 확인하세요.')
        if self.owner:
            self.owner_at = self.clock()

    def start(self, target, seconds):
        if target not in PRESETS.values():
            raise ValueError('전진 100mm 또는 후진 1.5mm만 지원합니다.')
        # Persist intent before output. A crash during a move restores UNKNOWN.
        self.target, self.phase, self.error = target, 'STARTING', None
        self.persist()
        try:
            self.output(round(1000 + target * 10))
        except Exception as exc:
            self.phase, self.error = 'ERROR', str(exc)
            self.persist()
            raise
        self.phase, self.deadline = 'MOVING', self.clock() + seconds
        self.persist()
        return self.status()

    def ensure(self, target):
        self.tick()
        if self.phase == 'ERROR':
            raise ValueError('LINEAR_ERROR: ' + str(self.error))
        if target == self.target and self.phase in ('TIMED_COMPLETE', 'MOVING'):
            return self.status()
        if self.phase == 'MOVING':
            raise ValueError('LINEAR_BUSY: 진행 중인 목표를 변경할 수 없습니다.')
        return self.start(target, 8.)

    def pause(self):
        raise ValueError('LINEAR_PAUSE_UNSUPPORTED: GPIO 신호로 실제 정지를 확인할 수 없습니다.')

    def resume(self):
        raise ValueError('LINEAR_PAUSE_UNSUPPORTED: 리니어는 기존 목표까지 계속 진행합니다.')

    def cancel(self):
        # No new PWM or timer changes: removing a pulse cannot cancel the physical move.
        self.tick()
        return self.status()

    def tick(self):
        if self.deadline is not None and self.clock() >= self.deadline:
            try:
                self.output(0)
            except Exception as exc:
                self.phase, self.error, self.deadline = 'ERROR', str(exc), None
                self.persist()
                raise
            self.phase, self.deadline = 'TIMED_COMPLETE', None
            self.persist()
        if self.owner and self.clock() - self.owner_at > 3:
            # Loss of the requester does not stop L12-R physically. Keep the original
            # timed move and reject a new owner's claim until it completes.
            self.owner = None

    def call(self, request):
        self.tick()
        op, owner = request.get('op'), request.get('owner')
        if op == 'status':
            if owner == self.owner: self.owner_at = self.clock()
            return {**self.status(), 'owner_matches': self.owner is not None and owner == self.owner}
        if op == 'claim': return self.claim(owner)
        self.verify(owner)
        if op == 'ensure': return self.ensure(request['target_mm'])
        if op == 'pause': return self.pause()
        if op == 'resume': return self.resume()
        if op == 'cancel': return self.cancel()
        if op == 'release':
            self.owner = None
            return self.status()
        raise ValueError('invalid linear command')
