import json,tempfile,unittest
from copy import deepcopy
from pathlib import Path
import cv2,numpy as np
from so101_teach.domain import ROOT
from so101_teach.vision import stl_profile,detect,project_plane
from so101_teach.configuration import JigCatalog
from so101_teach.height_reference import support_plane_profile,detection_plane_z
from so101_teach.grid_model import grid_layout
from so101_teach.raised_model import RaisedEvidence
from so101_teach.lens_geometry import DetectionFrame

class AssemblyDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).parent/'fixtures/vision-assembly';cls.cfg=json.loads((folder/'fixture.json').read_text());cls.frame=cv2.imread(str(folder/'current.png'))
        cls.key='b5ebdb54807c441c8267c15d77713b6a';cls.j=cls.cfg['jigs'][cls.key];cls.path=ROOT/'assets/carrier_assembly/carrier_assembly.stl'
        cls.mesh={**stl_profile(cls.path),'shape':'rectangle','method':'grid'};cls.profile=support_plane_profile(cls.cfg['profile'],cls.j)
    def test_assembly_separates_full_height_from_low_carrier_rim(self):
        m=self.mesh;self.assertAlmostEqual(m['size_mm'][2],31.3,places=4);self.assertAlmostEqual(m['rim_z_mm'],4.8)
        self.assertEqual(len(m['assembly']['fixtures']),6)
        np.testing.assert_allclose(grid_layout(m)['points'],[[-36,0],[36,0]],atol=1e-5)
        self.assertAlmostEqual(detection_plane_z(self.profile,m),self.profile['table_z_mm']+4.8)
    def test_registration_and_reopen_preserve_assembly_without_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog=JigCatalog(tmp,profile=self.cfg['profile']);d=catalog.save({**deepcopy(self.j),'stl':str(self.path)})
            self.assertEqual(Path(d['stl']).read_bytes(),self.path.read_bytes());self.assertEqual(d['roi'],self.j['roi']);self.assertEqual(d['support_height_mm'],self.j['support_height_mm'])
            reopened=JigCatalog(tmp,profile=self.cfg['profile']).mesh(self.key)
            self.assertEqual(reopened['assembly'],self.mesh['assembly']);self.assertEqual(reopened['sha256'],self.mesh['sha256'])
    def test_loaded_real_carrier_retains_boundary_and_ignores_inner_holes(self):
        old=detect(self.frame,{**self.j['mesh'],'shape':'rectangle','method':'grid'},self.j['roi'],self.profile)['selected']
        c=detect(self.frame,self.mesh,self.j['roi'],self.profile)['selected'];self.assertTrue(c['grid_verified']);self.assertEqual(c['hole_matches'],[])
        np.testing.assert_allclose(c['center_px'],old['center_px'],atol=.2)
    def test_changed_part_contents_keep_external_pose(self):
        before=detect(self.frame,self.mesh,self.j['roi'],self.profile)['selected'];metric=before['metric'];yaw=np.deg2rad(metric['yaw_deg']);rot=np.array([[np.cos(yaw),-np.sin(yaw)],[np.sin(yaw),np.cos(yaw)]])
        for color in [(20,20,20),(230,40,130)]:
            frame=self.frame.copy()
            for fixture in self.mesh['assembly']['fixtures']:
                local=np.array([[-18,-18],[18,-18],[18,18],[-18,18]])+fixture['center_mm'];xy=local@rot.T+metric['center_xy_mm']
                points=project_plane(xy,self.profile,detection_plane_z(self.profile,self.mesh)+12)
                cv2.fillConvexPoly(frame,np.rint(points).astype(int),color)
            # Product contents can change, but the user-confirmed fixed A
            # opening remains visible and is now the heading reference.
            keep=np.zeros(frame.shape[:2],np.uint8)
            cv2.fillConvexPoly(keep,np.rint(before['orientation_hole_outline_px']).astype(int),255)
            keep=cv2.dilate(keep,np.ones((7,7),np.uint8));frame[keep>0]=self.frame[keep>0]
            c=detect(frame,self.mesh,self.j['roi'],self.profile)['selected'];self.assertIsNotNone(c);self.assertEqual(c['hole_matches'],[])
            np.testing.assert_allclose(c['center_px'],before['center_px'],atol=1)
    def test_shifted_initial_rectangle_refits_the_visible_carrier_boundary(self):
        frame=cv2.imread(str(Path(__file__).parent/'fixtures/vision-assembly/shifted-boundary.png'))
        c=detect(frame,self.mesh,self.j['roi'],self.profile)['selected']
        self.assertTrue(c['observed_boundary_refined']);self.assertTrue(c['orientation_verified'])
        # Corners manually read on the raw photograph, excluding protruding products.
        expected=np.array([[1080,293],[1256,565],[1100,703],[904,402]])
        aligned=min((np.roll(np.asarray(c['quad']),i,axis=0) for i in range(4)),key=lambda q:np.linalg.norm(q-expected))
        np.testing.assert_allclose(aligned,expected,atol=5)
        self.assertGreaterEqual(min(c['edge_support']),.60)
        self.assertGreaterEqual(sorted(c['edge_support'])[1],.90)

    def test_assembly_rejects_absent_carrier_and_wrong_size(self):
        other=Path(__file__).parent/'fixtures/vision-position';v=json.loads((other/'fixture.json').read_text());j=v['jigs'][self.key]
        frame=cv2.imread(str(other/'carrier-removed.jpg'));self.assertIsNone(detect(frame,self.mesh,j['roi'],support_plane_profile(v['profile'],j))['selected'])
        self.assertIsNone(detect(self.frame,{**self.mesh,'size_mm':[160,110,31.3]},self.j['roi'],self.profile)['selected'])

    def test_tabletop_height_recovers_carrier_with_bounded_grid_seed_scale(self):
        profile=support_plane_profile(self.cfg['profile'],{**self.j,'support_height_mm':0.})
        for index in (0,1):
            frame=cv2.imread(str(Path(__file__).parent/f'fixtures/vision-assembly/tabletop-rim-{index}.jpg'))
            c=detect(frame,self.mesh,self.j['roi'],profile)['selected']
            self.assertIsNotNone(c);self.assertTrue(c['grid_verified']);self.assertTrue(c['observed_boundary_refined'])
            self.assertLessEqual(abs(c['boundary_scale']-1),.03)
            self.assertGreaterEqual(min(c['edge_support']),.60)
            self.assertGreaterEqual(sorted(c['edge_support'])[1],.90)
            expected=np.array([[1081,293],[1258,567],[1098,709],[902,403]])
            aligned=min((np.roll(np.asarray(c['quad']),i,axis=0) for i in range(4)),key=lambda q:np.linalg.norm(q-expected))
            np.testing.assert_allclose(aligned,expected,atol=5)

class RaisedPalletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder=Path(__file__).parent/'fixtures/vision-assembly';cls.cfg=json.loads((folder/'fixture.json').read_text());cls.frame=cv2.imread(str(folder/'current.png'))
        cls.j=cls.cfg['jigs']['pallet'];cls.mesh={**stl_profile(ROOT/'assets/jigs/pallet.stl'),'shape':'ring','method':'edges'};cls.profile=support_plane_profile(cls.cfg['profile'],cls.j)
    def test_empty_real_pallet_confirms_raised_corner_pairs(self):
        self.assertEqual(len(self.mesh['raised_edges_mm']),8)
        c=detect(self.frame,self.mesh,self.j['roi'],self.profile)['selected'];self.assertTrue(c['raised_features_verified']);self.assertGreaterEqual(min(c['raised_corner_support']),.55)
        np.testing.assert_allclose(c['center_px'],[431,188],atol=3)
    def test_ridges_alone_and_blank_do_not_establish_pallet(self):
        p={'table_z_mm':0,'intrinsics':{'size':[640,480],'K':[[1200,0,320],[0,1200,240],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,500],[0,0,0,1]]}}
        for draw in [False,True]:
            frame=np.full((480,640,3),70,np.uint8)
            if draw:
                for edge in np.array(self.mesh['raised_edges_mm']):
                    points=project_plane(edge[:,:2],p,float(edge[0,2]));cv2.line(frame,tuple(np.rint(points[0]).astype(int)),tuple(np.rint(points[1]).astype(int)),(230,230,230),2)
            self.assertIsNone(detect(frame,self.mesh,profile=p)['selected'])
    def test_raised_height_is_used_in_projection(self):
        p={'table_z_mm':0,'intrinsics':{'size':[640,480],'K':[[1000,0,320],[0,1000,240],[0,0,1]],'D':[0,0,0,0,0]},'extrinsics':{'base_from_camera':[[1,0,0,0],[0,-1,0,0],[0,0,-1,180],[0,0,0,1]]}}
        frame=np.full((480,640,3),70,np.uint8)
        for edge in np.array(self.mesh['raised_edges_mm']):
            pts=np.rint(project_plane(edge[:,:2],p,float(edge[0,2]))).astype(int);cv2.line(frame,tuple(pts[0]),tuple(pts[1]),(230,230,230),1)
        allowed=np.full(frame.shape[:2],255,np.uint8);pose=np.array([[0.,0.,0.]])
        good=RaisedEvidence(frame,self.mesh,allowed,p).evaluate(pose)[1][0];wrong=deepcopy(self.mesh)
        for edge in wrong['raised_edges_mm']:
            for vertex in edge:vertex[2]=20
        bad=RaisedEvidence(frame,wrong,allowed,p).evaluate(pose)[1][0]
        self.assertTrue(good);self.assertFalse(bad)
