"""Gripper endpoint calibration and lossless body calibration resume."""
import unittest,tempfile,time
from pathlib import Path
from copy import deepcopy
from dataclasses import replace,asdict
from unittest.mock import Mock,patch
from so101_teach.domain import JOINTS,read_json,atomic_json
from tests.fixtures import load_profile
from so101_teach.angle_mapping import AngleMapping
from so101_teach.calibration import CalibrationWorker,validate_progress
from so101_teach.communication import CommunicationCancelled
from tests.test_angle_calibration import points
from tests import test_angle_calibration as angle_tests
from tests import test_configuration as config_tests
from tests import test_motion as motion_tests
from tests import test_ui as ui_tests


def mixed(cal,low=2047,high=2900):
    motors={**cal.motors,'gripper':replace(cal.motors['gripper'],low=low,high=high)}
    doc=points(cal);doc['joints']['gripper']={'kind':'gripper_range','zero':{'ticks':2047,'degrees':-90},'closed':{'ticks':low},'open':{'ticks':high}}
    return doc,motors


class GripperMappingTests(unittest.TestCase):
    def test_anchor_endpoints_and_tick_roundtrip(self):
        _,cal,_=load_profile();doc,motors=mixed(cal);m=AngleMapping(doc,cal.sha256,motors)
        self.assertEqual(m.degrees('gripper',2047),-90)
        for tick in (2047,2300,2900):self.assertAlmostEqual(m.ticks('gripper',m.degrees('gripper',tick)),tick)
        for key,tick in [('closed',2048),('open',2047)]:
            bad=deepcopy(doc);bad['joints']['gripper'][key]['ticks']=tick
            with self.assertRaises(ValueError):AngleMapping(bad,cal.sha256,motors)


class GripperWorkerTests(unittest.TestCase):
    setUp=angle_tests.WorkerAngleTests.setUp
    cleanup=angle_tests.WorkerAngleTests.cleanup
    wait=angle_tests.WorkerAngleTests.wait
    notice_for=angle_tests.WorkerAngleTests.notice_for
    prepare=angle_tests.WorkerAngleTests.prepare
    def test_endpoint_rejection_and_closed_equals_anchor(self):
        w=self.worker;w.start();self.wait(lambda:w.state=='MIDPOINT');self.notice_for(('zero','gripper'))
        self.assertEqual(w.points['gripper']['zero']['degrees'],-90)
        self.notice_for(('grip_endpoint','closed'));before=deepcopy(w.points)
        self.assertIn('각도 대신',self.notice_for(('record','gripper','negative',-90)))
        self.bus.positions['gripper']=2000;self.assertIn('기록하지',self.notice_for(('grip_endpoint','open')))
        self.bus.positions['gripper']=2200;self.assertIn('기록하지',self.notice_for(('grip_endpoint','closed')))
        self.assertEqual(w.points,before)
        self.notice_for(('grip_endpoint','open'));self.assertEqual(w.points['gripper']['open']['ticks'],2200)
    def test_checkpoint_cancel_resume_preserves_body_without_rezero(self):
        progress=Path(self.tmp.name)/'progress.json';w=self.worker;w.progress_path=progress
        self.prepare();self.wait(lambda:progress.exists() and bool(read_json(progress)['mins']))
        before=deepcopy(w.points);self.cleanup();self.assertEqual(self.bus.cal,self.previous)
        saved=read_json(progress);self.assertEqual(saved['points'],before)
        self.bus.homing_calls.clear();writes=Mock(wraps=self.bus.write_calibration);self.bus.write_calibration=writes
        w=self.worker=CalibrationWorker('fake',self.cal,self.dest,bus_factory=lambda:self.bus,progress_path=progress,resume=saved)
        w.start();self.wait(lambda:w.state=='ANGLES');self.assertEqual(w.points,saved['points']);self.assertEqual(self.bus.cal,validate_progress(saved,'fake'));writes.assert_called_once()
        body={n:deepcopy(w.points[n]) for n in JOINTS[:-1]}
        self.notice_for(('zero','gripper'));self.assertEqual(self.bus.homing_calls,[['gripper']]);self.assertEqual({n:w.points[n] for n in body},body)
        self.notice_for(('grip_endpoint','closed'));self.bus.positions['gripper']=2600;self.notice_for(('grip_endpoint','open'))
        w.commands.put('range')
        self.wait(lambda:w.state=='RANGE');w.commands.put('review');self.wait(lambda:w.state=='VERIFY');w.commands.put('save');w.thread.join(3)
        self.assertTrue(w.saved);self.assertFalse(progress.exists());self.assertEqual(read_json(self.dest)['gripper']['range_min'],2047)
        self.assertFalse(any(c.args[0] in ('Goal_Position','Goal_Velocity') or c.args[0]=='Torque_Enable' and c.args[2]!=0 for c in self.raw_write.call_args_list))
    def test_matching_resume_does_not_rewrite_zero_and_wrong_port_never_connects(self):
        w=self.worker;w.progress_path=Path(self.tmp.name)/'progress.json';self.prepare();self.wait(lambda:w.progress_path.exists());doc=read_json(w.progress_path);self.cleanup()
        self.bus.cal=validate_progress(doc,'fake');writer=Mock(wraps=self.bus.write_calibration);self.bus.write_calibration=writer
        w=self.worker=CalibrationWorker('fake',self.cal,self.dest,bus_factory=lambda:self.bus,resume=doc);w.start();self.wait(lambda:w.state=='ANGLES');writer.assert_not_called();self.cleanup()
        factory=Mock();w=self.worker=CalibrationWorker('different-port',self.cal,self.dest,bus_factory=factory,resume=doc);w.run();factory.assert_not_called();self.assertFalse(self.dest.exists())
    def test_checkpoint_cancellation_is_not_reported_as_disk_failure(self):
        self.worker.progress_path=Path(self.tmp.name)/'progress.json'
        self.bus.read_calibration=Mock(side_effect=CommunicationCancelled('stop'))
        with self.assertRaises(CommunicationCancelled):self.worker.checkpoint(self.bus)


