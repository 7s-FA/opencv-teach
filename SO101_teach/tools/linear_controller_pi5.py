#!/usr/bin/env python3
"""Simple ROS 2 position command node for an Actuonix L12-R on Raspberry Pi 5."""

from linear_pwm import HardwarePWM
import rclpy
import time
import json
import os
import tempfile
import socket
import threading
import fcntl
import signal
from pathlib import Path
from rclpy.node import Node
from std_msgs.msg import Float32, Int32


PWM_GPIO = 18
PWM_FREQUENCY_HZ = 50
AUTO_STOP_SECONDS = 10.0

# Actuonix L12-100-R: 100 mm stroke, 1000 us = retract, 2000 us = extend.
STROKE_MM = 100.0
MIN_PULSE_US = 1000
MAX_PULSE_US = 2000
LAST_COMMAND_FILE = Path.home() / ".local/state/arm3-linear-controller/last-command.json"


def load_last_command(path):
    """An accepted command is history, never measured position or arrival."""
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("Invalid linear command record")
    if value.get("status") == "pending":
        return None
    pulse = value.get("pulse_us")
    if (value.get("status") != "accepted" or type(pulse) is not int
            or not MIN_PULSE_US <= pulse <= MAX_PULSE_US
            or value.get("position_measured") is not False):
        raise ValueError("Invalid linear command record")
    return pulse


