"""Evaluated YOLO segmentation backends; no teaching or motion inputs.

Pre/postprocessing retained from the 2026-10-16 nine-model evaluation.
"""
from pathlib import Path
import math,time
import cv2
import numpy as np
from .inference_protocol import DEFAULT_CONFIDENCE

CLASS_LABELS=('운반 지그','완제품 팔레트','리니어 지그','A 하단','A 중단','A 상단',
              'B 하단','B 중단','B 상단','A 하단+중단','B 하단+중단','A 완제품','B 완제품')
CLASS_NAMES=('carrier_jig','pallet','linear_jig','A_lower','A_middle','A_upper',
             'B_lower','B_middle','B_upper','A_lower_middle','B_lower_middle','A_complete','B_complete')

def checked_manifest(path):
 import hashlib,json
 path=Path(path);manifest=json.loads((path/'manifest.json').read_text())
 for name,digest in manifest['files'].items():
  if Path(name).name!=name:raise ValueError('잘못된 모델 파일 이름: '+name)
  actual=hashlib.sha256()
  with (path/name).open('rb') as stream:
   for chunk in iter(lambda:stream.read(1024*1024),b''):actual.update(chunk)
  if actual.hexdigest()!=digest:
   raise ValueError('모델 파일 해시가 다릅니다: '+name)
 return manifest

def label_font():
 from PIL import ImageFont
 return ImageFont.truetype(str(Path(__file__).resolve().parents[1]/'assets/fonts/NanumGothic.ttf'),18)

def deduplicate_classes(result,confidence):
 """Same conservative mask-IoU rules as the final comparison, using 0-based IDs."""
 keep=list(range(len(result['scores'])))
 for classes,minimum_iou,minimum_margin in [({1,2},.8,.02),(set(range(3,13)),.9,.05)]:
  retained=[];removed=set()
  for i in sorted((i for i in keep if int(result['classes'][i]) in classes and result['scores'][i]>=confidence),key=lambda i:(-float(result['scores'][i]),i)):
   for j in retained:
    if result['classes'][i]==result['classes'][j]:continue
    if float(result['scores'][j])-float(result['scores'][i])+1e-12<minimum_margin:continue
    a,b=result['masks'][i],result['masks'][j];union=np.count_nonzero(a|b)
    if union and np.count_nonzero(a&b)/union>=minimum_iou:
     removed.add(i);break
   if i not in removed:retained.append(i)
  keep=[i for i in keep if i not in removed]
 return {**result,**{k:result[k][keep] for k in ('boxes','scores','classes','masks')}}

