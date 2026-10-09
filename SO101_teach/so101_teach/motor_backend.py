"""Installed LeRobot bus, or the same motor-only code bundled for Pi."""
from functools import lru_cache
from importlib.util import find_spec

@lru_cache(maxsize=1)
def motor_backend():
    if find_spec('lerobot'):
        from lerobot.motors.feetech import FeetechMotorsBus
        from lerobot.motors import Motor,MotorCalibration,MotorNormMode
    else:
        from .vendor.lerobot.motors.feetech import FeetechMotorsBus
        from .vendor.lerobot.motors import Motor,MotorCalibration,MotorNormMode
    return FeetechMotorsBus,Motor,MotorCalibration,MotorNormMode
