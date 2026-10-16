import base64,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import cv2,numpy as np
from so101_teach import failure_archive as archive
from so101_teach.failure_archive_client import fetch


def fixture(data,image_path=None):
    run_id='a'*32;folder=data/'diagnostics/action-failures'/('20261012-120000-'+run_id);folder.mkdir(parents=True)
    if image_path:(folder/'inspection.jpg').write_bytes(image_path.read_bytes())
    else:assert cv2.imwrite(str(folder/'inspection.jpg'),np.full((80,120,3),221,np.uint8))
    record={'failed_at':1791774000.,'error':'안착 불합격 · B 중단 중심 이탈 · 검증용 예시','context':{'run_id':run_id,'arm':'arm2','command':'build_b','phase':'startup_inspection'},'images':[{'file':'inspection.jpg','role':'inspection','age_at_failure_seconds':.3}]}
    (folder/'error.json').write_text(json.dumps(record,ensure_ascii=False));runs=data/'episode-cli-runs';runs.mkdir(exist_ok=True)
    (runs/(run_id+'.json')).write_text(json.dumps({'run_id':run_id,'events':[{'at':1791773998,'arm':'arm2','status':'B_JIG_CONFIRMED:carrier'},{'at':1791774000,'arm':'arm2','status':'B_FAILED:안착 불합격'}]},ensure_ascii=False))
    other='b'*32
    (runs/(other+'.json')).write_text(json.dumps({'run_id':other,'events':[{'at':1791773900,'arm':'arm3','status':'A_FAILED:카메라 연결 오류'}]},ensure_ascii=False))
    return 'evidence:'+folder.name


class FailureArchiveTests(unittest.TestCase):
    def test_new_evidence_and_legacy_failure_are_deduplicated_and_read_with_images(self):
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp);key=fixture(data);rows=archive.list_records(data)['records']
            self.assertEqual(len(rows),2);self.assertEqual(rows[0]['key'],key);self.assertEqual(rows[1]['arm'],'arm3')
            result=archive.read_record(data,key);self.assertEqual(result['record']['error'],rows[0]['error'])
            self.assertTrue(base64.b64decode(result['images'][0]['data']).startswith(b'\xff\xd8'))
            self.assertEqual(len(result['run']['events']),2);self.assertEqual(archive.read_record(data,rows[1]['key'])['images'],[])
    def test_missing_image_still_returns_log_and_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp);key=fixture(data);next(data.glob('diagnostics/action-failures/*/inspection.jpg')).unlink()
            result=archive.read_record(data,key);self.assertEqual(result['images'],[]);self.assertTrue(result['warnings']);self.assertIsNotNone(result['run'])
    def test_traversal_and_symlink_cannot_read_outside_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp);key=fixture(data)
            for invalid in ('evidence:../../etc','run:../profile','bad:key'):
                with self.assertRaises(ValueError):archive.read_record(data,invalid)
            p=next(data.glob('diagnostics/action-failures/*/inspection.jpg'));p.unlink();p.symlink_to(data/'episode-cli-runs'/('a'*32+'.json'))
            result=archive.read_record(data,key);self.assertFalse(result['images']);self.assertTrue(result['warnings'])
    def test_bad_record_does_not_hide_other_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            data=Path(temp);fixture(data);folder=data/'diagnostics/action-failures'/('20261012-120001-'+'c'*32);folder.mkdir();(folder/'error.json').write_text('{bad')
            result=archive.list_records(data);self.assertEqual(len(result['records']),2);self.assertEqual(result['skipped'],1)
    def test_local_source_does_not_start_ssh_or_devices(self):
        with tempfile.TemporaryDirectory() as temp,patch('so101_teach.failure_archive_client.subprocess.run') as call:
            fixture(Path(temp));self.assertEqual(len(fetch(temp,'local',{'operation':'list'})['records']),2);call.assert_not_called()
