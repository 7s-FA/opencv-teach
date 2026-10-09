"""Reuse immutable image evidence within one frame, never across frames or ROIs."""
import cv2
import numpy as np

class FrameEvidence:
    def __init__(self,frame):self.frame=frame;self.cache={}
    def get(self,key,make):
        if key not in self.cache:self.cache[key]=make()
        return self.cache[key]
    def gray(self,enhanced=False):
        if enhanced:return self.get('clahe',lambda:cv2.createCLAHE(clipLimit=2.,tileGridSize=(8,8)).apply(self.gray()))
        return self.get('gray',lambda:cv2.cvtColor(self.frame,cv2.COLOR_BGR2GRAY))
    def blurred(self,enhanced=False):return self.get(('blur',enhanced),lambda:cv2.GaussianBlur(self.gray(enhanced),(3,3),0))
    def hsv(self):return self.get('hsv',lambda:cv2.cvtColor(self.frame,cv2.COLOR_BGR2HSV))
    def white(self):
        def make():
            hsv=self.hsv().astype(float)
            return np.clip((hsv[:,:,2]-90)/90,0,1)*(1-np.clip((hsv[:,:,1]-40)/120,0,1))
        return self.get('white',make)
    def edges(self,low=20,high=60,enhanced=False):return self.get(('edges',low,high,enhanced),lambda:cv2.Canny(self.blurred(enhanced),low,high))

class RegionEvidence:
    def __init__(self,frame,allowed,source=None):self.source=source or FrameEvidence(frame);self.allowed=allowed;self.cache={}
    def edges(self,low=20,high=60,enhanced=False):
        key=('edges',low,high,enhanced)
        if key not in self.cache:self.cache[key]=cv2.bitwise_and(self.source.edges(low,high,enhanced),self.allowed)
        return self.cache[key]
    def distance(self,low=20,high=60,enhanced=False):
        key=('distance',low,high,enhanced)
        if key not in self.cache:self.cache[key]=cv2.distanceTransform(cv2.bitwise_not(self.edges(low,high,enhanced)),cv2.DIST_L2,3)
        return self.cache[key]
