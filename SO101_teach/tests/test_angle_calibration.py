from tests.fixtures import DATA
import unittest,tempfile,shutil,json,math,time,hashlib
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import numpy as np
from so101_teach.domain import ROOT,JOINTS,Calibration,ModelReference,atomic_json,read_json
from tests.fixtures import load_profile
from so101_teach.angle_mapping import AngleMapping,angle_path
from so101_teach.configuration import ProfileLibrary
from so101_teach.calibration import CalibrationWorker
from tests import test_configuration as fixtures

def points(cal):
    return {'schema':1,'calibration_sha256':cal.sha256,'joints':{n:{'negative':{'ticks':max(m.low,1500),'degrees':-40.},'zero':{'ticks':2047,'degrees':0.},'positive':{'ticks':min(m.high,2250),'degrees':25.}} for n,m in cal.motors.items()},'verification':{}}

class AngleTests(unittest.TestCase):
    def setUp(self):
        self.profile,self.old,self.ref=load_profile();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'motor.json'
        shutil.copy2(DATA/'calibration/follower.json',self.path)
        atomic_json(angle_path(self.path),points(self.old));self.cal=Calibration(self.path);self.ref=ModelReference(self.cal,self.profile['model_reference'])
    def test_three_points_and_both_directions_roundtrip_with_asymmetric_slopes(self):
        for n,m in self.cal.motors.items():
            for key,p in self.cal.angle_mapping.document['joints'][n].items():
                self.assertAlmostEqual(self.cal.angle_mapping.degrees(n,p['ticks']),p['degrees'])
            for tick in (m.low,1600,2047,2200,m.high):
                if not m.low<=tick<=m.high:continue
                raw=self.ref.middle.copy();raw[n]=tick
                self.assertEqual(self.ref.ticks_from_angles(self.ref.angles(raw)),raw)
            self.assertEqual(self.ref.angle_limits(n),sorted([self.ref.joint_angle(n,m.low),self.ref.joint_angle(n,m.high)]))
    def test_reversed_tick_sides_are_rejected(self):
        doc=points(self.old);n='wrist_roll';doc['joints'][n]['negative']['ticks']=2700;doc['joints'][n]['positive']['ticks']=1700
        with self.assertRaisesRegex(ValueError,'음수 틱'):AngleMapping(doc,self.old.sha256,self.old.motors)
    def test_invalid_hash_nonmonotonic_and_missing_points_rejected(self):
        for change in (lambda d:d.update(calibration_sha256='wrong'),lambda d:d['joints']['gripper'].pop('positive'),lambda d:d['joints']['gripper']['positive'].update(ticks=1800),lambda d:d['joints']['gripper']['negative'].update(degrees=float('nan'))):
            d=points(self.old);change(d)
            with self.assertRaises(ValueError):AngleMapping(d,self.old.sha256,self.old.motors)
    def test_trims_cannot_be_double_applied_and_extrapolation_is_explicit(self):
        with self.assertRaises(ValueError):self.ref.set_trims({**dict.fromkeys(JOINTS,0),'wrist_roll':1})
        self.assertTrue(self.cal.angle_mapping.extrapolated('wrist_roll',3000));self.assertFalse(self.cal.angle_mapping.extrapolated('wrist_roll',2100))
    def test_profile_copy_preserves_bytes_and_sidecar_without_overwriting_other_fit(self):
        lib=ProfileLibrary(Path(self.tmp.name)/'profiles');one,cal=lib.copy_calibration(self.path)
        self.assertEqual(cal.sha256,self.old.sha256);self.assertEqual((lib.root/one).read_bytes(),self.path.read_bytes());self.assertIsNotNone(cal.angle_mapping)
        d=points(self.old);d['joints']['wrist_roll']['positive']['degrees']=30;atomic_json(angle_path(self.path),d)
        two,newcal=lib.copy_calibration(self.path);self.assertNotEqual(one,two);self.assertEqual(Calibration(lib.root/one).angle_mapping.degrees('wrist_roll',2250),25)
    def test_ik_uses_measured_mapping_and_preserves_gripper_raw_ticks(self):
        from so101_teach.geometry import Kinematics,corrected_step,transform_jig_pose
        kin=Kinematics(self.ref);ticks=read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks']
        ref={'pose':[228,-138,67],'symmetry_deg':90,'stl_sha256':'a'*64};step={'name':'test','ticks':ticks,'jig_id':'pallet','jig_reference':ref}
        current={**ref,'pose':[229,-138,67.5]};out=corrected_step(kin,step,current,'a'*64)
        target=transform_jig_pose(kin.fk(ticks),ref['pose'],current['pose'])
        self.assertLess(np.linalg.norm(kin.fk(out['ticks'])[:3,3]-target[:3,3]),3);self.assertEqual(out['ticks']['gripper'],ticks['gripper'])

    def test_linked_profile_does_not_silently_fall_back_when_angle_file_disappears(self):
        lib=ProfileLibrary(Path(self.tmp.name)/'profiles');profile=lib.reference_for(self.profile,self.cal)
        self.assertEqual(profile['model_reference']['angle_mapping_sha256'],self.cal.angle_mapping.sha256)
        angle_path(self.path).unlink()
        with self.assertRaisesRegex(ValueError,'3점 각도 파일'):ModelReference(Calibration(self.path),profile['model_reference'])

