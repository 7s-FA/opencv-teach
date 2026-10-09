"""Pi-authoritative read snapshots; local editing never pushes implicitly."""
from copy import deepcopy
import hashlib,json,time,uuid
from .domain import atomic_json,read_json
from .arm_workspace import arm_id
from . import episode_transfer,pi_connection

def digest(doc):
    return hashlib.sha256(json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def import_snapshot(store,data_dir,snapshot,*,preserve_local_changes=False):
    if snapshot['arm']!=store.robot_id or snapshot['calibration_sha256']!=store.calibration.sha256:
        raise ValueError('Pi 에피소드의 로봇팔·영점이 현재 앱과 다릅니다.')
    docs=snapshot['episodes'];ids=set()
    for doc in docs:
        store.validate(doc)
        if doc['id'] in ids:raise ValueError('Pi 에피소드 ID가 중복되었습니다.')
        ids.add(doc['id'])
    if any(r['episode_id'] not in ids for r in snapshot['recipes'].values()):raise ValueError('Pi A/B 실행본이 목록에 없습니다.')
    path=data_dir/'pi-episodes'/f'{snapshot["arm"]}.json'
    prior=read_json(path) if path.exists() else {};previous=prior.get('hashes',{})
    record={**deepcopy(snapshot),'fetched_at':time.time(),'hashes':{doc['id']:digest(doc) for doc in docs}}
    preserved=[];replaced={}
    try:
        for doc in docs:
            local=store.directory/(doc['id']+'.json');old=store.load(local) if local.exists() else None
            if old:
                old_hash=digest(old)
                if preserve_local_changes and old_hash!=digest(doc) and previous.get(doc['id'])==digest(doc):
                    # An unchanged Pi snapshot must not undo local review settings
                    # merely because the app was reopened.
                    continue
                if old_hash!=digest(doc) and old_hash!=previous.get(doc['id']):
                    draft=deepcopy(old);draft['id']=uuid.uuid4().hex;draft['name']=(old['name'][:105]+' · 로컬 수정본')
                    draft['local_copy_of']=old['id'];store.save(draft);preserved.append(draft['id'])
            replaced[local]=old;store.save(doc)
        atomic_json(path,record)
    except Exception:
        for local,old in replaced.items():
            if old is None:local.unlink(missing_ok=True)
            else:atomic_json(local,old)
        raise
    return record,preserved

class PiEpisodeLibrary:
    def __init__(self,app):
        self.app=app;self.job=None;self.attempted=False;self.ready=False;self.snapshot=None;self.start_signature=None
        path=app.data_dir/'pi-episodes'/f'{arm_id(app.profile)}.json'
        if path.exists():
            try:self.snapshot=read_json(path)
            except (OSError,ValueError):pass
        self.set_status('Pi 확인 전 · 이전에 저장한 에피소드 표시')
    def set_status(self,text):
        self.app.pi_episode_status.set(text)
    def refresh(self,*,preserve_local_changes=True):
        a=self.app
        if self.job or a.closed:return
        if a.episode_adjust_panel.dirty:raise ValueError('변경한 실행 설정·완제품·안착 검사를 저장하거나 입력을 되돌린 뒤 Pi에서 불러오세요.')
        if a.episode_name.get().strip()!=a.episode['name']:raise ValueError('에피소드 이름을 저장한 뒤 Pi에서 불러오세요.')
        step=next((s for s in a.episode['steps'] if s['id']==a.selected),None)
        if step and (a.step_name.get()!=step['name'] or any(a.tick_vars[k].get()!=str(v) for k,v in step['ticks'].items())):
            raise ValueError('편집한 자세를 스텝에 추가·수정한 뒤 Pi에서 불러오세요.')
        if a.jig_measurement_in_use() or a.live_adjust.owner is not None:raise ValueError('측정·실행·실물 조정을 마친 뒤 Pi에서 불러오세요.')
        config=pi_connection.validate(pi_connection.load(a.data_dir),True)
        self.start_signature=self.editor_signature();self.attempted=True;self.preserve_local_changes=preserve_local_changes
        self.job=a.settings.pool.submit(episode_transfer.remote_request,config,{'operation':'pull','arm':arm_id(a.profile)})
        self.set_status('Pi 에피소드·완료 알림·실행 설정 불러오는 중…')
    def editor_signature(self):
        a=self.app
        return (digest(a.episode),a.episode_name.get(),a.step_name.get(),tuple(v.get() for v in a.tick_vars.values()),a.follow_jig.get(),a.selected_jig_id(),getattr(a,'second_editor',None) is not None,tuple(v.get() for v in a.episode_adjust_panel.values.values()))
    def apply_settings(self,snapshot):
        a=self.app;s=snapshot['settings']
        from .motion import SPEED_PRESETS
        choice=next((k for k,v in SPEED_PRESETS.items() if v==s.get('speed')),None)
        if choice:a.motion_speed_choice.set(choice)
        seconds=s.get('hold_seconds',a.pose_latch.seconds);acquisition=s.get('acquisition_seconds',a.pose_latch.stability.seconds);attempts=s.get('acquisition_attempts',a.measurement_attempts_limit)
        a.pose_latch.configure(seconds,acquisition_seconds=acquisition,attempts_limit=attempts)
        a.detector.seconds=seconds;a.detector.acquisition_seconds=acquisition;a.detector.attempts_limit=attempts;a.measurement_attempts_limit=attempts
        for latch in a.detector.latches.values():latch.configure(seconds,acquisition_seconds=acquisition,attempts_limit=attempts)
        a.hold_seconds.set(str(seconds));a.acquisition_seconds.set(str(acquisition));a.measurement_attempts.set(str(attempts))
    def poll(self):
        if not self.job or not self.job.done():return
        job=self.job;self.job=None;a=self.app
        if a.closed:return
        try:
            snapshot=job.result()
            if a.episode_adjust_panel.dirty or a.jig_measurement_in_use() or a.live_adjust.owner is not None or self.editor_signature()!=self.start_signature:
                self.set_status('Pi 응답 도착 · 현재 편집·실행을 유지합니다. 완료 후 다시 불러오세요.');return
            previous_slot=next((k for k,v in (self.snapshot or {}).get('recipes',{}).items() if v['episode_id']==a.episode['id']),None)
            record,preserved=import_snapshot(a.store,a.data_dir,snapshot,preserve_local_changes=getattr(self,'preserve_local_changes',False));self.snapshot=record;self.ready=True
            wanted=a.episode['id'];ids=record['hashes']
            if previous_slot in record['recipes']:wanted=record['recipes'][previous_slot]['episode_id']
            if wanted not in ids:wanted=record['recipes'].get('A',{}).get('episode_id') or next(iter(ids),None)
            a.refresh_library()
            if wanted:
                index=next(i for i,(_,doc) in enumerate(a.library_entries) if doc['id']==wanted)
                a.library.selection_clear(0,'end');a.library.selection_set(index);a.load_selected_episode()
            self.apply_settings(record)
            local_review=any(d['id'] in ids and digest(d)!=ids[d['id']] for _,d in a.library_entries)
            self.set_status(('Pi 변경 없음 · 로컬 검토 설정 유지' if local_review else f'Pi에서 불러옴 · {len(ids)}개 · '+time.strftime('%H:%M:%S'))+(f' · 로컬 수정본 {len(preserved)}개 별도 보존' if preserved else ''))
            a.refresh_episode_policy()
        except Exception as exc:
            self.set_status('Pi 불러오기 실패 · 로컬 보관본 유지 · '+str(exc))
    def label(self,doc):
        snapshot=self.snapshot or {};slots=[k for k,v in snapshot.get('recipes',{}).items() if v['episode_id']==doc['id']]
        old=snapshot.get('hashes',{}).get(doc['id'])
        tag=('Pi '+('/'.join(slots) or '보관본')) if old else '로컬'
        if old and digest(doc)!=old:tag+=' · 수정됨'
        return f'[{tag}] {doc["name"]}'
