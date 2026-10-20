"""관제 PC의 수동 작업 지시. 입력 대기와 ROS 콜백을 분리합니다."""
import queue
import threading

from rclpy.node import Node
from console_ui import console, PromptLogger


HELP = '''
팔2·팔3 관제 명령 (작업 명령은 실제 장치를 움직입니다)
  BUILD_A           팔2 A 조립
  LOAD_A            팔3 A 적재
  BUILD_B           팔2 B 조립
  LOAD_B            팔3 B 적재
  BUILD_LOAD_A      팔2 A 조립 → 팔3 A 적재
  BUILD_LOAD_B      팔2 B 조립 → 팔3 B 적재
  BUILD_SLIDE       리니어 조립 위치
  LOAD_SLIDE        리니어 하차 위치
  PREPARE_HOME      두 팔 안전 자세 복귀 → 리니어 하차 위치 (팔 토크 유지)
  EMER_STOP         팔 정지·작업 보류 (리니어는 계속 진행)
  RESTART           팔/작업 재개 (리니어 재명령 없음)
  RESET             작업 취소·두 팔 순차 안전 복귀·토크 OFF
  CLEAR_ERROR       보류 작업 종료·오류 해제 (이동·토크 변경 없음)
  STATUS            서버 연결과 이 관제에서 보낸 작업 상태
  HELP              명령 목록
  QUIT              관제 프로그램 종료 (Pi 작업은 계속됨)
Ctrl+C도 관제만 종료합니다. 로봇 정지는 EMER_STOP을 사용하세요.
'''
COMMANDS = {
    'build_a': 'arm2', 'build_b': 'arm2', 'build_slide': 'arm2',
    'build_load_a': 'arm2', 'build_load_b': 'arm2', 'prepare_home': 'arm2',
    'load_a': 'arm3', 'load_b': 'arm3', 'load_slide': 'arm3',
}
# 원본의 1=build_a, 2=load_a를 유지합니다.
KEYS = {'1': 'build_a', '2': 'load_a', '3': 'build_b',
        '4': 'load_b', '5': 'build_slide', '6': 'load_slide'}
CONTROLS = {'estop': 'EMER_STOP', 'emer_stop': 'EMER_STOP', 'restart': 'RESTART', 'reset': 'RESET', 'clear_error': 'CLEAR_ERROR'}


class RobotController(Node):
    def __init__(self, burger1_node=None, burger2_node=None, waffle_node=None,
                 arm1_node=None, arm2_node=None, arm3_node=None):
        super().__init__('control', namespace='host')
        self.console_logger = PromptLogger(self.get_logger())
        self.burger1, self.burger2, self.waffle = burger1_node, burger2_node, waffle_node
        self.arm1, self.arm2, self.arm3 = arm1_node, arm2_node, arm3_node
        if self.arm2 is None or self.arm3 is None:
            raise ValueError('팔2·팔3 Action 클라이언트가 필요합니다.')
        self.commands = queue.Queue()
        self.exit_requested = threading.Event()
        self.active_arm = 'arm2'
        self.timer = self.create_timer(0.1, self.controlRobot)

    def read_input(self):
        # input()은 executor의 timer 콜백 안에서 호출하지 않습니다.
        console.write(HELP)
        while not self.exit_requested.is_set():
            try:
                command = console.read().strip()
            except (EOFError, KeyboardInterrupt):
                self.commands.put('quit')
                break
            if command:
                self.commands.put(command)
            if command.lower() in ('q', 'quit', 'exit'):
                break

    def controlRobot(self):
        try:
            command = self.commands.get_nowait()
        except queue.Empty:
            return
        try:
            self.dispatch(command)
        except Exception as exc:
            self.console_logger.error(f'명령 처리 오류: {exc}')

    def dispatch(self, command):
        command = command.strip().lower()
        command = KEYS.get(command, command)
        if command.lower() in ('q', 'quit', 'exit'):
            self.exit_requested.set()
            return
        if command.lower() == 'help':
            console.write(HELP)
            return
        if command.lower() == 'status':
            for arm in (self.arm2, self.arm3):
                online = arm._client.server_is_ready()
                self.console_logger.info(
                    f'{arm.arm}: 서버 연결={online}, 이 PC의 작업 결과 대기={arm.busy}, '
                    f'최근 Feedback={arm.feedback}',
                )
            self.console_logger.info('다른 PC에서 시작한 작업은 Pi의 work_watch로 확인하세요.')
            return
        control = CONTROLS.get(command.lower())
        if control:
            # 제어 요청 Result는 원래 작업의 완료 플래그와 분리됩니다.
            arm_name = next((arm.arm for arm in (self.arm2, self.arm3) if arm.busy), self.active_arm)
            return getattr(self, arm_name).send_goal(control)
        if command not in COMMANDS:
            self.console_logger.warning('알 수 없는 명령입니다. help로 목록을 확인하세요.')
            return
        if self.arm2.busy or self.arm3.busy:
            self.console_logger.warning('진행 중인 작업의 최종 결과를 기다려주세요. 제어 명령은 사용할 수 있습니다.')
            return
        self.active_arm = COMMANDS[command]
        return getattr(self, self.active_arm).send_goal(command.upper())
