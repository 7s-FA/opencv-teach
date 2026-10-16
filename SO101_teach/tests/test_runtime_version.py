"""Desktop independence, fail-closed runtime identity and read-only preflight."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from so101_teach import runtime_version as version
from so101_teach.remote_client import server_version_supported


class RuntimeVersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'app';self.root.mkdir()
        for name in version.REQUIRED_ASSETS:self.write(name,b'asset')
        self.write('so101_teach/motion.py',b'SPEED=300\n')
        self.write('so101_teach/ui.py',b'LABEL="old"\n')
        self.write('so101_teach/preview.py',b'FPS=30\n')
        self.write('so101_teach/mujoco_finish.py',b'GLOSS=.2\n')
        self.write('integration/episode_cli.py',b'PROTOCOL=6\n')
    def write(self,name,raw):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
    def test_desktop_and_render_changes_keep_runtime_identity(self):
        before=version.manifest(self.root)
        for name in ('ui','preview','mujoco_finish'):
            self.write('so101_teach/'+name+'.py',b'UPDATED=True\n')
        self.assertEqual(version.manifest(self.root),before)
        version.validate_boundaries(self.root)
    def test_control_new_modules_and_deleted_modules_change_identity(self):
        before=version.manifest(self.root)
        self.write('so101_teach/motion.py',b'SPEED=500\n')
        self.assertEqual(version.difference(before,version.manifest(self.root)),['so101_teach/motion.py'])
        self.write('so101_teach/new_control.py',b'VALUE=1\n')
        self.assertIn('so101_teach/new_control.py',version.difference(before,version.manifest(self.root)))
        (self.root/'integration/episode_cli.py').unlink()
        self.assertIn('integration/episode_cli.py',version.difference(before,version.manifest(self.root)))
    def test_inspection_and_model_assets_are_part_of_contract(self):
        before=version.manifest(self.root)
        self.write('inspection/shape_templates.npz',b'changed geometry')
        self.write('assets/so101/so101_modified.urdf',b'changed joint coordinates')
        self.assertEqual(set(version.difference(before,version.manifest(self.root))),
            {'inspection/shape_templates.npz','assets/so101/so101_modified.urdf'})
    def test_identity_is_independent_of_install_path_and_machine(self):
        other=Path(self.tmp.name)/'other';shutil.copytree(self.root,other)
        self.assertEqual(version.manifest(self.root),version.manifest(other))
    def test_shared_import_of_desktop_code_is_rejected(self):
        for source in (b'from . import ui\n',b'from .preview import Renderer\n',b'import so101_teach.ui\n'):
            self.write('so101_teach/motion.py',source)
            with self.assertRaisesRegex(ValueError,'화면 전용'):version.validate_boundaries(self.root)
    def test_missing_required_asset_is_not_silently_ignored(self):
        (self.root/version.REQUIRED_ASSETS[0]).unlink()
        with self.assertRaises(FileNotFoundError):version.manifest(self.root)
    def test_runtime_metadata_overrides_legacy_full_hash_and_rejects_bad_protocol(self):
        original=version.manifest(self.root)
        with patch('so101_teach.remote_client.ROOT',self.root):
            self.assertTrue(server_version_supported('different',1,runtime=original))
            self.assertFalse(server_version_supported('same',2,current='same',runtime=original))
            self.write('so101_teach/motion.py',b'SPEED=500\n')
            self.assertFalse(server_version_supported('same',1,current='same',runtime=original))
            for broken in (None,{},dict(original,schema=999),dict(original,sha256='0'*64)):
                self.assertFalse(server_version_supported('same',1,current='same',runtime=broken))
    def test_check_worker_does_not_acquire_devices_or_mutate_files(self):
        path=Path(__file__).resolve().parents[1]/'tools/pi_runtime_remote.py'
        spec=importlib.util.spec_from_file_location('pi_runtime_worker_test',path)
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        before={str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch.object(worker,'urlopen',side_effect=AssertionError('No server metadata: no HTTP needed')),patch.object(worker.subprocess,'run',side_effect=AssertionError('No processes in check')):
            result=worker.run({'operation':'check','root':str(self.root),'manifest_source':Path(version.__file__).read_text(),'expected':version.manifest(self.root)})
        self.assertFalse(result['changed_files']);self.assertFalse(any(s['running'] for s in result['servers'].values()))
        self.assertEqual(before,{str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_nonimportable_integration_backups_do_not_require_deployment(self):
        before=version.manifest(self.root)
        self.write('integration/episode_cli.before_20261013.py',b'old backup, not Python')
        self.assertEqual(version.manifest(self.root),before)
        version.validate_boundaries(self.root)

    def worker(self):
        path=Path(__file__).resolve().parents[1]/'tools/pi_runtime_remote.py'
        spec=importlib.util.spec_from_file_location('pi_runtime_worker_safety_test',path)
        worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
        return worker

    def test_sync_refuses_active_device_before_acquiring_or_writing_code(self):
        worker=self.worker();before=version.manifest(self.root)
        self.write('data/remote-server.json',json.dumps({'pid':123,'port':8765,'instance':'test'}).encode())
        self.write('data/remote-token',b'fixture-secret')
        endpoints=[]
        def response(req,timeout):
            endpoint=req.full_url.rsplit('/',1)[1];endpoints.append(endpoint)
            value=({'instance':'test','version':'build','protocol':1,'runtime':before} if endpoint=='health' else
                {'detached':True,'follower':{'running':True},'leader':None,'calibration':None} if endpoint=='state' else {'running':False})
            import io
            return io.BytesIO(json.dumps({'ok':True,'value':value}).encode())
        with patch.object(worker,'urlopen',side_effect=response),patch.object(worker.subprocess,'run',side_effect=AssertionError('No process changes')):
            with self.assertRaisesRegex(RuntimeError,'강제로 끊지'):
                worker.run({'operation':'apply','root':str(self.root),'manifest_source':Path(version.__file__).read_text(),'expected':before,'observed_sha256':before['sha256'],'files':{}})
        self.assertNotIn('acquire',endpoints);self.assertEqual(version.manifest(self.root),before)

    def test_failed_activation_restores_changed_code_and_removes_new_file(self):
        import base64
        worker=self.worker();before=version.manifest(self.root)
        old=(self.root/'so101_teach/motion.py').read_bytes()
        self.write('so101_teach/motion.py',b'SPEED=500\n');self.write('so101_teach/new_control.py',b'VALUE=1\n')
        expected=version.manifest(self.root)
        files={n:base64.b64encode((self.root/n).read_bytes()).decode() for n in ('so101_teach/motion.py','so101_teach/new_control.py')}
        self.write('so101_teach/motion.py',old);(self.root/'so101_teach/new_control.py').unlink()
        def fail_start(args,**kwargs):
            return SimpleNamespace(returncode=1,stderr='intentional activation failure')
        with patch.object(worker.subprocess,'run',side_effect=fail_start):
            with self.assertRaisesRegex(RuntimeError,'시작 실패'):
                worker.run({'operation':'apply','root':str(self.root),'manifest_source':Path(version.__file__).read_text(),'expected':expected,'observed_sha256':before['sha256'],'files':files})
        self.assertEqual(version.manifest(self.root),before)
        self.assertTrue(list((self.root/'verification').glob('runtime-sync-*/rollback.json')))

    def test_ensure_keeps_existing_runtime_after_desktop_only_changes(self):
        import contextlib,io
        from so101_teach import remote_server
        before=version.manifest(self.root)
        self.write('data/remote-token',b'fixture-token')
        self.write('so101_teach/ui.py',b'LABEL="new"\n')
        payload={'ok':True,'value':{'runtime':before,'version':'old-desktop-build','protocol':1,'instance':'same'}}
        with patch.object(remote_server,'ROOT',self.root),patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(payload).encode())),patch.object(remote_server,'stop_stale_idle_server') as stop,contextlib.redirect_stdout(io.StringIO()) as output:
            remote_server.ensure(8765)
        stop.assert_not_called();self.assertEqual(json.loads(output.getvalue())['instance'],'same')

    def test_ensure_detects_shared_code_changed_since_process_start(self):
        import io
        from so101_teach import remote_server
        before=version.manifest(self.root)
        self.write('data/remote-token',b'fixture-token');self.write('so101_teach/motion.py',b'SPEED=500\n')
        payload={'ok':True,'value':{'runtime':before,'version':'old-build','protocol':1,'instance':'same'}}
        with patch.object(remote_server,'ROOT',self.root),patch('urllib.request.urlopen',side_effect=lambda *a,**k:io.BytesIO(json.dumps(payload).encode())),patch.object(remote_server,'stop_stale_idle_server',side_effect=RuntimeError('stale shared code')) as stop:
            with self.assertRaisesRegex(RuntimeError,'stale shared code'):remote_server.ensure(8765)
        stop.assert_called_once()

if __name__=='__main__':unittest.main()
