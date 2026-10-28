# 로봇팔에 들어가는 Action Server 코드입니다.
# 원본과 동일하게 __init__ → goal_callback → execute_callback → main 순서입니다.
# Pi의 기존 실행부(run_episode.sh, integration/action_protocol.py)를 사용합니다.

import argparse
import fcntl
import json
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
from rclpy.duration import Duration
from std_msgs.msg import String
from host_pkg.action import Arm
from supervision_gate import SupervisionGate
from supervision_session import SupervisionHealth,RequestLedger,decode_goal

ROOT = Path(os.environ.get('SO101_APP_DIR', '/home/ubuntu/OpenCV_teach'))
COMMANDS = {
    'arm2': {'BUILD_A', 'BUILD_B', 'BUILD_LOAD_A', 'BUILD_LOAD_B', 'PC_BUILD_LOAD_A', 'PC_BUILD_LOAD_B', 'BUILD_SLIDE', 'PREPARE_HOME'},
    'arm3': {'LOAD_A', 'LOAD_B', 'LOAD_SLIDE'},
}
CONTROLS = {
    'EMER_STOP': 'estop', 'RESTART': 'restart', 'RESET': 'reset',
    'CLEAR_ERROR': 'clear_error',
}


class ArmActionServer(Node):
    def __init__(self, arm, job_lock, protocol, check_only=False, gate=None):
        super().__init__('data', namespace=arm)
        self.arm = arm
        self.job_lock = job_lock
        self.protocol = protocol;self.runtime_root=ROOT
        self.check_only = check_only
        self.process = None;self.dispatch_lock=None;self.ticket=None
        self.gate=gate if gate is not None else SupervisionGate();self.stop_ticket=None
        if not hasattr(self.gate,'health'):
            self.gate.health=SupervisionHealth(self,self.gate,self.notice)
            self.gate.ledger=RequestLedger(ROOT,self.gate.health.epoch)
        self.health=self.gate.health;self.ledger=self.gate.ledger
        if not hasattr(self.gate,'status_publishers'):
            self.gate.status_publishers={name:self.create_publisher(String,'/'+name+'/episode_status',10) for name in ('arm2','arm3')}
            self.gate.status_ready_at=time.monotonic()+1.
        self.admissions={};self.control_meta={}
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

    def publish_episode_status(self,arm,message):
        publisher=self.gate.status_publishers[arm]
        publisher.publish(String(data=message))
        publisher.wait_for_all_acked(Duration(seconds=1))

    def goal_callback(self, goal_request):
        try:meta=decode_goal(goal_request.command)
        except (ValueError,TypeError,RecursionError):
            self.notice('GOAL_REJECTED:PROTOCOL:SUPERVISION_V2_REQUIRED');return GoalResponse.REJECT
        command=meta['command'];attempt=meta['attempt_id']
        stopped=self.gate.stop_active() if command=='EMER_STOP' and meta.get('operation')!='query' else None
        with self.gate.lock:received_generation=self.gate.generation
        self.get_logger().info(f'Received goal request: CMD = {command}')
        self.notice('GOAL_RECEIVED:'+command)
        # Durable I/O is serialized independently of the urgent stop gate.
        with self.gate.admission_lock:
            if attempt in self.admissions:return GoalResponse.REJECT
            if command not in CONTROLS and command not in COMMANDS[self.arm]:
                self.notice('GOAL_REJECTED:'+command+':지원하지 않는 명령 또는 다른 팔의 명령');return GoalResponse.REJECT
            if not meta.get('legacy'):
                try:existing=self.ledger.lookup(meta,self.arm)
                except (OSError,json.JSONDecodeError):
                    # A storage fault cannot establish absence or authorize execution.
                    self.admissions[attempt]={'meta':meta,'replay':True,'storage_pending':True}
                    return GoalResponse.ACCEPT
                except ValueError:
                    self.notice('GOAL_REJECTED:'+command+':REQUEST_ID_CONFLICT');return GoalResponse.REJECT
                if existing is None and meta.get('operation')=='query':
                    # Fence an uncertain, never-admitted request against late delivery.
                    try:
                        self.ledger.accept(meta,self.arm);self.ledger.finish(meta,self.arm,False,'NOT_FOUND')
                        existing=self.ledger.lookup(meta,self.arm)
                    except (OSError,json.JSONDecodeError):
                        self.admissions[attempt]={'meta':meta,'replay':True,'storage_pending':True}
                        return GoalResponse.ACCEPT
                    except Exception:return GoalResponse.REJECT
                if existing is not None:
                    self.admissions[attempt]={'meta':meta,'replay':True}
                    return GoalResponse.ACCEPT
                if command!='EMER_STOP' and not self.health.accepts(meta):
                    self.notice('GOAL_REJECTED:'+command+':COMM_LOST_OR_EXPIRED_REQUEST');return GoalResponse.REJECT
            if command in CONTROLS:
                if not self.gate.accept_control(command):
                    self.notice('CONTROL_REJECTED:'+command+':CONTROL_PENDING');return GoalResponse.REJECT
                try:
                    if not meta.get('legacy'):self.ledger.accept(meta,self.arm)
                except Exception:
                    self.gate.release_control(command);return GoalResponse.REJECT
                if command=='EMER_STOP':self.stop_ticket=stopped
                self.admissions[attempt]={'meta':meta,'replay':False,'generation':received_generation}
                self.notice('CONTROL_ACCEPTED:'+command);return GoalResponse.ACCEPT
            if self.gate.controls:
                self.notice('GOAL_REJECTED:'+command+':CONTROL_PENDING');return GoalResponse.REJECT
            if not self.job_lock.acquire(blocking=False):
                self.notice('GOAL_REJECTED:'+command+':BUSY 다른 작업이 실행 중입니다.');return GoalResponse.REJECT
            reservation=None;ticket=None
            try:
                reservation=open(ROOT/'data/episode-cli.lock','a');fcntl.flock(reservation,fcntl.LOCK_EX|fcntl.LOCK_NB)
                ticket=self.gate.reserve(meta['request_id'],meta['client_id'])
                with self.gate.lock:
                    if self.gate.generation!=received_generation:ticket.stop()
                self.ledger.accept(meta,self.arm)
            except Exception as exc:
                self.gate.retire(ticket)
                if reservation:reservation.close()
                self.job_lock.release()
                self.notice('GOAL_REJECTED:'+command+(':BUSY 다른 작업이 실행 중입니다.' if isinstance(exc,BlockingIOError) else ':DISPATCH_UNAVAILABLE'))
                return GoalResponse.REJECT
            self.dispatch_lock=reservation;self.ticket=ticket
            self.admissions[attempt]={'meta':meta,'replay':False,'ticket':ticket,'reservation':reservation}
            self.notice('GOAL_ACCEPTED:'+command);return GoalResponse.ACCEPT

    def execute_callback(self, goal_handle):
        meta=decode_goal(goal_handle.request.command);command=meta['command'];attempt=meta['attempt_id']
        admission=self.admissions.get(attempt)
        if admission is None:raise RuntimeError('MISSING_ADMISSION')
        if admission['replay']:
            result=Arm.Result()
            try:
                try:result.success,result.message=self.ledger.response(meta,self.arm)
                except (OSError,json.JSONDecodeError):result.success,result.message=False,'STORAGE_PENDING'
                # A failed admission lookup may recover before this callback.
                # UNKNOWN would stop automatic queries before an absent ID can
                # be fenced by the next query. Preserve this transient failure;
                # a fresh admission still decides a genuinely old UNKNOWN job.
                if admission.get('storage_pending') and result.message=='UNKNOWN':
                    result.success,result.message=False,'STORAGE_PENDING'
                self.notice('REQUEST_REPLAY:'+meta['request_id']+':'+result.message)
                goal_handle.succeed();return result
            finally:self.admissions.pop(attempt,None)
        feedback_msg = Arm.Feedback()
        result = Arm.Result()

        def publish_feedback(message):
            if feedback_msg.message != message:
                feedback_msg.message = message
                goal_handle.publish_feedback(feedback_msg)

        publish_feedback('RUNNING')
        is_control = command in CONTROLS
        process=None;lock_owned=not is_control;reservation=None;ticket=None;status_relay=None
        try:
            if is_control:
                if self.gate.cancelled(command):raise RuntimeError('CONTROL_SUPERSEDED: 비상정지로 제어 요청 취소')
                control = CONTROLS[command]
                self.control_meta[control]={**meta,'_gate_generation':admission['generation']}
                result.success = self.invoke_control(control) and not self.gate.cancelled(command)
                state = {'estop': 'PAUSED', 'restart': 'RUNNING', 'reset': 'IDLE', 'clear_error': 'IDLE'}[control]
            else:
                reservation=admission['reservation'];ticket=admission['ticket']
                args = [str(ROOT / 'run_episode.sh'), {'BUILD_SLIDE': 'slide_build', 'LOAD_SLIDE': 'slide_load'}.get(command, command.lower()), '--managed-completion','--execution-lock-fd',str(reservation.fileno()),'--supervision-job-id',ticket.job_id,'--stop-fd',str(ticket.read_fd)]
                if meta.get('client_id'):args+=['--supervision-client-id',meta['client_id']]
                if self.check_only:
                    args += ['--check', '--no-ros']
                status_fds=()
                if not self.check_only:
                    from status_transport import StatusSocketRelay
                    remaining=self.gate.status_ready_at-time.monotonic()
                    if remaining>0:time.sleep(remaining)
                    status_relay=StatusSocketRelay(self.publish_episode_status)
                    status_fds=(status_relay.child.fileno(),);args+=['--status-fd',str(status_fds[0])]
                with self.gate.lock:
                    if ticket.stopped or not self.health.accepts(meta):raise RuntimeError('START_CANCELLED: 비상정지 또는 관제 연결 확인 실패')
                    ticket.started=True
                # A concurrent stop remains buffered in the inherited pipe;
                # the child consumes it before preparation. Popen never holds the stop gate.
                process = subprocess.Popen(
                    args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.PIPE,pass_fds=(reservation.fileno(),ticket.read_fd,*status_fds), text=True, bufsize=1, start_new_session=True,
                )
                if status_relay:status_relay.child.close()
                ticket.close_reader()
                reservation.close()
                if self.dispatch_lock is reservation:self.dispatch_lock=None
                self.process=process
                last = ''
                for line in process.stdout:
                    if line.strip()=='READY_TO_COMPLETE':
                        # CLI has already closed device resources and its file
                        # lock. Release the Action gate before it publishes DONE.
                        self.gate.retire(ticket)
                        if lock_owned:self.job_lock.release();lock_owned=False
                        process.stdin.write('COMPLETE\n');process.stdin.flush()
                        continue
                    detail=line.strip()
                    # Optional post-commit diagnostics must not replace the
                    # terminal protocol event. Exit status still gates success.
                    if detail.startswith('EXECUTOR_WARNING:'):
                        self.get_logger().warning(f'{command}: {detail}')
                        self.notice('EXECUTOR_OUTPUT:'+command+':'+detail)
                        continue
                    last = detail
                    self.get_logger().info(f'{command}: {last}')
                    if last and not last.startswith(('A_','B_','SLIDE_','CHECK_OK ')):
                        self.notice('EXECUTOR_OUTPUT:'+command+':'+last)
                    state = self.protocol.feedback_word(last)
                    if command.startswith(('BUILD_LOAD_','PC_BUILD_LOAD_')) and last in ('A_DONE','B_DONE'):state=None
                    # A correlated handoff milestone permits the transport robot
                    # to leave while arm3 loads. It is not whole-job completion.
                    if command.startswith(('BUILD_LOAD_','PC_BUILD_LOAD_')) and last == command[-1]+'_BUILD_DONE':
                        state = 'BUILD_DONE'
                    if state:
                        publish_feedback(state)
                result.success = self.protocol.job_succeeded(
                    process.wait(), last, self.check_only, chained=command.startswith(('BUILD_LOAD_','PC_BUILD_LOAD_')),
                    home=command=='PREPARE_HOME',
                )
                state = 'IDLE'
            if is_control or lock_owned or not result.success:publish_feedback(state if result.success else 'ERROR')
        except Exception as exc:
            self.get_logger().error(f'{command}: {exc}')
            self.notice('ACTION_FAILED:'+command+':'+str(exc))
            result.success = False
            publish_feedback('ERROR')
        finally:
            if is_control:
                self.control_meta.pop(CONTROLS[command],None);self.gate.release_control(command)
            else:
                self.gate.retire(ticket)
                if reservation is not None:reservation.close()
                if self.dispatch_lock is reservation:self.dispatch_lock=None
                self.stop_process(process)
                if status_relay:status_relay.close()
                if lock_owned:self.job_lock.release()
                if self.process is process:self.process=None

        result.message = 'IDLE' if result.success else 'ERROR'
        if not meta.get('legacy'):
            try:self.ledger.finish(meta,self.arm,result.success,result.message)
            except Exception as exc:
                result.success=False;result.message='UNKNOWN';self.notice('REQUEST_LEDGER_FAILED:'+str(exc))
        self.admissions.pop(attempt,None)
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
        meta=self.control_meta.get(command)
        if meta and command!='estop':
            if not self.health.accepts(meta):return False
            with self.gate.lock:
                if self.gate.generation!=meta['_gate_generation']:return False
        if command == 'clear_error':
            from standby_control import cancel
            cancel(ROOT)
            from linear_client import LinearClient
            # Status only: no lease, PWM output, arm resume or safe return.
            if LinearClient().call('status').get('phase') != 'TIMED_COMPLETE':
                self.notice('CONTROL_REJECTED:clear_error:리니어 완료 확인 필요')
                return False
            cleared=self.protocol.clear_paused_job(ROOT,self.job_lock)
            if cleared and meta:
                count=self.ledger.clear_abandoned(meta['client_id'])
                self.notice('ABANDONED_REQUESTS_CLEARED:'+str(count))
            return cleared
        ticket=(self.stop_ticket or self.gate.stop_active()) if command=='estop' else self.gate.active
        if command=='estop' and ticket is not None and not ticket.started:
            # The same gate guards Popen: a stopped reservation cannot launch.
            return True
        deadline=time.monotonic()+25
        while True:
            try:state=self.protocol.control_state(ROOT)
            except (FileNotFoundError,ConnectionRefusedError):state=None
            if state is not None and state.get('job_id'):
                if meta and command in ('restart','reset'):
                    if not self.health.control_fence(meta,state):
                        self.notice('CONTROL_REJECTED:'+command+':STALE_PC_STOP_CONTEXT');return False
                    if ticket is None or not self.health.fresh(ticket.client_id):
                        self.notice('CONTROL_REJECTED:'+command+':UNMONITORED_OR_DISCONNECTED_JOB');return False
                break
            if command=='reset' and ticket is None:
                from standby_control import cancel
                cancel(ROOT)
                return self.reset_idle()
            if command=='estop' and ticket is None:
                from standby_control import cancel
                if cancel(ROOT):return True
            if command=='estop' and ticket is None and self.gate.cancelled('RESET'):return True
            if command!='estop' or ticket is None or ticket.finished or time.monotonic()>=deadline:
                self.notice('CONTROL_REJECTED:'+command+':NO_ACTIVE_JOB');return False
            time.sleep(.05)
        if command in ('restart','reset'):
            with self.gate.lock:
                if meta and (self.gate.generation!=meta['_gate_generation'] or not self.health.accepts(meta)
                             or ticket is not self.gate.active or not self.health.fresh(ticket.client_id)):return False
                # Rearm before movement is permitted, including the entire RESET wait.
                if ticket:ticket.link_lost=False
        try:
            receipt=self.protocol.control_request(ROOT,command,job_id=ticket.job_id if ticket else state.get('job_id'),
                **({'request_id':meta['request_id']} if meta and not meta.get('legacy') else {}),
                **({'stop_generation':state.get('stop_generation')} if command in ('restart','reset') else {}))
        except (OSError,ValueError) as exc:
            self.notice('CONTROL_REJECTED:'+command+':'+str(exc));return False
        self.notice('CONTROL_REQUEST:'+command+':'+receipt['request_id'])
        confirmed=self.protocol.wait_control(ROOT,command,receipt)
        if not confirmed:self.notice('CONTROL_FAILED:'+command+':실제 완료 확인 실패 또는 요청 취소')
        return confirmed

    def reset_idle(self):
        with self.gate.lock:
            meta=self.control_meta.get('reset') or {}
            if self.gate.cancelled('RESET') or (meta and (self.gate.generation!=meta['_gate_generation'] or not self.health.accepts(meta))):return False
            if meta and not self.health.control_fence(meta,{}):return False
            ticket=self.gate.reserve(meta.get('request_id'),meta.get('client_id'))
        try:
            with self.gate.lock:
                if ticket.stopped:return False
                ticket.started=True
            result=subprocess.run([str(ROOT/'run_episode.sh'),'reset','--recovery-arm',self.arm,'--no-ros',
                '--supervision-job-id',ticket.job_id,'--stop-fd',str(ticket.read_fd)],
                pass_fds=(ticket.read_fd,),capture_output=True,text=True,timeout=300)
            detail=(result.stdout+result.stderr).strip();self.get_logger().info('reset: '+detail)
            self.notice('CONTROL_REPLY:reset:'+detail)
            return result.returncode==0 and bool(result.stdout.strip()) and result.stdout.strip().splitlines()[-1].endswith('_RESET_DONE')
        finally:self.gate.retire(ticket)

    def destroy_node(self):
        if self.health.node is self:self.health.close()
        return super().destroy_node()

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
    gate=SupervisionGate()
    servers = [ArmActionServer(arm, job_lock, action_protocol, options.check_only,gate=gate) for arm in COMMANDS]
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