class Segmenter:
 def __init__(self,path,threads=4,conf=DEFAULT_CONFIDENCE,iou=.7):
  self.path=Path(path);self.conf=conf;self.iou=iou;cv2.setNumThreads(1)
  import ncnn,yaml
  self.manifest=checked_manifest(self.path)
  self.ncnn=ncnn;self.net=ncnn.Net();self.net.opt.num_threads=threads;self.net.opt.use_vulkan_compute=False
  if self.net.load_param(str(self.path/'model.ncnn.param'))!=0:raise ValueError('모델 구조를 읽지 못했습니다.')
  if self.net.load_model(str(self.path/'model.ncnn.bin'))!=0:raise ValueError('모델 가중치를 읽지 못했습니다.')
  meta=yaml.safe_load((self.path/'metadata.yaml').read_text());self.kind='ncnn'
  self.names={int(k):v for k,v in meta['names'].items()};self.size=tuple(meta['imgsz']);self.end2end=meta.get('end2end',False)
  self.font=label_font()
 def infer(self,frame):
  started=time.perf_counter();h,w=frame.shape[:2];ih,iw=self.size;gain=min(ih/h,iw/w);nw,nh=round(w*gain),round(h*gain)
  left,top=round((iw-nw)/2-.1),round((ih-nh)/2-.1)
  im=cv2.resize(frame,(nw,nh),interpolation=cv2.INTER_LINEAR)
  im=cv2.copyMakeBorder(im,top,ih-nh-top,left,iw-nw-left,cv2.BORDER_CONSTANT,value=(114,114,114))
  x=np.ascontiguousarray(im[:,:,::-1].transpose(2,0,1),dtype=np.float32)/255.
  before=time.perf_counter()
  with self.net.create_extractor() as ex:
   if ex.input(self.net.input_names()[0],self.ncnn.Mat(x))!=0:raise RuntimeError('NCNN 입력 실패')
   outs=[]
   for name in sorted(self.net.output_names()):
    code,value=ex.extract(name)
    if code:raise RuntimeError(f'NCNN 출력 실패: {name} {code}')
    outs.append(np.array(value))
  infer_ms=(time.perf_counter()-before)*1000
  pred=next(o for o in outs if o.ndim==2);proto=next(o for o in outs if o.ndim==3)
  if self.end2end:
   rows=pred[pred[:,4]>=self.conf][:50];boxes=rows[:,:4].copy();scores=rows[:,4];classes=rows[:,5].astype(int);coeff=rows[:,6:]
  else:
   pred=pred.T;nc=len(self.names);classes=pred[:,4:4+nc].argmax(1);scores=pred[np.arange(len(pred)),4+classes];keep=scores>=self.conf
   pred,classes,scores=pred[keep],classes[keep],scores[keep];boxes=np.concatenate((pred[:,:2]-pred[:,2:4]/2,pred[:,:2]+pred[:,2:4]/2),axis=1)
   order=np.argsort(-scores,kind='stable');selected=[]
   while len(order) and len(selected)<50:
    i=order[0];selected.append(i);rest=order[1:]
    wh=np.maximum(0,np.minimum(boxes[i,2:],boxes[rest,2:])-np.maximum(boxes[i,:2],boxes[rest,:2]));inter=wh.prod(1)
    area=np.maximum(0,boxes[:,2:]-boxes[:,:2]).prod(1);ious=inter/np.maximum(area[i]+area[rest]-inter,1e-7)
    order=rest[(ious<=self.iou)|(classes[rest]!=classes[i])]
   selected=np.asarray(selected,dtype=int);boxes,scores,classes=boxes[selected],scores[selected],classes[selected];coeff=pred[selected,4+nc:]
  boxes=(boxes-np.array([left,top,left,top]))/gain;boxes[:,[0,2]]=boxes[:,[0,2]].clip(0,w);boxes[:,[1,3]]=boxes[:,[1,3]].clip(0,h)
  c,mh,mw=proto.shape;logits=(coeff@proto.reshape(c,-1)).reshape(-1,mh,mw);masks=[]
  g=min(4*mh/h,4*mw/w);pad=(4*mh-round(h*g))|(4*mw-round(w*g));factor=4//math.gcd(4,pad|pad//2)
  for mask,box in zip(logits,boxes):
   if factor>1:mask=cv2.resize(mask,(mw*factor,mh*factor),interpolation=cv2.INTER_LINEAR)
   hh,ww=mask.shape;g=min(hh/h,ww/w);t,l=round((hh-round(h*g))/2-.1),round((ww-round(w*g))/2-.1)
   mask=cv2.resize(mask[t:t+round(h*g),l:l+round(w*g)],(w,h),interpolation=cv2.INTER_LINEAR)>0
   x1,y1,x2,y2=np.ceil(box).astype(int);mask[:y1]=False;mask[y2:]=False;mask[:,:x1]=False;mask[:,x2:]=False;masks.append(mask)
  masks=np.asarray(masks,dtype=bool).reshape(-1,h,w);valid=masks.any(axis=(1,2))
  result={'boxes':boxes[valid],'scores':scores[valid],'classes':classes[valid],'masks':masks[valid],'inference_ms':infer_ms,'total_ms':(time.perf_counter()-started)*1000}
  return result
 def draw(self,frame,result):
  im=frame.copy()
  for box,score,cls,mask in zip(result['boxes'],result['scores'],result['classes'],result['masks']):
   color=tuple(int(v) for v in cv2.cvtColor(np.uint8([[[int(cls)*17%180,210,245]]]),cv2.COLOR_HSV2BGR)[0,0]);im[mask]=(im[mask]*.72+np.array(color)*.28).astype(np.uint8)
   cv2.drawContours(im,cv2.findContours(mask.astype(np.uint8),cv2.RETR_LIST,cv2.CHAIN_APPROX_SIMPLE)[0],-1,color,1)
   x1,y1,x2,y2=box.astype(int);cv2.rectangle(im,(x1,y1),(x2,y2),color,1)
  from PIL import Image,ImageDraw
  canvas=Image.fromarray(cv2.cvtColor(im,cv2.COLOR_BGR2RGB));draw=ImageDraw.Draw(canvas)
  for box,score,cls in zip(result['boxes'],result['scores'],result['classes']):
   label=f'{CLASS_LABELS[int(cls)]} {score*100:.0f}%';bounds=draw.textbbox((0,0),label,font=self.font)
   width,height=bounds[2]-bounds[0]+10,bounds[3]-bounds[1]+8
   x=max(0,min(int(box[0]),im.shape[1]-width));y=max(0,int(box[1])-height-2)
   draw.rounded_rectangle((x,y,x+width,y+height),radius=3,fill=(20,45,56))
   draw.text((x+5,y+4-bounds[1]),label,font=self.font,fill='white')
  return cv2.cvtColor(np.asarray(canvas),cv2.COLOR_RGB2BGR)


