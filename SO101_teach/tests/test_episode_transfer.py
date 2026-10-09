import fcntl
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import uuid
from tests.fixtures import DATA
from so101_teach.domain import EpisodeStore, load_profile, read_json, atomic_json
from so101_teach import episode_transfer_receiver as receiver
from so101_teach.episode_transfer import remote_request


class EpisodeTransferTests(unittest.TestCase):
    def library_request(self):
        request=deepcopy(self.request);request['transfer_id']=uuid.uuid4().hex;request.pop('slot');request['operation']='store_episode'
        request['expected_episode_sha256']=receiver.digest(self.episode)
        return request
    def test_episode_update_preserves_identity_bindings_and_global_settings(self):
        request=self.library_request();request['episode']['name']='수정된 에피소드'
        settings=read_json(self.root/'integration/execution-settings.json')
        result=receiver.store_episode(self.root,request)
        self.assertEqual(result['episode_id'],self.episode['id'])
        self.assertEqual(self.store.load(self.data/'episodes'/(self.episode['id']+'.json'))['name'],'수정된 에피소드')
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        self.assertEqual(read_json(self.root/'integration/execution-settings.json'),settings)
        self.assertEqual(read_json(Path(result['backup_directory'])/'previous-episode.json'),self.episode)
        self.assertTrue(receiver.store_episode(self.root,request)['already_applied'])
    def test_new_episode_is_stored_without_binding_and_conflicts_are_rejected(self):
        request=self.library_request();request['episode']['id']=uuid.uuid4().hex;request['expected_episode_sha256']=None
        self.assertFalse(receiver.store_episode(self.root,request)['linked_slots'])
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        request=self.library_request();request['expected_episode_sha256']='changed'
        with self.assertRaisesRegex(ValueError,'조회 이후'):receiver.store_episode(self.root,request)
    def test_library_explicit_settings_apply_preserves_bindings_and_rolls_back(self):
        request=self.library_request();request['episode']['name']='설정 포함'
        request['execution_settings']={'speed':350,'acquisition_seconds':4,'acquisition_attempts':2,'hold_seconds':8}
        before=read_json(self.root/'integration/execution-settings.json')
        from so101_teach import domain
        original=domain.atomic_json
        def write(path,value):
            if Path(path).name=='receipt.json':raise OSError('receipt failure')
            return original(path,value)
        with patch.object(domain,'atomic_json',side_effect=write):
            with self.assertRaises(OSError):receiver.store_episode(self.root,request)
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        self.assertEqual(read_json(self.root/'integration/execution-settings.json'),before)
        self.assertEqual(self.store.load(self.data/'episodes'/(self.episode['id']+'.json')),self.episode)
        result=receiver.store_episode(self.root,request)
        self.assertTrue(result['settings_applied'])
        after=read_json(self.root/'integration/recipes.json')
        for slot in ('A','B'):
            self.assertEqual(after['recipes']['arm2'][slot]['episode_id'],self.episode['id'])
            self.assertEqual(after['recipes']['arm2'][slot]['timeout_seconds'],120)
        self.assertEqual(read_json(self.root/'integration/execution-settings.json')['arm2']['speed'],350)

    def test_library_check_and_execution_lock_do_not_modify_episode(self):
        request=self.library_request();request['episode']['name']='미적용'
        self.assertTrue(receiver.store_episode(self.root,request,check_only=True)['check_only'])
        with (self.data/'episode-cli.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'실행 중'):receiver.store_episode(self.root,request)
        self.assertEqual(self.store.load(self.data/'episodes'/(self.episode['id']+'.json')),self.episode)

    def test_settings_are_applied_with_episode_and_rollback_on_recipe_failure(self):
        self.request['execution_settings']={'speed':350,'acquisition_seconds':4,'acquisition_attempts':2,'hold_seconds':8}
        before=read_json(self.root/'integration/execution-settings.json')
        from so101_teach import domain
        original=domain.atomic_json
        def write(path,value):
            if Path(path).name=='recipes.json' and value!=self.recipes:raise OSError('recipe write failed')
            return original(path,value)
        with patch.object(domain,'atomic_json',side_effect=write):
            with self.assertRaises(OSError):receiver.deliver(self.root,self.request)
        self.assertEqual(read_json(self.root/'integration/execution-settings.json'),before)
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        result=receiver.deliver(self.root,self.request)
        self.assertEqual(read_json(self.root/'integration/execution-settings.json')['arm2']['speed'],350)
        self.assertEqual(read_json(Path(result['backup_directory'])/'execution-settings-before.json'),before)
    def test_pull_reads_episodes_and_settings_without_writes(self):
        (self.root/'integration/episode_cli.py').write_text('# existing runner')
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=receiver.pull_target(self.root,'arm2')
        self.assertEqual(result['episodes'],[self.episode]);self.assertEqual(result['settings']['speed'],400)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_explicit_completion_requires_supported_runner_and_valid_step(self):
        path=self.root/'integration/episode_cli.py';path.write_text('# old runner')
        self.request['episode']['completion_events']={'LOWER':self.episode['steps'][0]['id']}
        with self.assertRaisesRegex(ValueError,'실행기 업데이트'):receiver.deliver(self.root,self.request)
        path.write_text('# completion_events supported')
        result=receiver.deliver(self.root,self.request)
        self.assertEqual(self.store.load(self.data/'episodes'/(result['episode_id']+'.json'))['completion_events'],self.request['episode']['completion_events'])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.data = self.root/'data';self.data.mkdir();(self.root/'integration').mkdir()
        profile = read_json(DATA/'profile.json');profile['robot_id']='arm2'
        atomic_json(self.data/'profile.json', profile);shutil.copytree(DATA/'calibration',self.data/'calibration')
        profile, cal, reference = load_profile(self.data)
        self.store=EpisodeStore(self.data/'episodes',cal,'arm2')
        self.episode=self.store.new('전송 에피소드');self.episode['steps']=[self.store.step(reference.middle,'자세 1')]
        self.store.save(self.episode)
        atomic_json(self.data/'jigs.json',{})
        self.recipes={'schema':1,'recipes':{'arm2':{'A':{'episode_id':self.episode['id'],'name':'기존 A','timeout_seconds':900},'B':{'episode_id':self.episode['id'],'name':'기존 B','timeout_seconds':900}},'arm3':{'A':{'episode_id':'c'*32,'name':'팔3 A','timeout_seconds':900}}}}
        atomic_json(self.root/'integration/recipes.json',self.recipes)
        atomic_json(self.root/'integration/execution-settings.json',{'arm2':{'speed':400,'acquisition_seconds':5,'acquisition_attempts':3}})
        target=receiver.inspect_target(self.root,'arm2')
        self.request={'operation':'deliver','arm':'arm2','slot':'A','transfer_id':uuid.uuid4().hex,'timeout_seconds':120,'expected_revision':target['revision'],'episode':deepcopy(self.episode)}
    def tearDown(self):self.temp.cleanup()
    def test_delivery_keeps_source_other_slots_and_backs_up_previous(self):
        result=receiver.deliver(self.root,self.request)
        after=read_json(self.root/'integration/recipes.json')
        self.assertEqual(after['recipes']['arm2']['A']['episode_id'],self.request['transfer_id'])
        self.assertEqual(after['recipes']['arm2']['B'],self.recipes['recipes']['arm2']['B'])
        self.assertEqual(after['recipes']['arm3'],self.recipes['recipes']['arm3'])
        self.assertEqual(self.store.load(self.data/'episodes'/(self.episode['id']+'.json')),self.episode)
        exported=self.store.load(self.data/'episodes'/(result['episode_id']+'.json'))
        self.assertEqual(exported['steps'],self.episode['steps']);self.assertEqual(exported['pi_export']['source_episode_id'],self.episode['id'])
        backup=Path(result['backup_directory'])
        self.assertEqual(read_json(backup/'recipes-before.json'),self.recipes)
        self.assertEqual(read_json(backup/'previous-episode.json'),self.episode)
    def test_retry_same_delivery_is_idempotent(self):
        receiver.deliver(self.root,self.request)
        self.assertTrue(receiver.deliver(self.root,self.request)['already_applied'])
        self.assertEqual(len(list((self.data/'episode-transfer-backups').iterdir())),1)
    def test_readonly_check_does_not_create_episode_or_change_recipe(self):
        result=receiver.deliver(self.root,self.request,check_only=True)
        self.assertTrue(result['check_only'])
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        self.assertFalse((self.data/'episodes'/(self.request['transfer_id']+'.json')).exists())
    def test_rejects_wrong_arm_calibration_empty_and_missing_jig_without_writes(self):
        changes=[{'robot_id':'arm3'},{'calibration_sha256':'0'*64},{'steps':[]}]
        step=deepcopy(self.episode['steps'][0]);step.update(jig_id='missing',jig_reference={'pose':[0,0,0],'symmetry_deg':90,'stl_sha256':'a'*64})
        changes.append({'steps':[step]})
        for change in changes:
            with self.subTest(change=list(change)):
                request=deepcopy(self.request);request['episode'].update(change)
                with self.assertRaises(ValueError):receiver.deliver(self.root,request)
                self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
        self.assertFalse((self.data/'episode-transfer-backups').exists())
    def test_busy_runner_blocks_delivery(self):
        with (self.data/'episode-cli.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError,'작업이 실행 중'):receiver.deliver(self.root,self.request)
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
    def test_target_change_requires_requery(self):
        edited=deepcopy(self.recipes);edited['recipes']['arm2']['B']['name']='다른 창 수정'
        atomic_json(self.root/'integration/recipes.json',edited)
        with self.assertRaisesRegex(ValueError,'다시 조회'):receiver.deliver(self.root,self.request)
        self.assertEqual(read_json(self.root/'integration/recipes.json'),edited)
    def test_write_failure_before_binding_preserves_old_recipe(self):
        with patch.object(EpisodeStore,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):receiver.deliver(self.root,self.request)
        self.assertEqual(read_json(self.root/'integration/recipes.json'),self.recipes)
    def test_arm3_uses_its_own_store(self):
        data=self.data/'arm3-runtime';shutil.copytree(self.data,data,ignore=shutil.ignore_patterns('arm3-runtime'))
        profile=read_json(data/'profile.json');profile['robot_id']='arm3';atomic_json(data/'profile.json',profile)
        settings=read_json(self.root/'integration/execution-settings.json');settings['arm3']=settings['arm2'];atomic_json(self.root/'integration/execution-settings.json',settings)
        request=deepcopy(self.request);request['arm']='arm3';request['episode']['robot_id']='arm3'
        request['expected_revision']=receiver.inspect_target(self.root,'arm3')['revision']
        result=receiver.deliver(self.root,request)
        self.assertTrue((data/'episodes'/(result['episode_id']+'.json')).exists())
        self.assertFalse((self.data/'episodes'/(result['episode_id']+'.json')).exists())
    def test_transport_uses_stdin_and_does_not_hide_unknown_outcome(self):
        config={'host':'127.0.0.1','user':'tester','app_dir':'/tmp/path with spaces','python':'python3'}
        with patch('subprocess.run',return_value=SimpleNamespace(stdout='{"ok":true,"value":{"arm":"arm2"}}',returncode=0)) as run:
            self.assertEqual(remote_request(config,{'operation':'inspect','arm':'arm2'}),{'arm':'arm2'})
            self.assertIn('input',run.call_args.kwargs);self.assertNotIn('shell',run.call_args.kwargs)
        import subprocess
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('ssh',25)):
            with self.assertRaisesRegex(ValueError,'적용 여부'):remote_request(config,self.request)


if __name__=='__main__':unittest.main()
