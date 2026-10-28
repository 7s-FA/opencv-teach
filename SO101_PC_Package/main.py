"""관제 PC 실행: final 후 python3 main.py. 시작만으로 장치를 움직이지 않습니다."""
import argparse
import importlib
import os
import threading
import time

import rclpy
from rclpy.executors import SingleThreadedExecutor, ExternalShutdownException
from arm2_node import Arm2ActionClient, Arm3ActionClient
from control import RobotController
from console_ui import console


def main(args=None):
    parser = argparse.ArgumentParser(description='SO-101 팔2·팔3 관제 PC')
    parser.add_argument('--check-connection', action='store_true', help='두 Action 서버 검색만 확인 후 종료')
    parser.add_argument('--with-team-nodes', action='store_true',
                        help='기존 버거·와플·팔1·웹 노드도 로드 (해당 팀 파일 필요, 자동 공정은 별도 구현)')
    options, ros_args = parser.parse_known_args(args)
    team_classes = []
    if options.with_team_nodes:
        try:
            for module, name in [('web_node', 'WebDataPase'), ('burger1_node', 'Burger1ActionClient'),
                                 ('burger2_node', 'Burger2ActionClient'), ('waffle_node', 'waffleActionClient'),
                                 ('arm1_node', 'Arm1ActionClient')]:
                team_classes.append(getattr(importlib.import_module(module), name))
        except (ImportError, AttributeError) as exc:
            parser.error(f'기존 팀 모듈을 불러올 수 없습니다: {exc}')
    # final을 사용하지 않은 경우에도 기본 도메인을 맞추며, 명시된 검증 도메인은 유지합니다.
    os.environ.setdefault('ROS_DOMAIN_ID', '40')
    rclpy.init(args=ros_args)
    nodes = []
    # 모든 Action 요청은 비동기이며 입력은 별도 스레드에서 처리합니다.
    executor = SingleThreadedExecutor()
    try:
        arm2 = Arm2ActionClient(monitor_jobs=not options.check_connection)
        nodes.append(arm2)
        arm3 = Arm3ActionClient(monitor_jobs=not options.check_connection)
        nodes.append(arm3)
        if options.check_connection:
            for arm in (arm2,arm3):executor.add_node(arm)
            until=time.monotonic()+8.
            while time.monotonic()<until:
                executor.spin_once(timeout_sec=.1)
                if all(arm._client.server_is_ready() and arm.health.online() for arm in (arm2,arm3)):break
            connected=True
            for arm in (arm2,arm3):
                ready=arm._client.server_is_ready() and arm.health.online()
                print(f'/{arm.arm}/data: '+('양방향 생존 신호 확인' if ready else '연결 안 됨'))
                connected=connected and ready
            return 0 if connected else 1
        team = []
        for node_class in team_classes:
            instance = node_class()
            team.append(instance)
            nodes.append(instance)
        if team:
            web, burger1, burger2, waffle, arm1 = team
            controller = RobotController(burger1, burger2, waffle, arm1, arm2, arm3)
            print('팀 노드를 함께 실행합니다. 이 컨트롤러는 팔2·팔3 수동 관제이며 웹 주문 자동 공정은 실행하지 않습니다.', flush=True)
        else:
            controller = RobotController(arm2_node=arm2, arm3_node=arm3)
        nodes.append(controller)
        for node in nodes:
            executor.add_node(node)
        threading.Thread(target=controller.read_input, daemon=True).start()
        while rclpy.ok() and not controller.exit_requested.is_set():
            executor.spin_once(timeout_sec=0.2)
        return 0
    except (KeyboardInterrupt, ExternalShutdownException):
        return 0
    finally:
        console.close()
        print('관제 연결 종료. 이 관제가 시작한 작업은 생존 신호 만료 시 현재 자세를 유지하며 보류됩니다. 자동 재개하지 않습니다.',flush=True)
        executor.shutdown(timeout_sec=3)
        for node in reversed(nodes):
            node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    raise SystemExit(main())
