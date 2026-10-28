"""Terminal presentation preserves diagnostic evidence and aligned measurements."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import re
import unicodedata
import unittest
from tests.test_work_monitor import monitor
from episode_timing import preparation_lines


class LogLayoutTests(unittest.TestCase):
    def test_korean_and_latin_labels_align_with_precise_values_and_counts(self):
        profile={'metrics':{
            'arm2.load.total':{'total_seconds':1.234,'count':1},
            'arm2.camera.model_warmup':{'total_seconds':12.345,'count':2},
            'arm2.jig.opencv_seconds':{'total_seconds':.007,'count':123},
            'arm2.inspection.ai':{'total_seconds':1234.567,'count':12},
            'arm2.planning.rpc':{'total_seconds':98.765,'count':3},
            'arm3.load.total':{'total_seconds':999.999,'count':99}}}
        original=deepcopy(profile)
        lines=preparation_lines(profile,'arm2')
        columns=[]
        for line in lines:
            match=re.search(r'(\d+\.\d{3})\s+(\d+)$',line)
            if match:
                columns.append(tuple(sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1
                                         for c in line[:match.end(i)]) for i in (1,2)))
        self.assertEqual(len(columns),5)
        self.assertEqual(len(set(columns)),1)
        self.assertTrue(all(len(line.expandtabs())<80 for line in lines))
        self.assertIn('1.234','\n'.join(lines));self.assertIn('1234.567','\n'.join(lines))
        self.assertNotIn('999.999','\n'.join(lines))
        self.assertEqual(profile,original)

    def test_known_events_are_korean_and_details_keep_exact_original_ids(self):
        key='263bd4dde3da4a3583d9797d98e31ad8'
        cases={
            'B_INSPECTION_MODEL_PREPARING':'안착 검사 모델 준비 중',
            'GOAL_ACCEPTED:BUILD_LOAD_B':'명령 접수: B 조립·적재',
            'CONTROL_REQUEST:estop:'+key:'팔 정지 요청',
            'B_IDLE_RELEASE_SCHEDULED:seconds=3':'3초 뒤',
            'B_RESET_DONE':'전체 팔 안전 초기화 완료',
            'ACTION_RESULT:build_b:ERROR':'실행 결과: B 조립 실패',
            'B_FAILURE_IMAGE_SAVE_FAILED:disk full':'실패 영상 저장 실패: disk full',
            'B_NEW_WARNING:unknown':'B_NEW_WARNING:unknown'}
        for raw,expected in cases.items():
            with self.subTest(raw=raw):
                event=dict(at=1,arm='arm2',status=raw)
                with redirect_stdout(io.StringIO()) as output:monitor.print_event(event)
                self.assertIn(expected,output.getvalue())
                with redirect_stdout(io.StringIO()) as output:monitor.print_event(event,{key:'스텝'},details=True)
                self.assertIn(raw,output.getvalue())
                self.assertEqual(event['status'],raw)

    def test_saved_failure_path_stays_usable_and_profile_is_not_raw_json(self):
        path='data/diagnostics/action-failures/20261022-172400-263bd4dde3da4a3583d9797d98e31ad8/error.json'
        profile={'metrics':{'arm2.load.total':{'total_seconds':1.234,'count':1}}}
        with redirect_stdout(io.StringIO()) as output:
            monitor.print_event(dict(at=1,arm='arm2',status='B_FAILURE_IMAGES:'+path))
            monitor.print_event(dict(at=2,arm='arm2',status='B_PREPARATION_PROFILE:'+json.dumps(profile)))
        shown=output.getvalue()
        self.assertIn(path,shown);self.assertIn('설정 로딩',shown)
        self.assertIn('1.234',shown);self.assertNotIn('total_seconds',shown)
