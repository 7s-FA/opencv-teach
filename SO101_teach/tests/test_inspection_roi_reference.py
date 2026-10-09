"""Validate CAD reference data without starting a simulator, camera or robot."""
import hashlib
import json
from pathlib import Path
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / 'inspection'


class InspectionROIReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((REFERENCE / 'roi_reference.json').read_text())

    def test_reference_never_enables_unvalidated_inspection(self):
        self.assertIs(self.data['inspection_enabled'], False)
        self.assertIsNone(self.data['live_pixel_rois'])
        self.assertIs(self.data['simulation_offsets_applied_to_real_roi'], False)
        self.assertIsNone(self.data['stations']['linear_assembly']['active_fixture_index'])

    def test_all_recorded_inputs_match_actual_sources(self):
        for name, digest in self.data['sources_sha256'].items():
            with self.subTest(source=name):
                self.assertEqual(hashlib.sha256((ROOT.parent / name).read_bytes()).hexdigest(), digest)

    def test_carrier_centers_follow_actual_assembly_not_sim_tuning(self):
        source=json.loads((ROOT/'assets/carrier_assembly/assembly.json').read_text())
        expected={p['name']:p['translation_mm'] for p in source['parts'] if p['name']!='carrier'}
        rows=self.data['stations']['carrier']['rois']
        self.assertEqual(len(rows),6)
        for row in rows:
            np.testing.assert_allclose(row['fixture_center_xyz_mm'],expected[row['id']],atol=1e-5)
            self.assertEqual(row['fixture_yaw_deg'],90)
            self.assertEqual(row['product'],'A' if row['fixture_center_xyz_mm'][1]<0 else 'B')

    def test_search_margin_is_not_a_pass_fail_tolerance(self):
        self.assertIs(self.data['search_margin_is_acceptance_tolerance'],False)
        for row in self.data['stations']['carrier']['rois']:
            lo=np.asarray(row['footprint_bbox_xy_mm'][:2]);hi=np.asarray(row['footprint_bbox_xy_mm'][2:])
            np.testing.assert_allclose(row['search_roi_xy_mm'],[*list(lo-3),*list(hi+3)],atol=1e-5)

    def test_sphere_has_no_angle_criterion_and_caps_keep_flip(self):
        rows={r['id']:r for r in self.data['stations']['carrier']['rois']}
        self.assertIsNone(rows['a_ball']['expected_yaw_deg'])
        self.assertIs(rows['a_ball']['yaw_observable'],False)
        for key in ('a_cap','b_cap'):
            rotation=np.array(rows[key]['part_transform_local_mm'])[:3,:3]
            self.assertAlmostEqual(np.linalg.det(rotation),1.,places=5)
            self.assertAlmostEqual(rotation[2,2],-1.,places=5)

    def test_linear_centers_and_final_pallet_origin(self):
        self.assertEqual(self.data['stations']['linear_assembly']['fixture_centers_carriage_mm'],[[-40,0,9.4],[40,0,9.4]])
        pallet=self.data['stations']['finished_pallet']
        self.assertEqual(pallet['size_xy_mm'],[70,70]);self.assertEqual(pallet['slot_center_xy_mm'],[0,0])
        self.assertEqual(pallet['slot_count'],1)
        for product in ('A','B'):
            np.testing.assert_allclose(pallet['rois'][product]['search_roi_xy_mm'],[-28,-28,28,28],atol=1e-4)

    def test_stages_require_previous_product_bound_evidence(self):
        order=['empty','housing_seated','insert_added','cap_added']
        for profile in self.data['assembly_profiles'].values():
            self.assertEqual([s['id'] for s in profile['stages']],order)
            for i,stage in enumerate(profile['stages']):
                self.assertEqual(stage['requires_previous_pass'],None if i==0 else order[i-1])
                self.assertIn('product_id',stage['history_binding_keys'])
                self.assertIs(stage['requires_frame_after_stage_start'],True)

    def test_final_A_requires_visible_ball_B_uses_hidden_part_history(self):
        a=self.data['assembly_profiles']['A']['stages'][-1]
        b=self.data['assembly_profiles']['B']['stages'][-1]
        self.assertEqual(a['expected_visible_parts'],['insert','cap'])
        self.assertEqual(a['occluded_parts_requiring_history'],['housing'])
        self.assertEqual(b['expected_visible_parts'],['cap'])
        self.assertEqual(b['occluded_parts_requiring_history'],['housing','insert'])

    def test_B_seating_agrees_with_RL_and_A_is_not_given_B_gap(self):
        rl=json.loads((ROOT.parent/'SO101_rl/outputs/assembly_current_12/scene.json').read_text())['assembly']
        b=self.data['assembly_profiles']['B'];a=self.data['assembly_profiles']['A']
        for ours,theirs in [('housing_seat_z_mm','housing_seating_z_mm'),('insert_bottom_relative_to_housing_mm','tpu_bottom_in_housing_mm'),('cap_bottom_relative_to_housing_mm','cap_bottom_in_housing_mm')]:
            self.assertAlmostEqual(b[ours],rl[theirs],places=4)
        self.assertAlmostEqual(b['cap_bottom_minus_housing_rim_mm'],1.6,places=3)
        self.assertLess(a['cap_bottom_minus_housing_rim_mm'],0)
        self.assertIs(b['sim_only_candidate_tolerances']['apply_to_real_pass_fail'],False)

    def test_masks_match_visible_geometry_metadata(self):
        for profile in self.data['assembly_profiles'].values():
            for stage in profile['stages']:
                labels=cv2.imread(str(REFERENCE/stage['top_view_label_mask']),cv2.IMREAD_UNCHANGED)
                self.assertEqual(labels.shape,(350,350))
                for label,kind in stage['label_mapping'].items():
                    area=np.count_nonzero(labels==int(label))*.2*.2
                    self.assertAlmostEqual(area,stage['visible_geometry'][kind]['visible_area_mm2'],places=4)