class TensorRTSegmenter(Segmenter):
 """The selected, pre-built FP16 engine; never export or fall back to CPU."""
 def __init__(self,path,threads=4,conf=DEFAULT_CONFIDENCE,iou=.7):
  import torch
  from ultralytics import YOLO
  self.path=Path(path);self.manifest=checked_manifest(path)
  if self.manifest.get('format')!='TensorRT FP16':raise ValueError('TensorRT FP16 모델이 아닙니다.')
  if not torch.cuda.is_available():raise RuntimeError('PC NVIDIA GPU를 사용할 수 없습니다.')
  torch.set_num_threads(threads);torch.backends.cuda.matmul.allow_tf32=False;torch.backends.cudnn.allow_tf32=False
  cv2.setNumThreads(1);self.conf=conf;self.iou=iou;self.font=label_font();self.kind='tensorrt'
  self.names=dict(enumerate(CLASS_NAMES));self.model=YOLO(str(self.path/'model.engine'),task='segment')
  self.infer(np.zeros((720,1280,3),dtype=np.uint8))

 def infer(self,frame):
  started=time.perf_counter();h,w=frame.shape[:2]
  pred=self.model.predict(frame,imgsz=960,device=0,quantize=16,conf=self.conf,iou=self.iou,
                          max_det=300,retina_masks=True,rect=False,nms=True,verbose=False,save=False)[0]
  boxes=np.empty((0,4),dtype=np.float32);scores=np.empty(0,dtype=np.float32)
  classes=np.empty(0,dtype=np.int64);masks=np.empty((0,h,w),dtype=bool)
  if pred.masks is not None:
   classes=pred.boxes.cls.cpu().numpy().astype(int);scores=pred.boxes.conf.float().cpu().numpy()
   boxes=pred.boxes.xyxy.float().cpu().numpy();masks=pred.masks.data.cpu().numpy().astype(bool)
   if masks.shape[1:]!=(h,w):raise ValueError('인식 마스크 크기가 원본 프레임과 다릅니다.')
   keep=[]
   for cls in sorted(set(classes)):
    indices=np.flatnonzero(classes==cls);indices=indices[np.argsort(-scores[indices],kind='stable')[:20]]
    keep.extend(int(i) for i in indices if scores[i]>=self.conf and masks[i].any())
   boxes,scores,classes,masks=boxes[keep],scores[keep],classes[keep],masks[keep]
  result=deduplicate_classes({'boxes':boxes,'scores':scores,'classes':classes,'masks':masks},self.conf)
  result.update(inference_ms=float(pred.speed['inference']),total_ms=(time.perf_counter()-started)*1000)
  return result


