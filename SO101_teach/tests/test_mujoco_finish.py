"""Display shading must preserve objects, texture detail and mesh geometry."""
import unittest
import numpy as np
from so101_teach.domain import ROOT
from so101_teach.mujoco_finish import soften_receiver_shadows


class MuJoCoFinishTests(unittest.TestCase):
    def test_soft_shadow_keeps_object_pixels_and_receiver_texture(self):
        # A textured receiver, a sharp shadow and a red foreground object.
        lit=np.full((60,120,3),200,dtype=np.uint8)
        lit[:,::2]=180
        shaded=lit.copy();shaded[:,:60]-=40
        lit[20:40,45:65]=[190,10,10];shaded[20:40,45:65]=lit[20:40,45:65]
        mask=np.full((60,120),255,dtype=np.uint8);mask[20:40,45:65]=0
        result=soften_receiver_shadows(shaded,lit,[mask])
        np.testing.assert_array_equal(result[20:40,45:65],shaded[20:40,45:65])
        np.testing.assert_array_equal(result[:,5:15],shaded[:,5:15])
        # Test at preview scale: small fixture needs a larger width to get
        # a meaningful feather, without altering the function's pixel scale.
        big_lit=np.repeat(np.repeat(lit,8,axis=0),8,axis=1)
        big_shaded=np.repeat(np.repeat(shaded,8,axis=0),8,axis=1)
        big_mask=np.repeat(np.repeat(mask,8,axis=0),8,axis=1)
        soft=soften_receiver_shadows(big_shaded,big_lit,[big_mask])
        self.assertGreater(int(soft[40,479,0]),int(big_shaded[40,479,0]))
        self.assertLess(int(soft[40,480,0]),int(big_lit[40,480,0]))

    def test_unshadowed_pixels_are_identical(self):
        image=np.random.default_rng(1).integers(0,255,(30,40,3),dtype=np.uint8)
        result=soften_receiver_shadows(image,image,[np.full((30,40),255,np.uint8)])
        np.testing.assert_array_equal(result,image)

    def test_closed_photo_boxes_have_outward_faces(self):
        for name in ('photo_corner_clip','photo_power_supply','photo_blue_pack','photo_blue_pack_2'):
            with self.subTest(name=name):
                raw=(ROOT/'assets/full_map/photo-assets'/f'{name}.stl').read_bytes()
                count=int.from_bytes(raw[80:84],'little')
                rows=np.frombuffer(raw,offset=84,count=count,
                    dtype=[('normal','<f4',(3,)),('vertices','<f4',(3,3)),('attribute','<u2')])
                xyz=rows['vertices'].astype(float)
                volume=np.einsum('ij,ij->i',xyz[:,0],np.cross(xyz[:,1],xyz[:,2])).sum()/6
                self.assertGreater(volume,0)
                normals=np.cross(xyz[:,1]-xyz[:,0],xyz[:,2]-xyz[:,0])
                self.assertTrue(np.all(np.einsum('ij,ij->i',normals,rows['normal'])>0))


if __name__=='__main__':unittest.main()