class WorkerAngleTests(unittest.TestCase):
    def setUp(self):
        _,self.cal,_=load_profile();self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bus,self.previous=fixtures.ConfigurationTests.calibration_bus(self);self.raw_write=self.bus.write;self.dest=Path(self.tmp.name)/'new.json'
        self.worker=CalibrationWorker('fake',self.cal,self.dest,bus_factory=lambda:self.bus)
        self.addCleanup(self.cleanup)
    def cleanup(self):
        self.worker.close()
        if hasattr(self.worker,'thread'):self.worker.thread.join(3)
    def wait(self,condition):
        end=time.monotonic()+2
        while not condition() and time.monotonic()<end:time.sleep(.005)
        self.assertTrue(condition(),self.worker.state)
    def prepare(self):
        w=self.worker;w.start();self.wait(lambda:w.state=='MIDPOINT')
        for n in JOINTS:
            w.commands.put(('zero',n));self.wait(lambda:n in w.middle)
        for side,deg,tick in [('negative',-45,1500),('positive',30,2600)]:
            for n in JOINTS:
                self.bus.positions[n]=tick
                key=('closed' if side=='negative' else 'open') if n=='gripper' else side
                w.commands.put(('grip_endpoint',key) if n=='gripper' else ('record',n,side,deg));self.wait(lambda:key in w.points[n])
        w.commands.put('range');self.wait(lambda:w.state=='RANGE');w.commands.put('review');self.wait(lambda:w.state=='VERIFY')
    def test_complete_points_verification_and_atomic_package(self):
        self.prepare();w=self.worker;self.bus.positions['wrist_roll']=2324;w.commands.put(('verify','wrist_roll',15));self.wait(lambda:bool(w.verification.get('wrist_roll')))
        w.commands.put('save');w.thread.join(2);self.assertTrue(w.saved);self.assertEqual(w.state,'DONE')
        cal=Calibration(self.dest);self.assertIsNotNone(cal.angle_mapping);self.assertEqual(cal.angle_mapping.document['verification']['wrist_roll'][0]['degrees'],15)
        self.assertTrue(all(v.range_min==1500 and v.range_max==2600 for v in self.bus.cal.values()))
        self.assertFalse(any(c.args[0] in ('Goal_Position','Goal_Velocity') or c.args[0]=='Torque_Enable' and c.args[2]!=0 for c in self.raw_write.call_args_list))
    def test_cancel_after_points_restores_previous_firmware_and_leaves_no_package(self):
        self.prepare();self.cleanup();self.assertEqual(self.bus.cal,self.previous);self.assertFalse(self.dest.exists());self.assertFalse(angle_path(self.dest).exists())
    def test_angle_file_write_failure_restores_firmware_and_removes_partial_files(self):
        self.prepare();original=atomic_json
        def fail(path,value):
            if Path(path)==self.dest:raise OSError('disk full')
            return original(path,value)
        with patch('so101_teach.calibration.atomic_json',side_effect=fail):
            self.worker.commands.put('save');self.worker.thread.join(2)
        self.assertFalse(self.worker.saved);self.assertEqual(self.bus.cal,self.previous);self.assertFalse(self.dest.exists());self.assertFalse(angle_path(self.dest).exists())
    def test_missing_points_keeps_worker_open_without_partial_save(self):
        w=self.worker;w.start();self.wait(lambda:w.state=='MIDPOINT')
        w.commands.put(('zero',JOINTS[0]));self.wait(lambda:w.state=='ANGLES');w.commands.put('range');time.sleep(.08)
        self.assertEqual(w.state,'ANGLES');self.assertFalse(self.dest.exists());self.assertTrue(w.running)
    def test_import_existing_three_point_package_keeps_mapping(self):
        source=Path(self.tmp.name)/'source.json'
        shutil.copy2(DATA/'calibration/follower.json',source)
        atomic_json(angle_path(source),points(self.cal))
        self.worker.preset=source;self.worker.start();self.worker.thread.join(3)
        self.assertTrue(self.worker.saved)
        imported=Calibration(self.dest)
        self.assertEqual(imported.angle_mapping.document['joints'],points(self.cal)['joints'])
        self.assertEqual(imported.angle_mapping.document['calibration_sha256'],imported.sha256)
        self.assertFalse(any(c.args[0] in ('Goal_Position','Goal_Velocity') for c in self.raw_write.call_args_list))
    def notice_for(self,command):
        import queue
        while True:
            try:self.worker.events.get_nowait()
            except queue.Empty:break
        self.worker.commands.put(command);end=time.monotonic()+2
        while time.monotonic()<end:
            kind,value=self.worker.events.get(timeout=2)
            if kind=='notice':return value
        self.fail('No command result')
    def test_single_zero_changes_only_selected_motor(self):
        w=self.worker;self.bus.positions=dict.fromkeys(JOINTS,2500);self.bus.positions[JOINTS[0]]=1600
        w.start();self.wait(lambda:w.state=='MIDPOINT');self.notice_for(('zero',JOINTS[0]))
        self.assertEqual(self.bus.homing_calls,[[JOINTS[0]]]);self.assertEqual(w.middle,{JOINTS[0]:2047})
        for n in JOINTS[1:]:
            self.assertEqual(self.bus.cal[n],self.previous[n]);self.assertEqual(self.bus.positions[n],2500)
        self.assertIn('먼저',self.notice_for(('record',JOINTS[1],'negative',-45)))
        self.assertNotIn(JOINTS[1],w.points)
    def test_wrong_side_and_equal_ticks_do_not_replace_recorded_point(self):
        w=self.worker;w.start();self.wait(lambda:w.state=='MIDPOINT');n=JOINTS[0];self.notice_for(('zero',n))
        self.bus.positions[n]=1500;self.notice_for(('record',n,'negative',-45));before=deepcopy(w.points[n])
        for side,deg,tick in [('negative',-45,2200),('negative',-45,2047),('positive',45,1500),('positive',45,2047)]:
            self.bus.positions[n]=tick;self.assertIn('기록하지',self.notice_for(('record',n,side,deg)));self.assertEqual(w.points[n],before)
        self.assertFalse(self.dest.exists())
    def test_rerecord_zero_discards_only_that_joint_angle_points(self):
        self.prepare();w=self.worker;w.commands.put('angles');self.wait(lambda:w.state=='ANGLES');before=deepcopy(w.points);n='wrist_roll'
        self.bus.positions[n]=2300;self.notice_for(('zero',n))
        self.assertEqual(w.points[n],{'zero':{'degrees':0.,'ticks':2047}})
        for other in JOINTS:
            if other!=n:self.assertEqual(w.points[other],before[other])
        self.assertIn('모두',self.notice_for('range'));self.assertEqual(w.state,'ANGLES')
    def test_single_zero_failure_restores_all_previous_firmware(self):
        w=self.worker;w.start();self.wait(lambda:w.state=='MIDPOINT')
        def fail(names):
            self.bus.cal[names[0]].homing_offset=0
            raise OSError('lost readback')
        self.bus.set_half_turn_homings=fail;w.commands.put(('zero','elbow_flex'));w.thread.join(2)
        self.assertFalse(w.running);self.assertEqual(self.bus.cal,self.previous);self.assertFalse(self.dest.exists())
    def test_installed_sdk_scopes_zero_writes_to_one_joint(self):
        from collections import Counter
        from unittest.mock import Mock
        from so101_teach.devices import new_bus
        bus=new_bus('unused',self.cal,Counter());bus.write=Mock();bus.sync_read=Mock(return_value={'elbow_flex':1600})
        result=bus.set_half_turn_homings(['elbow_flex'])
        self.assertEqual(result,{'elbow_flex':-447})
        self.assertTrue(all(call.args[1]=='elbow_flex' for call in bus.write.call_args_list))
        self.assertEqual([call.args[0] for call in bus.write.call_args_list],['Homing_Offset','Min_Position_Limit','Max_Position_Limit','Homing_Offset'])
