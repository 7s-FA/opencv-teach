from tests.fixtures import DATA
import unittest,tempfile,json,time
from copy import deepcopy
from pathlib import Path
from collections import Counter
from types import SimpleNamespace
from unittest.mock import Mock,patch
import numpy as np
from so101_teach.domain import ROOT,JOINTS,atomic_json,read_json,EpisodeStore
from tests.fixtures import load_profile
from so101_teach.configuration import model_tcp,tcp_matrix,JigCatalog,ProfileLibrary
from so101_teach.geometry import Kinematics
from so101_teach.playback import Playback
from so101_teach.telemetry import TemperatureTracker
from so101_teach.preview import configured_scene
from so101_teach.calibration import CalibrationWorker,install_calibration_gate
from so101_teach.devices import ReadOnlyViolation
from so101_teach.demo import DemoSession

class ConfigurationTests(unittest.TestCase):
    def setUp(self):self.profile,self.cal,self.ref=load_profile()
    def test_model_tcp_matches_assembled_fixed_finger_not_frame_origin(self):
        tcp=model_tcp();np.testing.assert_allclose(tcp['xyz_mm'],[1.7734096759,.6026567344,6.5496538999],atol=1e-8)
        frame=Kinematics(self.ref).fk(self.ref.middle);actual=Kinematics(self.ref,tcp=tcp).fk(self.ref.middle)
        np.testing.assert_allclose(actual,frame@tcp_matrix(tcp));self.assertFalse(tcp['verified'])
    def test_invalid_tcp_never_produces_transform(self):
        with self.assertRaises(ValueError):tcp_matrix({'xyz_mm':[float('nan'),0,0],'rpy_deg':[0,0,0]})
    def test_jig_catalog_copies_assets_and_keeps_separate_roi(self):
        with tempfile.TemporaryDirectory() as d:
            c=JigCatalog(d);one=c.save({**c.items['pallet'],'roi':[.1,.2,.5,.7]});two=c.duplicate('pallet');two['roi']=[.5,.2,.9,.7];c.save(two)
            again=JigCatalog(d);self.assertNotEqual(again.items['pallet']['roi'],again.items[two['id']]['roi']);self.assertTrue(Path(one['stl']).is_file())
            with self.assertRaises(ValueError):again.remove('pallet',{'pallet'})
            again.remove(two['id']);self.assertEqual(len(again.items),1)
    def test_scene_loads_registered_meshes_and_tcp_without_motor_access(self):
        import mujoco
        with tempfile.TemporaryDirectory() as d:
            c=JigCatalog(d);m=c.mesh('pallet');item={**c.items['pallet'],'low_mm':m['low_mm'],'size_mm':m['size_mm']}
            xml=configured_scene([item],model_tcp());model=mujoco.MjModel.from_xml_string(xml)
            self.assertGreaterEqual(model.body('registered_jig_0').mocapid[0],0);self.assertGreaterEqual(model.site('active_tcp').id,0)
    def test_preview_seek_pause_speed_and_finish(self):
        player=Playback();player.begin({'x':0},[{'x':100},{'x':200}],now=0)
        self.assertEqual(player.sample(1)[0]['x'],50);player.paused=True;self.assertEqual(player.sample(2)[0]['x'],50)
        player.paused=False;player.speed=2.;self.assertEqual(player.sample(2.5)[0]['x'],100)
        player.seek(3);player.last=3;self.assertEqual(player.sample(3)[0]['x'],150)
        self.assertTrue(player.sample(9)[1])
    def test_temperature_requires_five_seconds_and_resets_after_gap(self):
        tracker=TemperatureTracker()
        for t in np.arange(0,5,.5):tracker.observe('wrist',70,t)
        with self.assertRaises(RuntimeError):tracker.observe('wrist',70,5)
        tracker=TemperatureTracker();tracker.observe('wrist',70,0);tracker.observe('wrist',70,3);tracker.observe('wrist',30,3.2);tracker.observe('wrist',70,3.3)
    def test_safety_steps_must_be_fixed_and_equal(self):
        store=EpisodeStore('/unused',self.cal);doc=store.new();a=store.step(self.ref.middle,'안전');b=store.step(self.ref.middle,'안전');a['safe_boundary']='start';b['safe_boundary']='end';doc['steps']=[a,b];store.validate(doc)
        b['ticks']['gripper']+=1
        with self.assertRaises(ValueError):store.validate(doc)
    def test_demo_never_opens_motor_port_and_records_demo_source(self):
        s=DemoSession(self.cal,self.ref.middle)
        with patch('so101_teach.devices.new_bus',side_effect=AssertionError('No USB')):
            s.start()
            try:
                end=time.monotonic()+1
                while s.latest is None and time.monotonic()<end:time.sleep(.01)
                self.assertEqual(s.latest.role,'demo');self.assertTrue(all(h['torque']==0 for h in s.latest.telemetry.values()))
            finally:s.close();s.join(1)
    def test_calibration_gate_blocks_goal_and_torque_on(self):
        port=SimpleNamespace(writePort=Mock());counts=Counter();install_calibration_gate(port,counts)
        def packet(addr,data):
            b=[1,len(data)+3,3,addr,*data];return bytes([255,255,*b,(~sum(b))&255])
        for address,data in [(42,[0,8]),(40,[1])]:
            with self.assertRaises(ReadOnlyViolation):port.writePort(packet(address,data))
        port.writePort(packet(40,[0]));port.writePort(packet(31,[0,0]));self.assertEqual(port.writePort.__name__,'write')
    def calibration_bus(self):
        from lerobot.motors import MotorCalibration
        original={n:MotorCalibration(**row) for n,row in read_json(DATA/'calibration/follower.json').items()}
        bus=SimpleNamespace(motors={},cal=deepcopy(original),off=0,positions=dict.fromkeys(JOINTS,2047),port_handler=SimpleNamespace(closePort=Mock()))
        bus.connect=Mock();bus.read_calibration=lambda:deepcopy(bus.cal);bus.read=lambda reg,n,**kw:0
        bus.write=Mock();bus.disable_torque=lambda:setattr(bus,'off',bus.off+1)
        bus.write_calibration=lambda d:setattr(bus,'cal',deepcopy(d))
        bus.sync_read=lambda *a,**kw:bus.positions.copy()
        bus.homing_calls=[]
        def half(motors):
            bus.homing_calls.append(list(motors))
            for n in motors:bus.cal[n].homing_offset=0;bus.positions[n]=2047
            return dict.fromkeys(motors,0)
        bus.set_half_turn_homings=half;return bus,original
    def test_calibration_preset_commits_and_cancel_restores(self):
        with tempfile.TemporaryDirectory() as d:
            bus,original=self.calibration_bus();worker=CalibrationWorker('fake',self.cal,Path(d)/'new.json',preset=DATA/'calibration/follower.json',bus_factory=lambda:bus);worker.run()
            self.assertTrue(worker.saved);self.assertEqual(bus.cal,original);self.assertTrue((Path(d)/'new.json').exists());self.assertGreater(bus.off,0)
            bus,original=self.calibration_bus();worker=CalibrationWorker('fake',self.cal,Path(d)/'cancelled.json',bus_factory=lambda:bus);worker.start()
            deadline=time.monotonic()+1
            while worker.state!='MIDPOINT' and time.monotonic()<deadline:time.sleep(.01)
            worker.commands.put(('zero','shoulder_pan'))
            while worker.state!='ANGLES' and time.monotonic()<deadline:time.sleep(.01)
            worker.close();worker.thread.join(1);self.assertFalse(worker.saved);self.assertEqual(bus.cal,original);self.assertFalse((Path(d)/'cancelled.json').exists())

    def test_manual_rectangle_without_stl_has_stable_geometry_and_outline_scene(self):
        import mujoco
        with tempfile.TemporaryDirectory() as d:
            c=JigCatalog(d);item=c.save({'id':None,'name':'220 판','stl':None,'unit':'mm','shape':'rectangle','method':'combined','manual_size_mm':[220,148],'manual_rim_mm':4.8,'roi':None,'yaw_deg':0})
            m=c.mesh(item['id']);self.assertEqual(m['size_mm'],[220.,148.,4.8]);self.assertEqual(m['holes'],[])
            scene=configured_scene([{**item,'low_mm':m['low_mm'],'size_mm':m['size_mm']}]);model=mujoco.MjModel.from_xml_string(scene);self.assertGreaterEqual(model.geom('registered_jig_0_edge3').id,0)
    def test_calibration_measured_range_commits_actual_endpoints(self):
        with tempfile.TemporaryDirectory() as d:
            bus,_=self.calibration_bus();w=CalibrationWorker('fake',self.cal,Path(d)/'measured.json',bus_factory=lambda:bus);w.start()
            def wait(condition):
                end=time.monotonic()+2
                while not condition() and time.monotonic()<end:time.sleep(.005)
                self.assertTrue(condition(),w.state)
            wait(lambda:w.state=='MIDPOINT')
            for n in JOINTS:
                w.commands.put(('zero',n));wait(lambda:n in w.middle)
            for side,deg,tick in [('negative',-45,1700),('positive',45,2400)]:
                for n in JOINTS:
                    bus.positions[n]=tick
                    key=('closed' if side=='negative' else 'open') if n=='gripper' else side
                    w.commands.put(('grip_endpoint',key) if n=='gripper' else ('record',n,side,deg));wait(lambda:key in w.points[n])
            w.commands.put('range');wait(lambda:w.state=='RANGE')
            bus.positions=dict.fromkeys(JOINTS,1500);time.sleep(.08);bus.positions=dict.fromkeys(JOINTS,2600);time.sleep(.08)
            w.commands.put('review');wait(lambda:w.state=='VERIFY');w.commands.put('save');w.thread.join(2)
            self.assertTrue(w.saved);result=read_json(Path(d)/'measured.json');self.assertTrue(all(v['range_min']==1500 and v['range_max']==2600 for n,v in result.items() if n!='gripper'));self.assertEqual((result['gripper']['range_min'],result['gripper']['range_max']),(1700,2400))

    def test_failed_calibration_write_restores_old_values_and_does_not_save_json(self):
        with tempfile.TemporaryDirectory() as d:
            bus,previous=self.calibration_bus();calls=[]
            def write(values):
                calls.append(1)
                if len(calls)==1:
                    bus.cal['wrist_roll'].homing_offset=0;raise IOError('injected write failure')
                bus.cal=deepcopy(values)
            bus.write_calibration=write;w=CalibrationWorker('fake',self.cal,Path(d)/'failed.json',preset=DATA/'calibration/follower.json',bus_factory=lambda:bus);w.run()
            self.assertFalse(w.saved);self.assertEqual(bus.cal,previous);self.assertFalse((Path(d)/'failed.json').exists())
