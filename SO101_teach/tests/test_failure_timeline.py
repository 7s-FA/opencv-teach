import unittest
from copy import deepcopy
from desktop_monitor.failure_timeline import summarize,display_fields


def fixture():
    def event(kind,at,detail,step=None):
        return {'run_id':'r','arm':'arm3','at':at,'status':'B_'+kind+':'+detail,
                'event':{'plan_id':'p','context_id':'c','step':step}}
    error='놓기 위치 · 안착 불합격'
    steps=[{'id':'place','name':'놓기 위치'},{'id':'lift','name':'놓고 들기'},{'id':'return','name':'복귀 위치'}]
    return {'record':{'failed_at':106.,'error':error,'context':{'run_id':'r','arm':'arm3','episode_id':'e',
                'completed_step_id':'lift','completed_step_name':'놓고 들기',
                'last_follower_state':{'state':'MOVING','index':2,'program_active':True,'request_id':7,'latest':{'wall_time':103.1}}},
                'inspection':{'steps':[steps[0]]}},
            'run':{'run_id':'r','events':[
                event('STEP_DONE',100.,'1/3:놓기 위치',{'step_id':'place','step_name':'놓기 위치'}),
                event('INSPECTION_STARTED',100.01,'place:놓기 위치'),
                event('STEP_DONE',103.1,'2/3:놓고 들기',{'step_id':'lift','step_name':'놓고 들기'}),
                event('INSPECTION_FAILED',103.3,error),event('FAILED',106.,error)],
                'analysis':{'motion_calls':[{'arm':'arm3','action':'play','plan_id':'p','context_id':'c','requests':[{'request_id':7}],'targets_ticks':[{}, {}, {}]}],
                    'plans':[{'plan_id':'p','context_id':'c','arm':'arm3','steps':steps}],
                    'configurations':{'c':{'episode':{'id':'e'}}}}},
            'inspection_log':{'episode_id':'e','checks':[{'result':'FAIL','error':error,'at':103.2}]}}


class FailureTimelineTests(unittest.TestCase):
    def test_parallel_inspection_distinguishes_trigger_progress_and_last_done(self):
        result=summarize(fixture())
        self.assertEqual(result['inspection_step'],'놓기 위치 (실행 1/3)')
        self.assertEqual(result['interrupted_step'],'복귀 위치 (실행 3/3)')
        self.assertEqual(result['last_completed_step'],'놓고 들기 (실행 2/3)')
        self.assertEqual(result['started_at'],100.01);self.assertEqual(result['completed_at'],103.2)
        self.assertAlmostEqual(result['duration_seconds'],3.19)
        self.assertIn('정지 확정 상태 미기록',result['stop_note'])

    def test_old_event_only_archive_does_not_call_last_done_actual_stop(self):
        p=fixture();p['record']={};p['run']['analysis']={};p['inspection_log']=None
        result=summarize(p)
        self.assertEqual(result['inspection_step'],'놓기 위치 (실행 1/3)')
        self.assertEqual(result['interrupted_step'],'미확인')
        self.assertEqual(result['completion_source'],'실패 알림');self.assertEqual(result['completed_at'],103.3)

    def test_resume_request_stale_state_or_other_episode_cannot_name_target(self):
        for change in ('resume','request','stale','episode','nonmoving'):
            p=fixture();state=p['record']['context']['last_follower_state']
            if change=='resume':p['run']['analysis']['motion_calls'][0]['requests'].append({'request_id':8})
            elif change=='request':state['request_id']=99
            elif change=='stale':state['latest']['wall_time']=99.
            elif change=='episode':p['run']['analysis']['configurations']['c']['episode']['id']='other'
            else:state['state']='HOLD'
            with self.subTest(change=change):self.assertEqual(summarize(p)['interrupted_step'],'미확인')

    def test_other_run_arm_and_post_failure_events_cannot_replace_evidence(self):
        p=fixture()
        for run_id,arm,at in [('other','arm3',103.4),('r','arm2',103.4),('r','arm3',107.)]:
            p['run']['events'].append({'run_id':run_id,'arm':arm,'at':at,'status':'B_STEP_DONE:99/99:다른 스텝'})
        self.assertEqual(summarize(p)['last_completed_step'],'놓고 들기 (실행 2/3)')

    def test_latest_retry_start_matches_failed_step_not_parallel_check(self):
        p=fixture();p['run']['events'][2:2]=[
            {'run_id':'r','arm':'arm3','at':101.,'status':'B_INSPECTION_STARTED:place:놓기 위치'},
            {'run_id':'r','arm':'arm3','at':102.,'status':'B_INSPECTION_STARTED:other:다른 검사'}]
        self.assertEqual(summarize(p)['started_at'],101.)

    def test_missing_logs_are_unknown_and_unrelated_old_failure_is_not_reused(self):
        p={'record':{'failed_at':200.,'error':'카메라 연결 오류','context':{'phase':'preparation'}},'run':{}}
        result=summarize(p)
        self.assertTrue(all(value=='미확인' for _,value in display_fields(result)))
        p=fixture();p['record'].update(error='통신 오류',inspection=None);p['inspection_log']=None
        p['run']['events'].append({'run_id':'r','arm':'arm3','at':105.,'status':'B_FAILED:통신 오류'})
        result=summarize(p);self.assertIsNone(result['started_at']);self.assertIsNone(result['completed_at'])

    def test_summary_never_mutates_archived_payload(self):
        p=fixture();before=deepcopy(p);summarize(p);self.assertEqual(p,before)
