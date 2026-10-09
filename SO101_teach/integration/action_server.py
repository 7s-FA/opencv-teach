# 로봇팔에 들어가는 Action Server 코드입니다.
# 원본과 동일하게 __init__ → goal_callback → execute_callback → main 순서입니다.
# Pi의 기존 실행부(run_episode.sh, integration/action_protocol.py)를 사용합니다.

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import rclpy
from rclpy.action import ActionServer, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor, ExternalShutdownException
from rclpy.node import Node
from host_pkg.action import Arm

ROOT = Path(os.environ.get('SO101_APP_DIR', '/home/ubuntu/OpenCV_teach'))
COMMANDS = {
    'arm2': {'BUILD_A', 'BUILD_B', 'BUILD_LOAD_A', 'BUILD_LOAD_B', 'SLIDE_BUILD'},
    'arm3': {'LOAD_A', 'LOAD_B', 'SLIDE_LOAD'},
}
CONTROLS = {
    'EMER_STOP': 'estop', 'RESTART': 'restart', 'RESET': 'reset',
}


class ArmActionServer(Node):
    def __init__(self, arm, job_lock, protocol, check_only=False):
        super().__init__('data', namespace=arm)
        self.arm = arm
        self.job_lock = job_lock
        self.protocol = protocol
        self.check_only = check_only
        self.process = None;self.dispatch_lock=None
        self._action_server = ActionServer(
            self,
            Arm,
            f'/{arm}/data',
            goal_callback=self.goal_callback,
            execute_callback=self.execute_callback,
            # 작업 중에도 비상정지 요청을 받을 수 있도록 합니다.
            callback_group=ReentrantCallbackGroup(),
        )
        self.get_logger().info(f'Host Arm Action Server is ready: /{arm}/data')
        self.notice('ACTION_SERVER_READY')

    def notice(self, message):
        try:self.protocol.record_notice(ROOT,self.arm,message)
        except Exception as exc:self.get_logger().error('알림 이력 저장 실패: '+str(exc))

    def goal_callback(self, goal_request):
        command = goal_request.command
        self.get_logger().info(f'Received goal request: CMD = {command}')
        self.notice('GOAL_RECEIVED:'+command)
        if command in CONTROLS:
            self.notice('CONTROL_ACCEPTED:'+command)
            return GoalResponse.ACCEPT
        if command not in COMMANDS[self.arm]:
            self.notice('GOAL_REJECTED:'+command+':지원하지 않는 명령 또는 다른 팔의 명령')
            return GoalResponse.REJECT
        # 두 팔의 일반 작업은 한 번에 하나만 실행합니다.
        if not self.job_lock.acquire(blocking=False):
            self.notice('GOAL_REJECTED:'+command+':BUSY 다른 작업이 실행 중입니다.')
            return GoalResponse.REJECT
        # Reserve the workcell at acceptance, before Python/vision startup.
        # The child inherits this same lock; idle torque release cannot win in
        # the gap between receiving a goal and opening the controller leases.
        reservation=None
        try:
            reservation=open(ROOT/'data/episode-cli.lock','a');fcntl.flock(reservation,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:
            if reservation:reservation.close()
            self.job_lock.release()
            self.notice('GOAL_REJECTED:'+command+(':BUSY 다른 작업이 실행 중입니다.' if isinstance(exc,BlockingIOError) else ':실행 잠금 확인 실패'))
            return GoalResponse.REJECT
        self.dispatch_lock=reservation
        self.notice('GOAL_ACCEPTED:'+command)
        return GoalResponse.ACCEPT

    def execute_callback(self, goal_handle):
        command = goal_handle.request.command
        feedback_msg = Arm.Feedback()
        result = Arm.Result()

        def publish_feedback(message):
            if feedback_msg.message != message:
                feedback_msg.message = message
                goal_handle.publish_feedback(feedback_msg)

        publish_feedback('RUNNING')
        is_control = command in CONTROLS
        process=None;lock_owned=not is_control;reservation=None
        try:
            if is_control:
                control = CONTROLS[command]
                result.success = self.invoke_control(control)
                state = {'estop': 'PAUSED', 'restart': 'RUNNING', 'reset': 'IDLE'}[control]
            else:
                reservation=self.dispatch_lock
                args = [str(ROOT / 'run_episode.sh'), command.lower(), '--managed-completion','--execution-lock-fd',str(reservation.fileno())]
                if self.check_only:
                    args += ['--check', '--no-ros']
                process = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,pass_fds=(reservation.fileno(),), text=True, bufsize=1, start_new_session=True,
                )
                reservation.close()
                if self.dispatch_lock is reservation:self.dispatch_lock=None
                self.process=process
                last = ''
                for line in process.stdout:
                    if line.strip()=='READY_TO_COMPLETE':
                        # CLI has already closed device resources and its file
                        # lock. Release the Action gate before it publishes DONE.
                        if lock_owned:self.job_lock.release();lock_owned=False
                        process.stdin.write('COMPLETE\n');process.stdin.flush()
                        continue
                    last = line.strip()
                    self.get_logger().info(f'{command}: {last}')
                    if last and not last.startswith(('A_','B_','SLIDE_','CHECK_OK ')):
                        self.notice('EXECUTOR_OUTPUT:'+command+':'+last)
                    state = self.protocol.feedback_word(last)
                    if command.startswith('BUILD_LOAD_') and last in ('A_DONE','B_DONE'):state=None
                    if state:
                        publish_feedback(state)
                result.success = self.protocol.job_succeeded(
                    process.wait(), last, self.check_only, chained=command.startswith('BUILD_LOAD_'),
                )
                state = 'IDLE'
            if is_control or lock_owned or not result.success:publish_feedback(state if result.success else 'ERROR')
        except Exception as exc:
            self.get_logger().error(f'{command}: {exc}')
            self.notice('ACTION_FAILED:'+command+':'+str(exc))
            result.success = False
            publish_feedback('ERROR')
        finally:
            if not is_control:
                if reservation is not None:reservation.close()
                if self.dispatch_lock is reservation:self.dispatch_lock=None
                self.stop_process(process)
                if lock_owned:self.job_lock.release()
                if self.process is process:self.process=None

        result.message = 'IDLE' if result.success else 'ERROR'
        self.notice(('CONTROL_RESULT:' if is_control else 'ACTION_RESULT:')+command+':'+result.message)
        if result.success:
            goal_handle.succeed()
        else:
            goal_handle.abort()
        return result

    # 아래 보조 처리는 실제 정지·재개·초기화 완료 확인과 종료 정리입니다.
    def invoke_control(self, command):
        if self.check_only:
            return True
        try:info = self.protocol.control_state(ROOT).get('info')
        except OSError:info=None
        if not info and command=='reset':
            result=subprocess.run([str(ROOT/'run_episode.sh'),'reset','--recovery-arm',self.arm,'--no-ros'],capture_output=True,text=True,timeout=300)
            detail=(result.stdout+result.stderr).strip();self.get_logger().info('reset: '+detail)
            self.notice('CONTROL_REPLY:reset:'+detail)
            return result.returncode==0 and bool(result.stdout.strip()) and result.stdout.strip().splitlines()[-1].endswith('_RESET_DONE')
        if not info and command != 'estop':
            self.notice('CONTROL_REJECTED:'+command+':NO_ACTIVE_JOB')
            return False
        after = len(self.protocol.job_events(ROOT, info)) if info else 0
        result = subprocess.run(
            [str(ROOT / 'run_episode.sh'), command, '--no-ros'],
            capture_output=True, text=True, timeout=10,
        )
        self.get_logger().info(f'{command}: {(result.stdout + result.stderr).strip()}')
        self.notice('CONTROL_REPLY:'+command+':'+(result.stdout+result.stderr).strip())
        if result.returncode:
            return False
        deadline = time.monotonic() + 20
        while not info and time.monotonic() < deadline:
            info = self.protocol.control_state(ROOT).get('info')
            if not info:
                time.sleep(.05)
        confirmed=bool(info) and self.protocol.wait_control(ROOT, command, info, after)
        if not confirmed:self.notice('CONTROL_FAILED:'+command+':실제 완료 확인 실패 또는 시간 초과')
        return confirmed

    def stop_process(self,process=None):
        process = process if process is not None else self.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass


