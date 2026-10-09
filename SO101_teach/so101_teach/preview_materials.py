"""Deterministic procedural materials for the preview, without external assets."""
from functools import lru_cache
import numpy as np

@lru_cache(maxsize=2)
def oak_texture(width,height):
    # Seamless, matte light oak boards. Grain varies along each board, not per frame.
    u=np.arange(width,dtype=float)[None,:]/width;v=np.arange(height,dtype=float)[:,None]/height
    board=np.minimum((u*6).astype(int),5);across=u*6-board
    phases=np.array([.2,1.6,4.1,2.7,5.2,.9])[board]
    wave=across+.045*np.sin(2*np.pi*v+phases)+.018*np.sin(6*np.pi*v+phases)
    grain=np.sin(2*np.pi*(18*wave+.23*np.sin(2*np.pi*v+phases)))
    fine=np.sin(2*np.pi*(49*wave+.4*np.sin(4*np.pi*v+phases)))
    variation=np.array([2,-4,5,-1,3,-3])[board]
    shade=variation+3.5*grain+1.2*fine-5*np.maximum(0,grain)**10
    # Subtle plank edges; avoid dark stripes that compete with white jigs.
    shade-=np.where((across<.012)|(across>.988),10.,0.)
    rgb=np.array([215.,191.,157.])[None,None,:]+shade[:,:,None]*np.array([1.,1.05,.95])
    return np.clip(rgb,0,255).astype(np.uint8)

def apply_preview_materials(model):
    try:index=model.texture('floor').id
    except KeyError:return
    w,h=int(model.tex_width[index]),int(model.tex_height[index]);channels=int(model.tex_nchannel[index]);start=int(model.tex_adr[index])
    if channels!=3:raise ValueError('미리보기 바닥 텍스처는 RGB여야 합니다.')
    model.tex_data[start:start+w*h*channels]=oak_texture(w,h).reshape(-1)
