"""Render the existing inspection geometry into a PC video frame without Tk."""
from functools import lru_cache
from pathlib import Path
import math
from PIL import Image,ImageDraw,ImageFont
from .overlays import paint_annotations


class _Font:
    def __init__(self):
        self.face=ImageFont.truetype(str(Path(__file__).resolve().parents[1]/'assets/fonts/NanumGothic.ttf'),15)
    def measure(self,text):return self.face.getlength(text)


@lru_cache(maxsize=1)
def _font():return _Font()


class _ImageCanvas:
    """Small raster adapter for the shared annotation placement and labels."""
    def __init__(self,image):self.draw=ImageDraw.Draw(image)
    def create_line(self,*coords,fill,width=1,dash=(),**_):
        points=list(zip(coords[::2],coords[1::2]));width=round(width)
        if not dash:self.draw.line(points,fill=fill,width=width);return
        for a,b in zip(points,points[1:]):
            dx,dy=b[0]-a[0],b[1]-a[1];length=math.hypot(dx,dy)
            if not length:continue
            at=0.
            while at<length:
                end=min(length,at+dash[0])
                self.draw.line([(a[0]+dx*at/length,a[1]+dy*at/length),
                                (a[0]+dx*end/length,a[1]+dy*end/length)],fill=fill,width=width)
                at+=sum(dash)
    def create_rectangle(self,*coords,fill,outline='',**_):self.draw.rectangle(coords,fill=fill,outline=outline or None)
    def create_text(self,x,y,*,text,fill,font,anchor='center',**_):
        self.draw.text((x,y),text,fill=fill,font=font.face,anchor='lm' if anchor=='w' else 'mm')


def render_inspection(raw,size,scene=None):
    """Record only the displayed camera view, without a separate caption strip."""
    scene=scene or {};source=scene.get('image') or raw
    width,height=size
    scale=min(width/source.width,height/source.height)
    fitted=source.convert('RGB').resize((max(1,round(source.width*scale)),max(1,round(source.height*scale))),Image.Resampling.BILINEAR)
    output=Image.new('RGB',(width,height),'#142d38');x=(width-fitted.width)//2;y=(height-fitted.height)//2
    output.paste(fitted,(x,y));font=_font()
    paint_annotations(_ImageCanvas(output),(x,y,fitted.width,fitted.height),scene.get('source_size',source.size),
                      scene.get('outlines',[]),scene.get('checks',[]),font=font)
    if scene.get('placeholder'):
        ImageDraw.Draw(output).multiline_text((width/2,height/2),scene['placeholder'],fill='#e3edf2',font=font.face,anchor='mm',align='center')
    return output