def main(args=None):
    global ROOT
    parser = argparse.ArgumentParser(description='로봇팔 ROS 2 Action 서버')
    parser.add_argument('--app-dir', type=Path, default=ROOT)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--network-interface',default=os.environ.get('WORKCELL_NETWORK_INTERFACE','wlan0'))
    parser.add_argument('--network-timeout',type=float,default=60.)
    parser.add_argument('--network-check-only',action='store_true',help='네트워크 준비만 확인하고 종료 · ROS 노드·장치 연결 없음')
    options = parser.parse_args(args)
    ROOT = options.app_dir.expanduser().resolve()
    if not (ROOT / 'run_episode.sh').is_file() or not os.access(ROOT / 'run_episode.sh', os.X_OK):
        parser.error('--app-dir에 실행 가능한 run_episode.sh가 있어야 합니다.')
    if not (ROOT / 'integration/action_protocol.py').is_file():
        parser.error('--app-dir에 기존 integration/action_protocol.py가 있어야 합니다.')
    if options.check_only and os.environ.get('ROS_DOMAIN_ID', '0') == '40':
        parser.error('--check-only는 운영 도메인 40 대신 별도 도메인에서 실행하세요.')
    sys.path.insert(0, str(ROOT / 'integration'))
    import action_protocol

    # network-online.target alone may complete without a wait-online service.
    # Do this before rclpy.init so DDS sees the prepared interface at creation.
    if not options.check_only or options.network_check_only:
        from network_ready import wait_for_network
        wait_for_network(options.network_interface,options.network_timeout)
    if options.network_check_only:return

    rclpy.init(args=[])
    # ROS 기본 SIGINT/SIGTERM 처리로 대기 중인 executor도 깨웁니다.
    job_lock = threading.Lock()
    servers = [ArmActionServer(arm, job_lock, action_protocol, options.check_only) for arm in COMMANDS]
    executor = MultiThreadedExecutor(num_threads=4)
    for server in servers:
        executor.add_node(server)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        for server in servers:
            server.stop_process()
        executor.shutdown(timeout_sec=3)
        for server in servers:
            server._action_server.destroy()
            server.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