def store_command(path, pulse, status="accepted"):
    """Replace the durable record atomically; incomplete output is not restored."""
    value = {"schema": 1, "status": status, "pulse_us": pulse,
             "commanded_mm": (pulse - MIN_PULSE_US) / 10.0,
             "target_position": "ASSEMBLY" if pulse == 2000 else "UNLOAD" if pulse == 1015 else "CUSTOM",
             "target_label": "조립 위치" if pulse == 2000 else "하차 위치" if pulse == 1015 else f"{(pulse - MIN_PULSE_US) / 10:g}mm",
             "recorded_at": time.time(), "position_measured": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".linear-command-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class LinearController(Node):
    def __init__(self, *, control=False):
        super().__init__("linear_controller", namespace="/arm3")
        self._pwm = HardwarePWM()
        self._closed = False
        self._pwm_active = False
        self._position = -1
        self._recorded = False
        try:
            saved = load_last_command(LAST_COMMAND_FILE)
            if saved is not None:
                self._position = saved
                self._recorded = True
                self.get_logger().info(f"Restored command {saved} us; no PWM output")
        except (OSError, ValueError) as exc:
            self.get_logger().warning(f"Linear command record unavailable: {exc}")
        self._stop_at = None
        self.motion = None
        self.control_stop = threading.Event()
        self.control_lock = threading.RLock()
        if control:
            self.start_control()

        self.create_subscription(Float32, "linear_cmd", self._command, 10)
        self._state_pub = self.create_publisher(Int32, "linear_state", 10)
        self.create_timer(0.1, self._publish_state)

        self.get_logger().info(
            f"Ready: /arm3/linear_cmd, GPIO{PWM_GPIO} hardware PWM, "
            f"{MIN_PULSE_US}-{MAX_PULSE_US} us"
        )

    def _command(self, message: Float32) -> None:
        if self.motion:
            try:
                target = round(float(message.data) * 10) / 10
                with self.control_lock:
                    self.motion.call({'op': 'ensure', 'target_mm': target})
            except Exception as exc:
                self.get_logger().warning(str(exc))
            return
        position_mm = float(message.data)
        if not 0.0 <= position_mm <= STROKE_MM:
            self.get_logger().warning(
                f"Position must be 0..{STROKE_MM:.0f} mm, got {position_mm}"
            )
            return

        pulse_us = round(MIN_PULSE_US + position_mm * 10.0)
        if pulse_us == self._position:
            # Do not restart output or extend the active stop deadline.
            if not self._recorded:
                self._recorded = self._save_accepted()
            self.get_logger().info(f"Duplicate target {position_mm:.1f} mm ignored; no PWM output")
            return

        # Fail before touching PWM if command history cannot be written. A crash
        # between output and acceptance leaves an explicitly incomplete record.
        try:
            store_command(LAST_COMMAND_FILE, pulse_us, "pending")
        except OSError as exc:
            self.get_logger().error(f"Command not sent: cannot save linear history: {exc}")
            return
        try:
            result = self._pwm.write(pulse_us)
            if result < 0:
                raise RuntimeError(f"PWM error {result}")
        except Exception as exc:
            if self._position != -1:
                self._recorded = self._save_accepted()
            self.get_logger().error(f"PWM output failed: {exc}")
            return

        self._pwm_active = True
        # The state topic reports the accepted command, not position feedback.
        self._position = pulse_us
        self._stop_at = time.monotonic() + AUTO_STOP_SECONDS
        self._recorded = self._save_accepted()
        self.get_logger().info(f"Target {position_mm:.1f} mm -> {pulse_us} us")

    def _save_accepted(self):
        try:
            store_command(LAST_COMMAND_FILE, self._position)
            return True
        except OSError as exc:
            self.get_logger().error(f"Linear command accepted but history not committed: {exc}")
            return False

    def _publish_state(self) -> None:
        if self.motion:
            with self.control_lock:
                try:
                    self.motion.tick()
                except Exception as exc:
                    self.get_logger().error(str(exc))
                from std_msgs.msg import String
                self.detail_pub.publish(String(data=json.dumps(self.motion.status(), ensure_ascii=False)))
        if self._stop_at is not None and time.monotonic() >= self._stop_at:
            result = self._pwm.write(0)
            self._stop_at = None
            if result < 0:
                self.get_logger().error(f"PWM stop failed (PWM error {result})")
            else:
                self._pwm_active = False
                self.get_logger().info(
                    f"PWM signal stopped after {AUTO_STOP_SECONDS:.0f} seconds; "
                    "actuator power is still on"
                )

        message = Int32()
        message.data = (-1 if self.motion and self.motion.phase != 'TIMED_COMPLETE' else self._position)
        self._state_pub.publish(message)

    def start_control(self):
        from linear_motion import LinearMotion
        from std_msgs.msg import String
        folder = LAST_COMMAND_FILE.parent
        folder.mkdir(parents=True, exist_ok=True)
        state_path = folder / 'motion.json'
        def save(value):
            fd, name = tempfile.mkstemp(prefix='.motion-', dir=folder)
            try:
                with os.fdopen(fd, 'w') as file:
                    json.dump(value, file, allow_nan=False); file.flush(); os.fsync(file.fileno())
                os.replace(name, state_path)
            finally:
                if os.path.exists(name): os.unlink(name)
        def output(pulse):
            if pulse == 0 and not self._pwm_active:
                return
            result = self._pwm.write(pulse)
            if result < 0: raise RuntimeError(f'PWM output failed: {result}')
            self._pwm_active = bool(pulse)
            if pulse:
                self._position = pulse
                self._recorded = self._save_accepted()
        try:
            restored = json.loads(state_path.read_text()) if state_path.exists() else None
        except (OSError, ValueError):
            restored = None
        self.motion = LinearMotion(output, save, restored, clock=lambda: time.monotonic())
        self.detail_pub = self.create_publisher(String, 'linear_status', 10)
        self.control_socket = folder / 'control.sock'
        self.control_thread = threading.Thread(target=self.serve_control, daemon=True)
        self.control_thread.start()

    def serve_control(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            self.control_socket.unlink(missing_ok=True)
            server.bind(str(self.control_socket)); os.chmod(self.control_socket, 0o600)
            server.listen(8); server.settimeout(.2)
            while not self.control_stop.is_set():
                try: connection, _ = server.accept()
                except socket.timeout: continue
                with connection:
                    connection.settimeout(1)
                    try:
                        raw = b''
                        while b'\n' not in raw and len(raw) < 4096:
                            part = connection.recv(4096)
                            if not part: break
                            raw += part
                        request = json.loads(raw)
                        with self.control_lock:
                            value = self.motion.call(request)
                        response = {'ok': True, 'value': value}
                    except Exception as exc:
                        response = {'ok': False, 'error': str(exc)}
                    try: connection.sendall((json.dumps(response, allow_nan=False) + '\n').encode())
                    except OSError: pass
        self.control_socket.unlink(missing_ok=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.control_stop.set()
        if hasattr(self, "control_thread"): self.control_thread.join(2)
        try:
            if self._pwm_active:
                self._pwm.write(0)
                self._pwm_active = False
        finally:
            self._pwm.close()


def main(args=None):
    import argparse,sys
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network-interface',default=os.environ.get('WORKCELL_NETWORK_INTERFACE','wlan0'))
    parser.add_argument('--network-timeout',type=float,default=60.)
    parser.add_argument('--network-check-only',action='store_true')
    options,ros_args=parser.parse_known_args(args)
    # DDS fixes its interface locators at participant creation. network-online
    # alone does not guarantee that Wi-Fi has acquired its address and route.
    local=Path(__file__).resolve().parents[1]/'integration'
    network_dir=Path(os.environ.get('SO101_APP_DIR','/home/ubuntu/OpenCV_teach'))/'integration'
    if local.is_dir():network_dir=local
    sys.path.insert(0,str(network_dir))
    from network_ready import wait_for_network
    wait_for_network(options.network_interface,options.network_timeout)
    if options.network_check_only:return
    rclpy.init(args=ros_args)
    signal.signal(signal.SIGTERM, lambda *_: rclpy.try_shutdown())
    node = None
    try:
        lock_path = LAST_COMMAND_FILE.parent / 'controller.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        node = LinearController(control=True)
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        if node is not None:
            node.close()
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
