import json,math,tempfile,time,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
from so101_teach.domain import *
from tests.fixtures import load_profile
from so101_teach.devices import install_read_gate,ReadOnlyViolation,ReadOnlySession,decode_telemetry
from so101_teach.geometry import Kinematics,transform_jig_pose

class CoreTests(unittest.TestCase):
    def setUp(self):self.profile,self.cal,self.ref=load_profile()
    def test_all_six_servos_roundtrip_raw_ticks_including_gripper(self):
        for f in np.linspace(0,1,81):
            ticks={n:round(m.low+f*(m.high-m.low)) for n,m in self.cal.motors.items()}
            self.assertEqual(self.ref.ticks_from_angles(self.ref.angles(ticks)),ticks)
        a=self.ref.middle.copy();b=a.copy();b['gripper']+=1
        self.assertAlmostEqual(self.ref.angles(b)[5]-self.ref.angles(a)[5],2*math.pi/4096)
    def test_recorded_wrist_frame_distinguishes_command_from_internal_target(self):
        raw=bytes.fromhex('0100f007000001006400000000000000e807328020007a20000001e3070000')
        ticks,health=decode_telemetry(raw)
        self.assertEqual(ticks,2024);self.assertEqual(health['goal_ticks'],2032)
        self.assertEqual(health['internal_goal_ticks'],2019)
        self.assertEqual(health['velocity_signed_raw'],-50)
        self.assertEqual(health['torque'],1);self.assertEqual(health['status'],0)
    def test_invalid_types_missing_ticks_and_limits_are_rejected(self):
        for value in [True,2047.,-1,4096,float('nan')]:
            with self.assertRaises(ValueError):self.cal.ticks({**self.ref.middle,'gripper':value})
        with self.assertRaises(ValueError):self.cal.ticks({'gripper':2047})
        with self.assertRaises(ValueError):self.ref.ticks_from_angles([float('nan')]*6)
    def test_calibration_identity_is_required_for_model_reference(self):
        p=deepcopy(self.profile['model_reference']);p['calibration_sha256']='other'
        with self.assertRaises(ValueError):ModelReference(self.cal,p)
    def test_read_gate_blocks_every_write_reset_and_action_on_wire(self):
        port=SimpleNamespace(writePort=Mock(return_value=8));counts={1:0,2:0,0x82:0};original=port.writePort
        install_read_gate(port,counts)
        for op in [3,4,5,6,8,0x83,0x92,0x93]:
            with self.assertRaises(ReadOnlyViolation):port.writePort(bytes([255,255,1,2,op,0]))
        original.assert_not_called()
        for op in [1,2,0x82]:port.writePort(bytes([255,255,1,2,op,(~(1+2+op))&255]))
        with self.assertRaises(ReadOnlyViolation):port.writePort(bytes([255,255,1,2,2,250])*2)
        self.assertEqual(original.call_count,3)
    def test_episode_preserves_gripper_ticks_and_rejects_other_zero(self):
        with tempfile.TemporaryDirectory() as d:
            store=EpisodeStore(d,self.cal);e=store.new('실물 저장');ticks=self.ref.middle.copy();ticks['gripper']=888
            e['steps']=[store.step(ticks,'바닥 접촉')];p=store.save(e);self.assertEqual(store.load(p)['steps'][0]['ticks']['gripper'],888)
            bad=deepcopy(e);bad['calibration_sha256']='wrong'
            with self.assertRaises(ValueError):store.save(bad)
            self.assertEqual(store.load(p),e)
    def test_capture_rejects_stale_leader_and_mismatched_samples(self):
        store=EpisodeStore('/unused',self.cal)
        for role,at,match in [('follower',time.monotonic()-1,True),('leader',time.monotonic(),True),('follower',time.monotonic(),False)]:
            snap=Snapshot(role,self.ref.middle,{},at,time.time(),self.cal.sha256,match,'fake')
            with self.assertRaises(ValueError):store.capture(snap,'capture')
    def test_independent_urdf_fk_matches_mujoco_at_multiple_actual_tick_poses(self):
        import mujoco
        from so101_teach.preview import SCENE
        m=mujoco.MjModel.from_xml_path(str(SCENE));d=mujoco.MjData(m);kin=Kinematics(self.ref)
        poses=[self.ref.middle,read_json(ROOT/'verification/floor-contact/snapshot.json')['ticks']]
        for f in (.25,.5,.75):poses.append({n:round(a.low+f*(a.high-a.low)) for n,a in self.cal.motors.items()})
        for pose in poses:
            for n,a in zip(JOINTS,self.ref.angles(pose)):d.qpos[m.joint(n).qposadr[0]]=a
            mujoco.mj_forward(m,d);fk=kin.fk(pose)
            np.testing.assert_allclose(fk[:3,3],d.body('gripper_frame_link').xpos*1000,atol=.003)
            np.testing.assert_allclose(fk[:3,:3],d.body('gripper_frame_link').xmat.reshape(3,3),atol=1e-5)
    def test_jig_transform_retains_height_and_contact_side(self):
        tcp=np.eye(4);tcp[:3,3]=[110,220,38]
        q=transform_jig_pose(tcp,[100,200,0],[120,180,90])
        np.testing.assert_allclose(q[:3,3],[100,190,38]);np.testing.assert_allclose(q[:3,0],[0,1,0],atol=1e-10)
        np.testing.assert_allclose(transform_jig_pose(q,[120,180,90],[100,200,0]),tcp,atol=1e-10)
    def test_read_error_closes_without_sdk_disconnect_or_torque_write(self):
        bus=SimpleNamespace(connect=Mock(),read_calibration=Mock(side_effect=IOError('read failed')),port_handler=SimpleNamespace(closePort=Mock()))
        s=ReadOnlySession('fake',self.cal,bus_factory=lambda *args:bus);s.run()
        self.assertIn('read failed',s.error);bus.port_handler.closePort.assert_called_once();self.assertFalse(s.running)

if __name__=='__main__':unittest.main()
