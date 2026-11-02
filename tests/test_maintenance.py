from pathlib import Path
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import maintenance


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=maintenance.SharedStore(self.root)

    def test_atomic_queue_and_strict_actions(self):
        with patch.object(maintenance,'DATA_ROOT',self.root):
            self.store.write('status.json',{'heartbeat':__import__('time').time(),'busy':False})
            job=maintenance.enqueue('upgrade')
            self.assertEqual(self.store.read(job['id']+'.json',section='jobs')['kind'],'upgrade')
            with self.assertRaises(ValueError):maintenance.enqueue('rm -rf /')
            with self.assertRaises(ValueError):maintenance.enqueue('rollback')

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'Linux directory-fd safety')
    def test_symlinks_cannot_escape_worker_data_directory(self):
        outside=self.root/'outside';outside.mkdir()
        (self.root/'maintenance').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(OSError):self.store.write('status.json',{'secret':'no'})
        self.assertFalse((outside/'status.json').exists())

    @unittest.skipUnless(hasattr(os,'O_NOFOLLOW'),'Linux directory-fd safety')
    def test_job_file_symlink_is_rejected(self):
        self.store.write('status.json',{})
        private=self.root/'private';private.write_text('{"secret":1}')
        (self.root/'maintenance/settings.json').symlink_to(private)
        with self.assertRaises(OSError):self.store.read('settings.json')
        self.assertEqual(private.read_text(),'{"secret":1}')

    def test_checks_gate_the_exact_main_commit(self):
        sha='a'*40
        commit={'sha':sha,'commit':{'tree':{'sha':'b'*40},'message':'update','committer':{'date':'2026-11-02T00:00:00Z'}}}
        runs={'workflow_runs':[{'path':'.github/workflows/security.yml','head_sha':sha,'head_branch':'main','conclusion':'failure'}]}
        with patch.object(maintenance,'api',side_effect=[commit,runs]):self.assertFalse(maintenance.latest_release()['ready'])
        runs['workflow_runs'][0]['conclusion']='success'
        with patch.object(maintenance,'api',side_effect=[commit,runs]):self.assertTrue(maintenance.latest_release()['ready'])

    def test_github_download_falls_back_on_transport_failure(self):
        import urllib.error
        with patch.object(maintenance.urllib.request,'build_opener') as opener,patch.object(maintenance.urllib.request,'urlopen',return_value='proxy-response') as proxy:
            opener.return_value.open.side_effect=urllib.error.URLError('TLS EOF')
            self.assertEqual(maintenance.open_url('https://api.github.com/anything'),'proxy-response')
            proxy.assert_called_once()

    def test_github_permission_and_rate_limit_failures_are_not_retried(self):
        import urllib.error
        with patch.object(maintenance.urllib.request,'build_opener') as opener,patch.object(maintenance.urllib.request,'urlopen') as proxy:
            opener.return_value.open.side_effect=urllib.error.HTTPError('https://api.github.com',403,'rate limit',{},None)
            with self.assertRaises(urllib.error.HTTPError):maintenance.open_url('https://api.github.com/anything')
            proxy.assert_not_called()

    def test_auto_sync_switch_is_a_boolean(self):
        with patch.object(maintenance,'DATA_ROOT',self.root):
            with self.assertRaises(ValueError):maintenance.set_auto_sync('false')
            self.assertFalse(maintenance.set_auto_sync(False)['autoSync'])


class WorkerInputTests(unittest.TestCase):
    def worker_module(self):
        spec=importlib.util.spec_from_file_location('worker',ROOT/'scripts/maintenance_worker.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def test_source_archive_cannot_overwrite_runtime_secrets(self):
        module=self.worker_module()
        for name in ['.env','mtproto/config.py','proxy/config.yaml','data/traffic.sqlite3','../etc/passwd','/etc/passwd','.git/config']:
            self.assertFalse(module.allowed_source(name),name)
        for name in ['app.py','proxy/config.example.yaml','mtproto/traffic_accounting.py']:
            self.assertTrue(module.allowed_source(name),name)

    def test_source_backup_and_install_do_not_follow_proxy_symlinks(self):
        module=self.worker_module()
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);project=root/'project';(project/'proxy').mkdir(parents=True)
            private=root/'private';private.write_text('host-private')
            link=project/'proxy/config.example.yaml';link.symlink_to(private)
            with self.assertRaises(OSError):module.read_source(project,'proxy/config.example.yaml')
            module.write_source(project,'proxy/config.example.yaml',b'safe-template',0o644)
            self.assertEqual(private.read_text(),'host-private')
            self.assertFalse(link.is_symlink());self.assertEqual(link.read_text(),'safe-template')

    def test_failed_ci_does_not_build_or_change_services(self):
        module=self.worker_module()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ):
            root=Path(folder);project=root/'project';project.mkdir();(project/'.env').write_text('ADMIN_PASSWORD=test-password\n')
            worker=module.Worker(project,root/'private',maintenance)
            with patch.object(worker,'run') as run,patch.object(maintenance,'latest_release',return_value={'ready':False,'commit':'a'*40}):
                worker.process({'id':'a'*32,'kind':'upgrade','state':'queued','requestedAt':'2026-11-02T00:00:00Z'})
                run.assert_not_called()
                result=worker.shared.read('a'*32+'.json',section='jobs')
                self.assertEqual(result['state'],'failed')
            with self.assertRaises(ValueError):worker.process({'id':'b'*32,'kind':'upgrade','state':'queued','requestedAt':'now','command':'anything'})

    def test_interrupted_jobs_are_released_and_partial_apply_triggers_rollback(self):
        module=self.worker_module()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ):
            root=Path(folder);project=root/'project';project.mkdir();(project/'.env').write_text('ADMIN_PASSWORD=test-password\n')
            worker=module.Worker(project,root/'private',maintenance)
            worker.shared.write('a'*32+'.json',{'id':'a'*32,'state':'running'},'jobs')
            worker.save_private('apply.json',{'pending':True})
            def restore():worker.save_private('apply.json',{'pending':False})
            with patch.object(worker,'rollback',side_effect=restore) as rollback:
                worker.recover_interrupted();rollback.assert_called_once()
            self.assertEqual(worker.shared.read('a'*32+'.json',section='jobs')['state'],'failed')
            self.assertFalse(worker.private('apply.json')['pending'])

    def test_rollback_preserves_history_and_pauses_auto_sync(self):
        module=self.worker_module()
        with tempfile.TemporaryDirectory() as folder,patch.dict(os.environ):
            root=Path(folder);project=root/'project';project.mkdir();(project/'.env').write_text('ADMIN_PASSWORD=test-password\n')
            worker=module.Worker(project,root/'private',maintenance)
            data=project/'data';data.mkdir();(data/'traffic.sqlite3').write_bytes(b'preserved-history')
            (project/'app.py').write_text('new-version')
            backup=worker.state/'backups'/'old';(backup/'source').mkdir(parents=True)
            (backup/'source/app.py').write_text('old-version');(backup/'images.json').write_text('{}');(backup/'release.json').write_text('{"commit":"old"}')
            worker.save_private('rollback.json',{'directory':str(backup)})
            with patch.object(worker,'compose'),patch.object(worker,'health'):
                worker.rollback()
            self.assertEqual((project/'app.py').read_text(),'old-version')
            self.assertEqual((data/'traffic.sqlite3').read_bytes(),b'preserved-history')
            self.assertFalse(worker.shared.read('settings.json')['autoSync'])


if __name__=='__main__':unittest.main()