class GripperFollowTests(unittest.TestCase):
    setUp=motion_tests.MotionTests.setUp
    snapshot=motion_tests.MotionTests.snapshot
    def test_two_calibrated_grippers_follow_endpoint_ratio_not_angle(self):
        _,leader,_=load_profile();ld,lmotors=mixed(leader,2047,2800);fd,fmotors=mixed(self.cal,1500,3000)
        leader.motors=lmotors;leader.angle_mapping=AngleMapping(ld,leader.sha256,lmotors)
        self.cal.motors=fmotors;self.cal.angle_mapping=AngleMapping(fd,self.cal.sha256,fmotors)
        self.s.arm(self.bus,self.snapshot());goal={**self.ref.middle,'gripper':2423}
        self.s.leader_calibration=leader;self.s.leader_provider=lambda:replace(self.snapshot(),role='leader',ticks=goal.copy());self.s.request('follow')
        for _ in range(500):self.clock+=.02;self.s.on_snapshot(self.bus,self.snapshot())
        expected=round(1500+(2423-2047)/(2800-2047)*1500)
        self.assertEqual(self.s.last_goals['gripper'],expected);self.assertNotEqual(expected,2423)


class GripperUITests(unittest.TestCase):
    setUp=ui_tests.UITests.setUp
    tearDown=ui_tests.UITests.tearDown
    def test_gripper_controls_and_model_use_endpoint_reference(self):
        s=self.app.settings;p=s.calibration_panel;s.vars['cal_joint'].set('집게 벌림');p.select_combo();p.state_changed('ANGLES')
        self.assertEqual(p.zero.cget('text').replace('\n',' '),'집게 −90° 기준 기록');self.assertEqual(str(p.negative_input.cget('state')),'readonly')
        p.show('zero');spec,caption=p.spec();self.assertAlmostEqual(spec[0][5],0);self.assertIn('-90',caption)
        p.show('positive');self.assertIsNone(p.spec()[0])
        with patch.object(s,'cal_command') as command:
            p.record('negative');command.assert_called_once_with(('grip_endpoint','closed'))
        p.state_changed('VERIFY');self.assertTrue(p.save.instate(['!disabled']));self.assertNotIn('cal_verify',s.vars)
    def test_pending_body_records_display_without_motor_access(self):
        s=self.app.settings;p=s.calibration_panel;bus,_=config_tests.ConfigurationTests.calibration_bus(self)
        doc={'schema':1,'port':'/dev/test-leader','firmware_calibration':{n:asdict(v) for n,v in bus.cal.items()},'points':{n:v for n,v in points(self.app.calibration)['joints'].items() if n!='gripper'},'middle':dict.fromkeys(JOINTS[:-1],2047),'offsets':{n:bus.cal[n].homing_offset for n in JOINTS[:-1]},'mins':{},'maxs':{}}
        atomic_json(self.data/'calibration_progress/leader.json',doc);s.vars['leader_port'].set(doc['port']);s.vars['cal_role'].set('리더')
        with patch('so101_teach.calibration.CalibrationWorker') as worker:
            p.sync_role();worker.assert_not_called()
        self.assertEqual(p.last_sample['points'],doc['points']);self.assertEqual(p.start.cget('text'),'이어서 보정');self.assertEqual(p.progress.get(),'기준 5/6 · 완료 5/6')
