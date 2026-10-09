import json,queue,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from so101_teach.linear_state import StateStream,decode_report,decode_state,apply_state,state_label,stream_command
from so101_teach.preview import scene_signature

class LinearStreamTests(unittest.TestCase):
    def wait_state(self,stream,predicate,seconds=5):
        end=time.monotonic()+seconds;seen=[]
        while time.monotonic()<end:
            try:state=stream.results.get(timeout=.1)
            except queue.Empty:continue
            seen.append(state)
            if predicate(state):return state
        self.fail(str(seen))
    def test_subscriber_is_read_only_and_uses_configured_domain_topic(self):
        args,script=stream_command({'host':'localhost','user':'robot'},{'environment_script':'/tmp/ros.bash','topic':'/arm3/linear_state','domain_id':40})
        self.assertIn('create_subscription',script);self.assertIn('"ROS_DOMAIN_ID"]=\'40\'',script)
        self.assertIn("TOPIC='/arm3/linear_state'",script);self.assertNotIn('create_publisher',script);self.assertNotIn('lgpio',script)
        self.assertIn('/usr/bin/python3 -u -',args[-1])
    def test_heartbeat_expiry_is_distinct_from_controller_without_a_command(self):
        unknown=decode_report({'pulse_us':-1,'age_s':.1});self.assertFalse(unknown['known']);self.assertTrue(unknown['connected'])
        self.assertIn('명령 기록 없음',state_label(apply_state({'linear_stage':{}},unknown)))
        stale=decode_report({'pulse_us':2000,'age_s':1.1});self.assertFalse(stale['known']);self.assertFalse(stale['connected'])
        for bad in (-1,float('nan'),True,'0'):
            with self.assertRaises(ValueError):decode_report({'pulse_us':2000,'age_s':bad})
    def test_repeated_heartbeat_does_not_enqueue_rebuilds(self):
        stream=StateStream({},{});stream.publish(decode_report({'pulse_us':2000,'age_s':.1}));stream.results.get_nowait()
        for _ in range(20):stream.publish(decode_report({'pulse_us':2000,'age_s':.05}))
        self.assertTrue(stream.results.empty())
        stream.publish(decode_report({'pulse_us':1015,'age_s':.1}));self.assertEqual(stream.results.get_nowait()['commanded_mm'],1.5)
    def test_disconnect_keeps_last_command_explicit_and_recovery_updates_geometry(self):
        p={'linear_stage':{'stroke_mm':0}}
        p=apply_state(p,decode_report({'pulse_us':2000,'age_s':.1}));signature=scene_signature({'jigs':[],'workcell':p})
        p=apply_state(p,decode_report({'pulse_us':2000,'age_s':2}));self.assertEqual(p['linear_stage']['stroke_mm'],100)
        self.assertIn('수신 끊김',state_label(p));self.assertIn('마지막 전진',state_label(p))
        self.assertEqual(signature,scene_signature({'jigs':[],'workcell':p}))
        p=apply_state(p,decode_report({'pulse_us':1015,'age_s':.1}));self.assertIn('후진 목표',state_label(p));self.assertEqual(p['linear_stage']['stroke_mm'],1.5)
        self.assertNotEqual(signature,scene_signature({'jigs':[],'workcell':p}))
    def test_stream_reconnects_after_eof_and_close_reaps_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            counter=Path(folder)/'attempt'
            script=f'''import time,json
from pathlib import Path
p=Path({str(counter)!r});first=not p.exists();p.touch()
for i in range(1 if first else 100):
 print('SO101_LINEAR_STATE='+json.dumps({{'pulse_us':2000 if first else 1015,'age_s':.01}}),flush=True)
 time.sleep(.25)
'''
            with patch('so101_teach.linear_state.stream_command',return_value=([sys.executable,'-u','-'],script)) as command:
                stream=StateStream({},{});stream.start()
                try:
                    self.wait_state(stream,lambda s:s.get('pulse_us')==2000)
                    self.wait_state(stream,lambda s:s.get('connected') is False)
                    self.wait_state(stream,lambda s:s.get('pulse_us')==1015)
                    process=stream.process;self.assertEqual(command.call_count,2)
                finally:stream.close();stream.join(2)
                self.assertFalse(stream.is_alive());self.assertIsNotNone(process.poll())
    def test_silent_transport_times_out_and_can_close_during_retry(self):
        script="import time\ntime.sleep(10)\n"
        with patch('so101_teach.linear_state.stream_command',return_value=([sys.executable,'-u','-'],script)),patch('so101_teach.linear_state.STARTUP_TIMEOUT',.15):
            stream=StateStream({},{});stream.start()
            try:self.assertIn('시간 초과',self.wait_state(stream,lambda s:s.get('connected') is False)['reason'])
            finally:stream.close();stream.join(2)
            self.assertFalse(stream.is_alive());self.assertIsNone(stream.process)
