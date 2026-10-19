"""Class IDs, empty masks and frame colour at the SAM processor boundary."""
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import torch
from so101_teach.yolo_inference import Sam3Segmenter


class Sam3AdapterTests(unittest.TestCase):
    def model(self,mask):
        model=Sam3Segmenter.__new__(Sam3Segmenter)
        model.conf=.5;model.prompts=[{'id':1,'prompt':'carrier'},{'id':13,'prompt':'B complete'}]
        self.seen=[]
        def image(value):self.seen.append(np.asarray(value));return {}
        def text(prompt,state):
            return {'scores':torch.tensor([.9,.4]),'boxes':torch.tensor([[0.,0.,3.,2.],[0.,0.,3.,2.]]),
                    'masks':torch.tensor(np.stack([mask,mask])[:,None])}
        model.processor=SimpleNamespace(set_image=image,set_text_prompt=text)
        return model

    def test_rgb_frame_and_zero_based_class_ids_preserve_confidence_and_masks(self):
        model=self.model(np.ones((2,3),bool));frame=np.zeros((2,3,3),np.uint8);frame[:,:,0]=255
        with patch('torch.autocast',return_value=nullcontext()):result=model.infer(frame)
        np.testing.assert_array_equal(self.seen[0][0,0],[0,0,255])
        self.assertEqual(result['classes'].tolist(),[0,12])
        self.assertEqual(result['masks'].shape,(2,2,3));self.assertTrue(np.all(result['scores']>=.5))

    def test_empty_masks_produce_well_shaped_empty_output(self):
        model=self.model(np.zeros((2,3),bool))
        with patch('torch.autocast',return_value=nullcontext()):result=model.infer(np.zeros((2,3,3),np.uint8))
        self.assertEqual(result['boxes'].shape,(0,4));self.assertEqual(result['masks'].shape,(0,2,3))

    def test_text_cache_is_per_model_and_unknown_prompts_fall_back(self):
        from unittest.mock import Mock
        instances=[]
        for value in (1,2):
            model=Sam3Segmenter.__new__(Sam3Segmenter);model.prompts=[{'prompt':'carrier'}]
            model.text_cache_stats={'hits':0,'misses':0}
            original=Mock(return_value={'features':torch.tensor(value)})
            model.model=SimpleNamespace(backbone=SimpleNamespace(forward_text=original))
            with patch('torch.autocast',return_value=nullcontext()):model.enable_text_cache()
            cached=model.model.backbone.forward_text
            first=cached(['carrier'],device='cuda');first['new_key']=True
            self.assertNotIn('new_key',cached(['carrier'],device='cuda'))
            self.assertEqual(original.call_count,1);self.assertEqual(model.text_cache_size,1)
            cached(['new prompt'],device='cuda');self.assertEqual(original.call_count,2)
            self.assertEqual(model.text_cache_stats,{'hits':2,'misses':1})
            instances.append(cached(['carrier'])['features'].item())
        self.assertEqual(instances,[1,2])