class Sam3Segmenter(Segmenter):
 """Selected mixed adapter, original base and 13 evaluated text prompts."""
 def __init__(self,path,threads=4,conf=DEFAULT_CONFIDENCE,iou=.7):
  import json,torch
  from sam3.model_builder import build_sam3_image_model
  from sam3.model.sam3_image_processor import Sam3Processor
  self.path=Path(path);self.manifest=checked_manifest(path)
  if self.manifest.get('format')!='PyTorch BF16':raise ValueError('SAM3 BF16 모델이 아닙니다.')
  optimization=self.manifest.get('runtime_optimization',{})
  compiled=bool(optimization.get('torch_compile',False))
  if not torch.cuda.is_available():raise RuntimeError('PC NVIDIA GPU를 사용할 수 없습니다.')
  free,_=torch.cuda.mem_get_info()
  if free<4*1024**3:
   raise RuntimeError(f'SAM3 실행에 필요한 GPU 여유 메모리가 부족합니다 (현재 {free/1024**3:.1f} GiB). 다른 GPU 작업 완료 후 다시 시작하세요.')
  torch.set_num_threads(threads);cv2.setNumThreads(1)
  self.conf=conf;self.iou=iou;self.font=label_font();self.kind='sam3'
  self.names=dict(enumerate(CLASS_NAMES));self.prompts=json.loads((self.path/'prompts.json').read_text())
  if [(p['id'],p['class_name']) for p in self.prompts]!=[(i+1,n) for i,n in enumerate(CLASS_NAMES)]:
   raise ValueError('SAM3 클래스·프롬프트 순서가 다릅니다.')
  self.model=build_sam3_image_model(checkpoint_path=str(self.path/'base.pt'),load_from_HF=False,
       device='cuda',eval_mode=True,enable_segmentation=True,enable_inst_interactivity=False,compile=compiled)
  saved=torch.load(self.path/'adapter.pt',map_location='cpu',weights_only=False)
  expected={n for n,p in self.model.named_parameters() if not n.startswith('backbone.')}
  if set(saved['trainable'])!=expected:raise ValueError('SAM3 학습 가중치 구성이 다릅니다.')
  mismatch=self.model.load_state_dict(saved['trainable'],strict=False)
  if mismatch.unexpected_keys or any(n in expected for n in mismatch.missing_keys):
   raise ValueError('SAM3 학습 가중치를 복원하지 못했습니다.')
  del saved
  self.model.eval();self.model.requires_grad_(False)
  self.processor=Sam3Processor(self.model,resolution=1008,confidence_threshold=conf)
  self.network_ms=0.
  # Match the comparison's synchronized image/text/grounding call boundary.
  def timed(fn):
   def call(*args,**kwargs):
    torch.cuda.synchronize();start=time.perf_counter()
    result=fn(*args,**kwargs)
    torch.cuda.synchronize();self.network_ms+=(time.perf_counter()-start)*1000
    return result
   return call
  self.model.backbone.forward_image=timed(self.model.backbone.forward_image)
  self.model.backbone.forward_text=timed(self.model.backbone.forward_text)
  self.model.forward_grounding=timed(self.model.forward_grounding)
  self.text_cache_stats={'hits':0,'misses':0}
  if optimization.get('text_feature_cache',False):self.enable_text_cache()
  if compiled:
   # Build/load kernels before the worker announces readiness, not on a camera request.
   self.infer(np.zeros((720,1280,3),dtype=np.uint8))

 def enable_text_cache(self):
  import torch
  original=self.model.backbone.forward_text;cache={}
  with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):
   for prompt in self.prompts:cache[(prompt['prompt'],)]=original([prompt['prompt']],device='cuda')
  self.text_cache_size=len(cache)
  def cached(texts,*args,**kwargs):
   key=tuple(texts)
   if key in cache:
    self.text_cache_stats['hits']+=1;return dict(cache[key])
   self.text_cache_stats['misses']+=1;return original(texts,*args,**kwargs)
  self.model.backbone.forward_text=cached

 def infer(self,frame):
  import torch
  from PIL import Image
  started=time.perf_counter();self.network_ms=0.;h,w=frame.shape[:2]
  self.processor.confidence_threshold=self.conf
  boxes=[];scores=[];classes=[];masks=[]
  with torch.inference_mode(),torch.autocast('cuda',dtype=torch.bfloat16):
   state=self.processor.set_image(Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)))
   for prompt in self.prompts:
    state=self.processor.set_text_prompt(prompt['prompt'],state)
    confidence=state['scores'].float().cpu().numpy()
    for i in np.argsort(-confidence,kind='stable')[:20]:
     if confidence[i]<self.conf:continue
     mask=state['masks'][int(i),0].cpu().numpy().astype(bool)
     if mask.shape!=(h,w):raise ValueError('SAM3 마스크 크기가 원본과 다릅니다.')
     if not mask.any():continue
     boxes.append(state['boxes'][int(i)].float().cpu().numpy());scores.append(confidence[i])
     classes.append(prompt['id']-1);masks.append(mask)
   del state
  result=deduplicate_classes({'boxes':np.asarray(boxes,dtype=np.float32).reshape(-1,4),
    'scores':np.asarray(scores,dtype=np.float32),'classes':np.asarray(classes,dtype=np.int64),
    'masks':np.asarray(masks,dtype=bool).reshape(-1,h,w)},self.conf)
  result.update(inference_ms=self.network_ms,total_ms=(time.perf_counter()-started)*1000)
  return result
