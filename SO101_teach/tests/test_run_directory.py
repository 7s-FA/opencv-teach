from pathlib import Path
import json,os,tempfile,unittest
from unittest.mock import patch
from so101_teach.domain import atomic_json
from so101_teach.run_directory import RunDirectoryWatch
from integration.work_monitor import Events

class RunDirectoryTests(unittest.TestCase):
    def test_atomic_and_inplace_updates_are_observed_without_history_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);watch=RunDirectoryWatch(root);self.addCleanup(watch.close)
            self.assertTrue(watch.poll()[1])
            if watch.fd is None:self.skipTest('inotify unavailable; full-scan fallback retained')
            a=root/'a.json';atomic_json(a,{'n':1});changed,rescan=watch.poll();self.assertIn(a,changed);self.assertFalse(rescan)
            a.write_text('{"n":2}');self.assertIn(a,watch.poll()[0])
            self.assertEqual(watch.poll(),(set(),False))
            a.unlink();self.assertTrue(watch.poll()[1])
    def test_directory_recreation_and_unavailable_backend_request_rescan(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'runs';watch=RunDirectoryWatch(root);self.addCleanup(watch.close)
            self.assertTrue(watch.poll()[1]);root.mkdir();self.assertTrue(watch.poll()[1]);watch.close()
            with patch('so101_teach.run_directory.ctypes.CDLL',side_effect=OSError('unsupported')):
                self.assertTrue(watch.poll()[1]);self.assertTrue(watch.poll()[1])
    def test_partial_file_retries_without_another_change_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True);path=runs/'a.json';path.write_text('{')
            events=Events(root);self.assertTrue(events.read_new()[1])
            path.write_text(json.dumps({'events':[{'at':1,'arm':'arm2','status':'A_ACCEPTED'}]}))
            # The retry set must suffice even if a platform loses a notification.
            with patch.object(events.changes,'poll',return_value=(set(),False)):
                rows,errors=events.read_new();self.assertFalse(errors);self.assertEqual(len(rows),1)
    def test_unchanged_history_does_not_stat_every_old_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True)
            for n in range(100):atomic_json(runs/(f'{n:032x}.json'),{'events':[{'at':n,'arm':'arm2','status':'A_ACCEPTED'}]})
            events=Events(root);self.assertEqual(len(events.read_new()[0]),100)
            if events.changes.fd is None:self.skipTest('inotify unavailable')
            with patch.object(Path,'glob',side_effect=AssertionError('full history rescan')):
                self.assertEqual(events.read_new(),([],[]))

class MonitorOrderingTests(unittest.TestCase):
    def test_equal_timestamp_selection_keeps_existing_directory_order(self):
        from desktop_monitor.source import Source
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True)
            for n in range(60,0,-1):
                path=runs/(f'{n:032x}.json');atomic_json(path,{'events':[{'at':n,'status':'A_ACCEPTED'}]})
                os.utime(path,ns=(1700000000000000000,1700000000000000000))
            source=Source(root);self.addCleanup(source.run_changes.close)
            def expected():return sorted(runs.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)[:48]
            source.records({});self.assertEqual(source.paths,expected())
            new=runs/('f'*32+'.json');atomic_json(new,{'events':[{'at':61,'status':'A_ACCEPTED'}]})
            os.utime(new,ns=(1700000000000000000,1700000000000000000))
            source.scan_at=0;source.records({});self.assertEqual(source.paths,expected())
            if source.run_changes.fd is not None:
                source.scan_at=0
                with patch.object(Path,'glob',side_effect=AssertionError('unchanged tied history must not rescan')):source.records({})
    def test_backdated_run_refills_recent_window(self):
        from desktop_monitor.source import Source
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True)
            for n in range(60):
                path=runs/(f'{n:032x}.json');atomic_json(path,{'events':[{'at':n,'status':'A_ACCEPTED'}]})
                os.utime(path,ns=(1700000000000000000+n*1000000,1700000000000000000+n*1000000))
            source=Source(root);self.addCleanup(source.run_changes.close);source.records({})
            os.utime(source.paths[0],ns=(1600000000000000000,1600000000000000000))
            source.scan_at=0;source.records({})
            expected=sorted(runs.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True)[:48]
            self.assertEqual(source.paths,expected)
    def test_same_stamp_and_size_rewrite_refreshes_visible_row(self):
        from desktop_monitor.source import Source
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True);path=runs/('a'*32+'.json')
            def save(status):
                path.write_text(json.dumps({'run_id':path.stem,'events':[{'at':1,'arm':'arm2','status':status}]}))
                os.utime(path,ns=(1700000000000000000,1700000000000000000))
            save('A_STARTED');source=Source(root);self.addCleanup(source.run_changes.close);source.records({})
            if source.run_changes.fd is None:self.skipTest('inotify unavailable')
            save('B_STARTED');source.scan_at=0
            self.assertEqual(source.records({})[0][0]['status'],'B_STARTED')

    def test_same_timestamp_shorter_log_still_resets_terminal_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);runs=root/'data/episode-cli-runs';runs.mkdir(parents=True);path=runs/'first.json'
            def save(statuses):
                path.write_text(json.dumps({'events':[{'at':i,'arm':'arm2','status':s} for i,s in enumerate(statuses)]}))
                os.utime(path,ns=(1700000000000000000,1700000000000000000))
            save(['A_STARTED','A_DONE']);events=Events(root);self.addCleanup(events.changes.close);events.read_new()
            if events.changes.fd is None:self.skipTest('inotify unavailable')
            save(['B_STARTED']);self.assertEqual(events.read_new()[0][0]['status'],'B_STARTED')
