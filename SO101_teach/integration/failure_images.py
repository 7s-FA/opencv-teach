"""Bounded received-image cache; write evidence only after an action fails."""
import base64
from collections import OrderedDict
from copy import deepcopy
import math
from pathlib import Path
import time
import uuid
from so101_teach.domain import atomic_json


class FailureImages:
    def __init__(self, data_dir, *, capacity=32):
        self.directory=Path(data_dir)/'diagnostics/action-failures'
        self.capacity=capacity;self.frames=OrderedDict();self.detection_at=None
        self.camera_error=None;self.capture_error=None;self.saved=None;self.confirmed=None

    def remember(self, reply):
        """Keep paired image/timestamp/metadata without requesting a new frame."""
        self.camera_error=reply.get('error')
        for image_key,at_key,kind in (('image','at','detection'),('preview_image','preview_at','preview')):
            image,at=reply.get(image_key),reply.get(at_key)
            if not isinstance(image,str) or type(at) not in (int,float) or not math.isfinite(at):continue
            record=self.frames.get(at,{'captured_monotonic':at})
            record.update(image=image,source=kind)
            if image_key=='image':
                record['detection']=deepcopy(reply.get('detection'));self.detection_at=at
            self.frames[at]=record
            # Sort by capture time: a delayed detection reply must not evict a
            # newer preview or become the alleged latest frame.
            self.frames=OrderedDict(sorted(self.frames.items()))
            while len(self.frames)>self.capacity:self.frames.popitem(last=False)
        at=reply.get('at')
        if at in self.frames and isinstance(reply.get('detection'),dict):
            self.frames[at]['detection']=deepcopy(reply['detection']);self.detection_at=at

    def confirm_jigs(self,poses):
        """Pin one received confirmation frame past normal cache eviction."""
        source=self.frames.get(self.detection_at)
        if source:self.confirmed={**deepcopy(source),'confirmed_jigs':deepcopy(poses)}

    def save(self, error, *, context, inspection=None):
        if self.saved:return self.saved
        now=time.monotonic();wall=time.time()
        run_id=context.get('run_id');safe_id=run_id if isinstance(run_id,str) and len(run_id)==32 and all(c in '0123456789abcdef' for c in run_id) else uuid.uuid4().hex
        attempt=context.get('recovery_id')
        suffix=('-retry-'+attempt) if isinstance(attempt,str) and len(attempt)==32 and all(c in '0123456789abcdef' for c in attempt) else ''
        folder=self.directory/(time.strftime('%Y%m%d-%H%M%S',time.localtime(wall))+'-'+safe_id+suffix)
        folder.mkdir(parents=True,exist_ok=False)
        record={'error':str(error),'failed_at':wall,'failed_monotonic':now,'context':deepcopy(context),
                'inspection':deepcopy(inspection),'images':[],'camera_error':self.camera_error,
                'capture_error':self.capture_error,'image_encoding':'Raw received JPEG preserved; paired annotated JPEG rendered from the same frame and saved metadata; no new capture or detection',
                'image_missing_reason':None}
        observation=(inspection or {}).get('result') or {}
        references=[]
        for value in [observation,*((observation.get('results') or {}).values())]:
            if isinstance(value,dict) and isinstance(value.get('decision_evidence'),dict):
                reference=deepcopy(value['decision_evidence'])
                if reference not in references:references.append(reference)
        record['decision_evidence']={'inspection_frames':references,
            'inspection_session':deepcopy(context.get('decision_evidence')),
            'jig_sessions':deepcopy(context.get('jig_decision_evidence',[])),
            'note':'Exact algorithm-input PNG/masks and per-frame decisions are in the referenced manifests; check their errors/gaps before replay.'}
        at=observation.get('at')
        requests=[]
        if inspection:
            record['inspection_image_matched']=at in self.frames
            if at in self.frames:requests.append(('inspection',at))
            else:record['image_missing_reason']='판정 영상이 수신 영상 캐시에 없음; 마지막 수신 영상은 해당 판정 영상이 아님'
        elif self.detection_at in self.frames:requests.append(('detection',self.detection_at))
        if self.frames:
            latest=next(reversed(self.frames))
            if latest not in [stamp for _,stamp in requests]:requests.append(('last-received',latest))
        else:record['image_missing_reason']='이번 실행에서 수신한 카메라 영상 없음'
        sources=[(label,self.frames[stamp]) for label,stamp in requests]
        if self.confirmed:sources.append(('jig-confirmed',self.confirmed))
        for label,source in sources:
            stamp=source['captured_monotonic'];name=label+'.jpg'
            item={k:deepcopy(v) for k,v in source.items() if k!='image'}
            item.update(role=label,variant='raw',age_at_failure_seconds=now-stamp,file=name)
            try:
                raw=base64.b64decode(source['image'],validate=True)
                if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):raise ValueError('수신 JPEG 형식 오류')
                (folder/name).write_bytes(raw)
            except Exception as exc:item.update(file=None,save_error=str(exc))
            record['images'].append(item)
            if item['file']:
                annotated={k:deepcopy(v) for k,v in item.items() if k not in ('detection','confirmed_jigs')}
                annotated.update(role=label+'-annotated',variant='annotated',source_file=name,file=label+'-annotated.jpg')
                try:
                    from so101_teach.failure_overlay import render
                    jpeg,metadata=render(raw,source=source,context=context,role=label,inspection=inspection,error=str(error))
                    (folder/annotated['file']).write_bytes(jpeg);annotated['annotation']=metadata
                except Exception as exc:annotated.update(file=None,save_error=str(exc))
                record['images'].append(annotated)
        from so101_teach.remote_config import code_version
        from so101_teach.episode_inspection import criteria_signature
        record.update(code_version=code_version(),inspection_criteria=criteria_signature())
        atomic_json(folder/'error.json',record)
        self.saved=folder/'error.json'
        return self.saved
