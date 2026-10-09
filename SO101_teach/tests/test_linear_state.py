import json
import subprocess
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from so101_teach.linear_state import apply_state,decode_state,read_state,state_label


class LinearStateTests(unittest.TestCase):
    def setUp(self):
        self.placement={'linear_stage':{'stroke_mm':100.,'carriage_home_x_mm':-45.65,
                         'endpoint_reference':{'forward':{'commanded_mm':100.},'retracted':{'commanded_mm':1.5}}}}
        self.connection={'host':'192.0.2.10','user':'ubuntu','port':22,'identity_file':'',
                         'app_dir':'/home/ubuntu/OpenCV_teach','python':'python3'}
        self.source={'environment_script':'/home/ubuntu/project/arm3_ros_env.bash'}

    def test_direction_shortcuts_use_100_and_1_point_5_mm_as_targets_not_feedback(self):
        before=deepcopy(self.placement)
        for pulse,mm,label in ((2000,100.,'전진'),(1015,1.5,'후진'),(1500,50.,'50mm')):
            state=decode_state(pulse);value=apply_state(self.placement,state)
            self.assertEqual(value['linear_stage']['stroke_mm'],mm)
            self.assertFalse(value['linear_stage']['startup_state']['measured'])
            self.assertIn(label,state_label(value));self.assertIn('위치 미측정',state_label(value))
        self.assertEqual(self.placement,before)

    def test_unknown_and_timeout_keep_reference_explicitly_unverified(self):
        for state in (decode_state(-1),decode_state(None),{'known':False,'reason':'시간 초과'}):
            value=apply_state(self.placement,state)
            self.assertEqual(value['linear_stage']['stroke_mm'],100.)
            self.assertIn('상태 미확인',state_label(value));self.assertIn('기준 배치',state_label(value))
        for bad in (0,999,2001,1015.0,True,'1015'):
            with self.assertRaises(ValueError):decode_state(bad)

    def test_query_only_subscribes_using_project_environment(self):
        result=SimpleNamespace(returncode=0,stdout='SO101_LINEAR_STATE={"pulse_us":1015}\n',stderr='')
        with patch('so101_teach.linear_state.subprocess.run',return_value=result) as run:
            value=read_state(self.connection,self.source)
        args=run.call_args.args[0];script=run.call_args.kwargs['input']
        self.assertIn('source /home/ubuntu/project/arm3_ros_env.bash',args[-1])
        self.assertIn('create_subscription',script)
        self.assertNotIn('create_publisher',script);self.assertNotIn('lgpio',script)
        self.assertNotIn('linear_r',args[-1]);self.assertNotIn('linear_f',args[-1])
        self.assertEqual(value['commanded_mm'],1.5)

    def test_query_failure_is_not_a_retracted_position(self):
        with patch('so101_teach.linear_state.subprocess.run',side_effect=subprocess.TimeoutExpired('ssh',10)):
            with self.assertRaisesRegex(ValueError,'시간 초과'):read_state(self.connection,self.source)
        with patch('so101_teach.linear_state.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='bad')):
            with self.assertRaisesRegex(ValueError,'응답 오류'):read_state(self.connection,self.source)

    def test_camera_endpoints_and_intermediate_target_preserve_carriage_mapping(self):
        from so101_teach.workcell_scene import carriage_position_mm
        stage=self.placement['linear_stage']
        stage['endpoint_reference']['forward']['carriage_x_mm']=54.
        stage['endpoint_reference']['retracted']['carriage_x_mm']=-42.
        for target,expected in ((100.,54.),(1.5,-42.),(50.75,6.)):
            stage['stroke_mm']=target
            self.assertAlmostEqual(carriage_position_mm(stage),expected)


if __name__=='__main__':unittest.main()
