import unittest
from copy import deepcopy
from desktop_monitor.model import summary,run_events,event_row,current_run

def fixture():
    run={'run_id':'a'*32,'job_id':'job1','arm':'arm2','started':10.,'at':14.,'status':'B_INSPECTION_STARTED:step:하단 확인','operation':'BUILD_B',
         'expected':{'arm2':[{'step_id':'step','step_name':'하단 확인','check':{'expected':'housing_seated'}}]},'checks':{}}
    events=[{'run_id':run['run_id'],'sequence':i+1,'arm':'arm2','at':10.+i,'status':s} for i,s in enumerate(['B_ACCEPTED','B_JIG_POSE:jig:x_mm=1:y_mm=2:yaw_deg=3','B_STEP_DONE:11/28:중단 잡기 위치','B_INSPECTION_STARTED:step:하단 확인'])]
    return {'runs':[run],'events':events,'names':{'jig':'운반 지그','step':'하단 확인'},'control':{'known':True,'info':{'run_id':run['run_id'],'arm':'arm2'},'job_id':'job1','paused':False}}

class WorkModelTests(unittest.TestCase):
    def test_actual_events_show_after_step_check_without_inventing_pass(self):
        s=fixture();r=s['runs'][0];out=summary(s,r)
        self.assertEqual(out['checks'][0]['state'],'검사 중');self.assertEqual(out['last'],'11/28:중단 잡기 위치')
        self.assertEqual(out['jigs'][0]['name'],'운반 지그');self.assertNotIn('이동 중',out['current'])
    def test_new_run_does_not_inherit_previous_adoption_or_pass(self):
        s=fixture();old=s['runs'][0];new={**old,'run_id':'b'*32,'job_id':'job2','started':20.,'at':20.}
        s['runs'].insert(0,new);s['control'].update(info={'run_id':new['run_id'],'arm':'arm2'},job_id='job2')
        out=summary(s,current_run(s));self.assertEqual(out['jigs'],[]);self.assertEqual(out['checks'][0]['state'],'대기')
        self.assertEqual(run_events(s,new),[])
    def test_invalidated_pass_remains_pending_reinspection(self):
        s=fixture();r=s['runs'][0];r['checks']={'arm2':{'records':[{'step_id':'step','at':12.,'result':'PASS'}]}}
        s['events'].append({'run_id':r['run_id'],'sequence':5,'arm':'arm2','at':14.,'status':'B_INSPECTION_INVALIDATED:step'})
        self.assertEqual(summary(s,r)['checks'][0]['state'],'재검사')
    def test_no_control_socket_never_calls_old_completed_record_running(self):
        s=fixture();s['control']={'known':True,'info':None};self.assertFalse(summary(s,current_run(s))['live'])
        self.assertIn('최근 기록',summary(s,current_run(s))['phase'])
    def test_detailed_unknown_events_are_preserved_and_linear_is_not_measured(self):
        values,_=event_row({'at':10.,'status':'B_NEW_EVENT:detail','arm':'arm2'},{});self.assertEqual(values[2: ],('NEW_EVENT','detail'))
        values,_=event_row({'at':10.,'status':'B_LINEAR_DONE:target_mm=100','arm':'arm2'},{});self.assertIn('실측 없음',values[3])
    def test_current_step_requires_matching_single_motion_request(self):
        s=fixture();r=s['runs'][0]
        r.update(follower=[{'arm':'arm2','state':'MOVING','request_id':8,'index':0}],motions=[{'arm':'arm2','action':'play','plan_id':'p','requests':[{'request_id':8}],'finished_elapsed_s':None}],plans=[{'plan_id':'p','arm':'arm2','context_id':'c','steps':[{'id':'target','name':'대기 위치'}]}],contexts={'c':{'episode':{'steps':[{'id':'safe'},{'id':'target'}]}}})
        self.assertEqual(summary(s,r)['current'],'이동 중 · 02/2 · 대기 위치')
        r['motions'][0]['requests'].append({'request_id':9})
        self.assertNotIn('대기 위치',summary(s,r)['current'])
    def test_outlines_require_current_job_adoption_and_matching_frozen_pose(self):
        from desktop_monitor.model import adopted_outlines
        s=fixture();r=s['runs'][0];s['camera']={'arm':'arm2','frozen':{'jig':{'quad':[[0,0],[1,0],[1,1],[0,1]],'center_xy_mm':[1.,2.],'yaw_deg':3.}}}
        self.assertEqual(len(adopted_outlines(s,r)),1)
        s['camera']['frozen']['jig']['center_xy_mm']=[100.,2.];self.assertEqual(adopted_outlines(s,r),[])
        s['control']['info']=None;self.assertEqual(adopted_outlines(s,r),[])
    def test_active_executor_waiting_for_log_is_not_shown_idle(self):
        s=fixture();s['runs']=[]
        self.assertIn('기록 수신 대기',summary(s,current_run(s))['phase'])

class WorkRecordParityTests(unittest.TestCase):
    def test_summary_uses_terminal_rules_for_reset_failures_and_timing(self):
        from desktop_monitor.model import record_lines
        s=fixture();run=s['runs'][0]
        statuses=['GOAL_RECEIVED:RESET','B_ARM_RESET_STARTED:arm3','B_ARM_RESET_DONE:arm3','B_RESET_DONE',
                  'A_INSPECTION_COUNT:step:1/2','A_INSPECTION_FAILED:완제품: 관측 부품 없음 · ROI 밖 5.7%',
                  'A_FAILED:완제품: 관측 부품 없음 · ROI 밖 5.7%','ACTION_RESULT:build_load_a:ERROR']
        s['events']=[dict(at=20.+i,arm='arm2',status=status,run_id=run['run_id'],sequence=i+1) for i,status in enumerate(statuses)]
        s['events'][-2]['timing_summary']={'total_seconds':110.,'completed':False,'phases':[{'phase':'load','seconds':24.4,'completed':False}]}
        text=''.join(row[0] for row in record_lines(s,run))
        self.assertNotIn('GOAL_RECEIVED',text);self.assertNotIn('INSPECTION_COUNT',text)
        self.assertIn('팔3 | 안전 복귀 시작',text);self.assertIn('조립대 | 전체 팔 안전 초기화 완료',text)
        self.assertEqual(text.count('ROI 밖 5.7%'),1);self.assertIn('동일 실패 원인 참조',text)
        self.assertIn('구간: 하차 24.4초 (중단)',text);self.assertIn('소요 110.0초',text)
        self.assertIn('실행 결과: A 조립·적재 실패',text)
        raw=''.join(row[0] for row in record_lines(s,run,details=True))
        self.assertIn('GOAL_RECEIVED:RESET',raw);self.assertIn('A_INSPECTION_COUNT:step:1/2',raw)
        self.assertIn('전체 110.000초',raw)

    def test_default_log_includes_independent_notices_after_job_finished(self):
        from desktop_monitor.model import record_lines
        s=fixture();run=s['runs'][0]
        s['events'].append(dict(at=run['at']+4,arm='arm3',status='IDLE_TORQUE_RELEASE_REQUESTED',run_id='c'*32,sequence=1))
        self.assertIn('3초 정지 후 토크 해제 요청',''.join(r[0] for r in record_lines(s,run)))
        self.assertNotIn('토크 해제 요청',''.join(r[0] for r in record_lines(s,run,all_events=False)))
        s['runs']=[]
        self.assertIn('토크 해제 요청',''.join(r[0] for r in record_lines(s,None)))
