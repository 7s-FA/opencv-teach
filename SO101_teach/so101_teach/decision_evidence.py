"""Bounded, asynchronous diagnostics. Never an input to an acceptance decision."""
from collections import OrderedDict, deque
from copy import deepcopy
import hashlib
import io
import json
import math
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid
import weakref

from .domain import atomic_json

_REGISTRY = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def plain(value):
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if hasattr(value, 'tolist'):
        return plain(value.tolist())
    if isinstance(value,float) and not math.isfinite(value):
        return {'non_finite':str(value)}
    return value


def notify(callback, frame, masks, rows):
    """An optional observer cannot replace, retry or fail a classification."""
    if callback is not None:
        try:
            callback(frame, masks, rows)
        except Exception:
            pass


def note_observation(observation, details):
    reference = (observation or {}).get('decision_evidence')
    if not isinstance(reference, dict):
        return
    with _REGISTRY_LOCK:
        recorder = _REGISTRY.get(reference.get('session'))
    if recorder is not None:
        recorder.note(reference, details)


class DecisionEvidence:
    """Keep the last 64 processed frames / 128 MiB, with a 32 MiB input queue.

    Quotas apply to this newly created session only. Previous sessions and user
    photographs are never deleted. Loss and storage errors remain explicit.
    """
    def __init__(self, data_dir, context, *, stream='inspection', max_frames=64,
                 max_bytes=128 * 1024 * 1024, pending_bytes=32 * 1024 * 1024):
        run_id = context.get('run_id', '')
        if not isinstance(run_id, str) or re.fullmatch('[0-9a-f]{32}', run_id) is None:
            raise ValueError('판정 영상 기록에는 실행 ID가 필요합니다.')
        if stream not in ('jig', 'inspection') or min(max_frames, max_bytes, pending_bytes) <= 0:
            raise ValueError('판정 영상 기록 설정 오류')
        self.session = uuid.uuid4().hex
        self.folder = Path(data_dir) / 'diagnostics/decision-evidence' / run_id / (stream + '-' + self.session)
        self.folder.mkdir(parents=True, exist_ok=False)
        self.context = plain(deepcopy(context))
        self.stream = stream
        self.max_frames, self.max_bytes, self.pending_limit = max_frames, max_bytes, pending_bytes
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.frames = deque()
        self.gaps = deque(maxlen=128)
        self.notes = OrderedDict()
        self.errors = deque(maxlen=16)
        self.total = self.pending = self.retained_bytes = self.dropped = self.evicted = 0
        self.notes_evicted = 0
        self.complete = False
        self.created_at = time.time()
        self._write_manifest()
        with _REGISTRY_LOCK:
            _REGISTRY[self.session] = self
        self.thread = threading.Thread(target=self._run, name='decision-evidence', daemon=True)
        self.thread.start()

    def reference(self, frame_id=None):
        return {'session': self.session, 'manifest': str(self.folder / 'manifest.json'),
                'frame_id': frame_id, 'stream': self.stream}

    def _gap(self, reference, reason, at):
        self.dropped += 1
        self.gaps.append({**reference, 'reason': reason, 'captured_at': at})

    def submit(self, frame, at, *, masks=(), search_masks=None, metadata=None):
        """Copy only after reserving bounded memory; never wait for disk/queue."""
        reference = None
        reserved = 0
        try:
            search_masks = search_masks or {}
            cost = frame.nbytes + sum(row['mask'].nbytes for row in masks)
            cost += sum(value.nbytes for value in search_masks.values())
            with self.lock:
                self.total += 1
                reference = self.reference(f'{self.total:08d}')
                if self.stopping.is_set() or self.pending + cost > self.pending_limit or self.queue.full():
                    self._gap(reference, 'closed' if self.stopping.is_set() else 'queue_capacity', at)
                    return {**reference, 'status': 'dropped'}
                self.pending += cost
                reserved = cost
            image = frame.copy()
            instances = [{**deepcopy({k: v for k, v in row.items() if k != 'mask'}),
                          'mask': row['mask'].copy()} for row in masks]
            searches = {key: value.copy() for key, value in search_masks.items()}
            item = (reference, image, at, instances, searches, deepcopy(metadata or {}), cost)
            with self.lock:
                if self.stopping.is_set():
                    self.pending -= cost
                    reserved = 0
                    self._gap(reference, 'closed_during_copy', at)
                    return {**reference, 'status': 'dropped'}
                self.queue.put_nowait(item)
                reserved = 0
            return {**reference, 'status': 'queued'}
        except Exception as exc:
            with self.lock:
                self.pending -= reserved
                self.errors.append('capture: ' + str(exc))
                if reference is not None:
                    self._gap(reference, 'capture_error', at)
            return {**(reference or self.reference()), 'status': 'capture_error'}

    def note(self, reference, details):
        try:
            value = plain(deepcopy(details))
            # Polling the same result must not fill the diagnostic ring.
            key = (reference.get('session'), reference.get('frame_id'), value.get('kind'), value.get('step_id'))
            with self.lock:
                self.notes[key] = {'reference': deepcopy(reference), **value}
                self.notes.move_to_end(key)
                while len(self.notes) > 256:
                    self.notes.popitem(last=False)
                    self.notes_evicted += 1
        except Exception as exc:
            with self.lock:
                self.errors.append('decision_note: ' + str(exc))

    def snapshot(self):
        with self.lock:
            return {'schema': 1, **self.reference(), 'created_at': self.created_at,
                    'context': deepcopy(self.context), 'complete': self.complete,
                    'closing': self.stopping.is_set(),
                    'limits': {'frames': self.max_frames, 'stored_bytes': self.max_bytes,
                               'pending_bytes': self.pending_limit},
                    'submitted_frames': self.total, 'pending_bytes': self.pending,
                    'retained_bytes': self.retained_bytes, 'dropped_frames': self.dropped,
                    'evicted_frames': self.evicted, 'notes_evicted': self.notes_evicted,
                    'frames': list(self.frames), 'gaps': list(self.gaps),
                    'decisions': list(self.notes.values()), 'errors': list(self.errors),
                    'format': 'Original algorithm-input BGR pixels as lossless PNG; exact masks in NPZ. No inference during saving.'}

    def _write_manifest(self):
        atomic_json(self.folder / 'manifest.json', self.snapshot())

    def _save(self, item):
        import cv2
        import numpy as np
        reference, image, at, masks, searches, metadata, _ = item
        ok, encoded = cv2.imencode('.png', image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise OSError('원본 판정 PNG 인코딩 실패')
        arrays = {}
        descriptions = []
        for index, row in enumerate(masks):
            key = f'ai_{index:03d}'
            mask = row['mask']
            arrays[key] = mask
            descriptions.append({'array': key, **plain({k: v for k, v in row.items() if k != 'mask'}),
                                 'sha256': hashlib.sha256(mask.tobytes()).hexdigest(),
                                 'shape': list(mask.shape), 'dtype': str(mask.dtype)})
        search_descriptions = []
        for index, (name, mask) in enumerate(searches.items()):
            key = f'search_{index:03d}'
            arrays[key] = mask
            search_descriptions.append({'array': key, 'name': name,
                                        'sha256': hashlib.sha256(mask.tobytes()).hexdigest(),
                                        'shape': list(mask.shape), 'dtype': str(mask.dtype)})
        buffer = io.BytesIO()
        np.savez_compressed(buffer, **arrays)
        image_bytes, mask_bytes = encoded.tobytes(), buffer.getvalue()
        document = {'schema': 1, **reference, 'captured_at': at,
                    'image': 'frame.png', 'masks': 'masks.npz',
                    'pixel_sha256': hashlib.sha256(image.tobytes()).hexdigest(),
                    'image_file_sha256': hashlib.sha256(image_bytes).hexdigest(),
                    'masks_file_sha256': hashlib.sha256(mask_bytes).hexdigest(),
                    'shape': list(image.shape), 'dtype': str(image.dtype),
                    'ai_instances': descriptions, 'search_masks': search_descriptions,
                    'metadata': plain(metadata)}
        encoded_metadata = json.dumps(document, ensure_ascii=False, allow_nan=False).encode()
        size = len(image_bytes) + len(mask_bytes) + len(encoded_metadata) + 4096
        if size > self.max_bytes:
            with self.lock:
                self._gap(reference, 'single_frame_capacity', at)
            return
        folder = self.folder / reference['frame_id']
        folder.mkdir()
        try:
            (folder / 'frame.png').write_bytes(image_bytes)
            (folder / 'masks.npz').write_bytes(mask_bytes)
            atomic_json(folder / 'frame.json', document)
            size = sum(p.stat().st_size for p in folder.iterdir())
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        with self.lock:
            self.frames.append({'frame_id': reference['frame_id'], 'captured_at': at,
                                'file': reference['frame_id'] + '/frame.json', 'bytes': size})
            self.retained_bytes += size
            retired = []
            while len(self.frames) > self.max_frames or self.retained_bytes > self.max_bytes:
                old = self.frames.popleft()
                self.retained_bytes -= old['bytes']
                self.evicted += 1
                self.gaps.append({'frame_id': old['frame_id'], 'captured_at': old['captured_at'],
                                  'reason': 'rolling_retention'})
                retired.append(old['frame_id'])
        for name in retired:
            shutil.rmtree(self.folder / name)

    def _run(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._save(item)
            except Exception as exc:
                with self.lock:
                    self.errors.append('storage: ' + str(exc))
                    self._gap(item[0], 'storage_error', item[2])
            finally:
                with self.lock:
                    self.pending -= item[-1]
                self.queue.task_done()
            try:
                self._write_manifest()
            except Exception as exc:
                with self.lock:
                    self.errors.append('manifest: ' + str(exc))
        with self.lock:
            self.complete = not self.errors and not self.dropped
        try:
            self._write_manifest()
        except Exception as exc:
            with self.lock:
                self.errors.append('final_manifest: ' + str(exc))
                self.complete = False

    def close(self, timeout=2.):
        self.stopping.set()
        self.thread.join(timeout)
        return self.snapshot()


def start(data_dir, context, *, stream):
    try:
        return DecisionEvidence(data_dir, context, stream=stream), None
    except Exception as exc:
        return None, str(exc)
