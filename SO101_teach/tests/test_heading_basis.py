import unittest
from copy import deepcopy
import numpy as np
from tests.fixtures import load_profile
from so101_teach.geometry import Kinematics,corrected_step,jig_delta
from so101_teach.jig_heading import pose_in_reference_basis,mesh_yaw_offset
from so101_teach.jig_comparison import comparison_groups

class HeadingBasisTests(unittest.TestCase):
    def test_old_and_new_frame_references_do_not_create_a_false_half_turn_motion(self):
        profile,cal,ref=load_profile();kin=Kinematics(ref,tcp=profile.get('tcp'));sha='a'*64
        old={'pose':[180,-130,209.4],'symmetry_deg':360,'stl_sha256':sha}
        new={'pose':[180,-130,29.4],'symmetry_deg':360,'stl_sha256':sha,'mesh_yaw_offset_deg':180.}
        for saved,current in ((old,new),(new,old),(new,new)):
            step={'id':'s','name':'test','ticks':ref.middle.copy(),'jig_id':'carrier','jig_reference':deepcopy(saved)};before=deepcopy(step)
            result=corrected_step(kin,step,current,sha);self.assertEqual(result['ticks'],ref.middle);self.assertEqual(result['position_error_mm'],0);self.assertEqual(step,before)
            report=comparison_groups([step],{'carrier':{'name':'carrier'}},{'carrier':current})[0]
            np.testing.assert_allclose(report['delta'],[0,0,0],atol=1e-9)
        moved=deepcopy(new);moved['pose'][2]+=180
        self.assertAlmostEqual(abs(jig_delta(new['pose'],pose_in_reference_basis(new,moved),360)[2]-new['pose'][2]),180.)
        legacy=deepcopy(old);legacy['symmetry_deg']=180;legacy['stl_sha256']='b'*64
        step={'ticks':ref.middle,'jig_id':'carrier','jig_reference':legacy}
        with self.assertRaises(ValueError):corrected_step(kin,step,new,sha)
        for invalid in (90,float('nan'),True):
            with self.assertRaises(ValueError):mesh_yaw_offset({'mesh_yaw_offset_deg':invalid})
