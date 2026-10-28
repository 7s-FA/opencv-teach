from contextlib import redirect_stdout
import io,unittest
from tests.test_work_monitor import monitor

class OperatorLogTranslationTests(unittest.TestCase):
    def test_known_notices_are_readable_and_details_retain_the_original(self):
        cases={
            'ABANDONED_REQUESTS_CLEARED:0':'미확정 요청 기록 정리: 0건',
            'ABANDONED_REQUESTS_CLEARED:12':'미확정 요청 기록 정리: 12건',
            'COMM_LOST_HOLD_REQUESTED:'+'a'*32:'관제 통신 끊김 · 팔 정지·작업 보류 요청',
            'REQUEST_REPLAY:'+'a'*32+':CLEARED':'기존 요청 결과 조회: 미확정 기록 정리됨',
            'REQUEST_LEDGER_FAILED:disk full':'요청 결과 기록 저장 실패: disk full',
            'CONTROL_RESULT:CLEAR_ERROR:IDLE':'제어 결과: 오류 해제 · 완료',
            'CONTROL_RESULT:RESTART:UNKNOWN':'제어 결과: 재개 · 결과 미확인',
            'CONTROL_RESTART:REJECTED:NO_ACTIVE_JOB':'재개 요청 거절: 진행 중인 작업 없음',
            'GOAL_REJECTED:BUILD_A:COMM_LOST_OR_EXPIRED_REQUEST':'관제 통신 단절 또는 요청 유효시간 만료',
            'CONTROL_REJECTED:reset:STALE_PC_STOP_CONTEXT':'현재 정지 상태보다 오래된 제어 요청',
            'B_PLAN_DISCARDED:PREPARATION_CHANGED':'준비 조건 변경 · 기존 경로 폐기',
            'B_RESET_TORQUE_ON_STARTED':'안전 복귀를 위한 토크 켜기 시작',
            'HOME_ARM_RESET_DONE:arm2':'안전 복귀 완료 · 토크 유지',
            'HOME_ARM_SAFE_ALREADY:arm3':'안전 자세 일치 · 이동 생략',
            'HOME_RESET_DONE':'두 팔 안전 복귀·리니어 하차 위치 이동 완료',
            'HOME_FAILED:stopped':'안전 준비 · 작업 실패',
            'ACTION_RESULT:BUILD_A:UNKNOWN':'실행 결과: A 조립 결과 미확인',
        }
        for raw,expected in cases.items():
            with self.subTest(raw=raw):
                event=dict(at=1,arm='arm2',status=raw)
                with redirect_stdout(io.StringIO()) as output:monitor.print_event(event)
                self.assertIn(expected,output.getvalue())
                if raw.endswith(':UNKNOWN'):self.assertNotIn('실패',output.getvalue())
                if raw.startswith('HOME_ARM_RESET_DONE'):self.assertNotIn('해제',output.getvalue())
                with redirect_stdout(io.StringIO()) as output:monitor.print_event(event,details=True)
                self.assertIn(raw,output.getvalue());self.assertEqual(event['status'],raw)

    def test_unknown_errors_are_not_discarded(self):
        event=dict(at=1,arm='arm2',status='GOAL_REJECTED:BUILD_A:NEW_CODE:offset=-2.34')
        with redirect_stdout(io.StringIO()) as output:monitor.print_event(event)
        self.assertIn('NEW_CODE:offset=-2.34',output.getvalue())
