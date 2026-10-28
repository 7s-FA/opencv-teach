from copy import deepcopy
import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from so101_teach.domain import atomic_json,read_json
from so101_teach.execution_records import RunJournal,RunReader,read_run,changes,apply
from tests.test_episode_cli import cli

class JournalTests(unittest.TestCase):
    def test_exact_deltas_preserve_nested_updates_deletes_append_and_truncation(self):
        old={'events':[{'n':0}],'analysis':{'x':[1,2,3],'remove':1},'unchanged':{'value':1}}
        new={'events':[{'n':0},{'n':1}],'analysis':{'x':[4,2],'add':True},'unchanged':old['unchanged']}
        result=old
        for operation in changes(old,new):result=apply(result,operation)
        self.assertEqual(result,new);self.assertEqual(old['analysis']['x'],[1,2,3]);self.assertIn('remove',old['analysis'])
    def test_incremental_and_cold_readers_match_every_committed_revision(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('a'*32+'.json');writer=RunJournal(path);reader=RunReader()
            doc={'schema_version':2,'record_kind':'execution','run_id':path.stem,'job_id':'b'*32,'events':[],'analysis':{}}
            snapshots=[]
            for n in range(12):
                doc=deepcopy(doc);doc['events'].append({'status':str(n),'at':n,'arm':'arm2'});doc['analysis']={'count':n,'mutable':[n,{'x':True}]}
                writer.append(doc,atomic_json);snapshots.append(reader.read(path,copy=False))
                self.assertEqual(snapshots[-1],doc);self.assertEqual(read_run(path),doc);self.assertEqual(read_json(path),doc)
            self.assertEqual(len(snapshots[0]['events']),1);self.assertEqual(snapshots[0]['analysis']['count'],0)
            writer.materialize(atomic_json);self.assertEqual(json.loads(path.read_text()),doc)
            doc=deepcopy(doc);doc['analysis']['count']=20;writer.append(doc,atomic_json);self.assertEqual(reader.read(path),doc)
    def test_uncommitted_or_torn_tail_is_not_reported_as_a_new_event(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('a'*32+'.json');w=RunJournal(path);doc={'run_id':path.stem,'job_id':'b'*32,'events':[1]};w.append(doc,atomic_json)
            with w.journal.open('ab') as f:f.write(b'{"partial":')
            self.assertEqual(read_run(path),doc)
            manifest=json.loads(path.read_text());manifest['revision']=2;atomic_json(path,manifest)
            with self.assertRaisesRegex(ValueError,'잘렸'):read_run(path)
    def test_hash_corruption_and_outside_journal_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('a'*32+'.json');w=RunJournal(path);w.append({'run_id':path.stem,'job_id':'b'*32,'events':[1]},atomic_json)
            raw=w.journal.read_text();w.journal.write_text(raw.replace('"events":[1]','"events":[2]'))
            with self.assertRaisesRegex(ValueError,'해시'):read_run(path)
            manifest=json.loads(path.read_text());manifest['journal']='../secret';atomic_json(path,manifest)
            with self.assertRaisesRegex(ValueError,'색인'):read_run(path)
    def test_existing_journal_cannot_be_overwritten_by_fresh_writer(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('a'*32+'.json');doc={'run_id':path.stem,'job_id':'b'*32,'events':[]}
            RunJournal(path).append(doc,atomic_json)
            with self.assertRaises(FileExistsError):RunJournal(path).append(doc,atomic_json)
    def test_even_a_matching_hash_cannot_authorize_malformed_changes(self):
        import hashlib
        from so101_teach.execution_records import encoded
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/('a'*32+'.json');w=RunJournal(path);w.append({'run_id':path.stem,'job_id':'b'*32,'events':[]},atomic_json)
            for delta in ({'unexpected':True},[[]],[['delete',['missing'],None]]):
                row={'sequence':1,'previous':'','changes':delta};digest=hashlib.sha256(encoded(row)).hexdigest()
                w.journal.write_bytes(encoded({**row,'sha256':digest})+b'\n')
                manifest=json.loads(path.read_text());manifest['sha256']=digest;atomic_json(path,manifest)
                with self.assertRaises(ValueError):read_run(path)
    def test_status_readers_and_export_keep_original_record_contract(self):
        from integration.action_protocol import job_events
        from integration.work_monitor import Events
        from desktop_monitor.source import Source
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)):
            root=Path(folder);(root/'data').mkdir();status=cli.Status('arm2','B',False)
            try:
                for n in range(40):status.publish('STEP_DONE:'+str(n))
                status.flush_records(materialize=False)
                self.assertEqual(json.loads(status.log.read_text())['record_kind'],'execution_journal')
                doc=read_run(status.log);self.assertEqual(doc['events'],status.events)
                self.assertEqual(job_events(root,{'run_id':status.run_id}),status.events)
                events,errors=Events(root).read_new();self.assertFalse(errors);self.assertEqual(events,status.events)
                source=Source(root);events,runs,errors=source.records({});self.assertFalse(errors);self.assertEqual(len(events),40)
                status.flush_records();self.assertEqual(json.loads(status.log.read_text()),doc)
            finally:status.close()
    def test_durable_append_failure_blocks_later_success(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(cli,'ROOT',Path(folder)):
            root=Path(folder);(root/'data').mkdir();status=cli.Status('arm2','B',False)
            try:
                with patch('so101_teach.execution_records.RunJournal.append',side_effect=OSError('disk full')):
                    with self.assertRaisesRegex(OSError,'disk full'):
                        for n in range(40):status.publish('STEP_DONE:'+str(n))
                with self.assertRaisesRegex(OSError,'disk full'):status.publish('DONE')
            finally:
                with self.assertRaises(OSError):status.close()
