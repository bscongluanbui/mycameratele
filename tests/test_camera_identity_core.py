"""Synthetic DHCP/MAC rebinding; no real cameras or credentials are contacted."""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from archive_app.core import Archive, Settings
from archive_app.sd_source import SDSource, SDSourceError


SUBNET='192.168.2.0/24'
MAC_A='02:00:00:00:00:01'
MAC_B='02:00:00:00:00:02'
MAC_C='02:00:00:00:00:03'


class CameraIdentityCoreTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=parent/('.tmp-mac-core-'+uuid.uuid4().hex)
        self.root.mkdir()
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0)
        self.archive=Archive(self.settings)

    def tearDown(self):
        self.archive.close()
        shutil.rmtree(self.root)

    def add(self,slug='one',host='192.168.2.5',mac=MAC_A,**extra):
        return self.archive.add_camera({'id':slug,'name':slug,'host':host,
            'mac_address':mac,'auto_ip':True,'identity_subnet':SUBNET,**extra})

    def camera(self,slug='one'):
        return next(row for row in self.archive.cameras() if row['id']==slug)

    def apply(self,mapping,stamp=None):
        return self.archive.apply_camera_identity(SUBNET,mapping,time.time() if stamp is None else stamp)

    def test_existing_manual_add_remains_unchanged(self):
        camera=self.archive.add_camera({'id':'manual','host':'192.168.2.5'})
        self.assertFalse(camera['auto_ip'])
        self.assertEqual(camera['mac_address'],'')
        self.assertEqual(camera['identity_subnet'],'')
        self.assertEqual(camera['identity_status'],'manual')
        self.assertEqual(self.apply({MAC_A:'192.168.2.8'})['updated'],[])
        self.assertEqual(self.camera('manual')['host'],'192.168.2.5')

    def test_normalizes_mac_and_subnet_on_add(self):
        camera=self.add(mac=MAC_A.upper().replace(':','-'),identity_subnet='192.168.2.29/24')
        self.assertEqual(camera['mac_address'],MAC_A)
        self.assertEqual(camera['identity_subnet'],SUBNET)
        self.assertTrue(camera['auto_ip'])
        self.assertEqual(camera['identity_status'],'pending')

    def test_auto_ip_requires_complete_identity(self):
        for field,value in [('mac_address',''),('identity_subnet',''),('host',''),
                            ('host','camera.local'),('host','192.168.3.5'),
                            ('host','192.168.2.0'),('host','192.168.2.255')]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):
                self.add(**{field:value})

    def test_rejects_nonboolean_auto_ip(self):
        for value in (1,0,'true',None):
            with self.subTest(value=value),self.assertRaises(ValueError):self.add(auto_ip=value)

    def test_bad_mac_and_subnet_rejected(self):
        for value in ('00:00:00:00:00:00','ff:ff:ff:ff:ff:ff','01:00:00:00:00:01','invalid',123):
            with self.subTest(mac=value),self.assertRaises(ValueError):self.add(mac=value)
        for value in ('8.8.8.0/24','192.168.0.0/16','192.168.2.2/31','192.168.2.5','fd00::/64',123):
            with self.subTest(subnet=value),self.assertRaises(ValueError):self.add(identity_subnet=value)

    def test_partial_edit_validates_merged_binding(self):
        self.add()
        for data in ({'mac_address':''},{'identity_subnet':''},{'host':'192.168.4.5'}):
            with self.subTest(data=data),self.assertRaises(ValueError):self.archive.update_camera('one',data)
        self.assertEqual(self.camera()['host'],'192.168.2.5')
        self.assertEqual(self.archive.update_camera('one',{'name':'New'})['name'],'New')

    def test_partial_enable_accepts_existing_complete_fields(self):
        self.add(auto_ip=False)
        camera=self.archive.update_camera('one',{'auto_ip':True})
        self.assertTrue(camera['auto_ip'])
        self.assertEqual(camera['identity_status'],'pending')

    def test_duplicate_active_mac_in_same_subnet_is_rejected(self):
        self.add()
        with self.assertRaises(ValueError):self.add('two','192.168.2.6',MAC_A)
        self.add('two','192.168.2.6',MAC_B)
        with self.assertRaises(ValueError):self.archive.update_camera('two',{'mac_address':MAC_A})

    def test_same_mac_on_another_subnet_is_separate_identity(self):
        self.add()
        self.add('two','192.168.3.5',MAC_A,identity_subnet='192.168.3.0/24')
        self.assertEqual(len(self.archive.cameras()),2)

    def test_removed_camera_does_not_reserve_mac(self):
        self.add()
        self.archive.delete_camera('one','one')
        self.add('replacement','192.168.2.6',MAC_A)
        self.assertEqual(self.camera('replacement')['mac_address'],MAC_A)

    def test_rebind_preserves_recordings_channel_secret_and_id(self):
        self.add(sd_password='synthetic-camera-secret',channel_chat_id=-1001234567890)
        key=hashlib.sha256(b'fixture').hexdigest()
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at,message_id,file_id)
                VALUES (?,'one','clip',1,2,'fixture','uploaded',?,77,'telegram-fixture')''',(key,time.time()))
            self.archive.conn.execute('UPDATE cameras SET probe_json=? WHERE id=?',('{"host":"192.168.2.5","tcp":{"8000":"open"}}','one'))
        before=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone())
        result=self.apply({MAC_A:'192.168.2.19'})
        camera=self.camera()
        self.assertEqual(result['updated'],['one'])
        self.assertEqual(camera['host'],'192.168.2.19')
        self.assertEqual(camera['id'],'one')
        self.assertEqual(camera['channel_chat_id'],-1001234567890)
        self.assertIsNone(camera['probe'])
        self.assertEqual(self.archive.camera_sd_config('one')['sd_password'],'synthetic-camera-secret')
        self.assertEqual(dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(key,)).fetchone()),before)

    def test_dhcp_swap_is_atomic_not_a_false_conflict(self):
        self.add()
        self.add('two','192.168.2.6',MAC_B)
        result=self.apply({MAC_A:'192.168.2.6',MAC_B:'192.168.2.5'})
        self.assertEqual(set(result['updated']),{'one','two'})
        self.assertEqual(result['conflicts'],[])
        self.assertEqual((self.camera()['host'],self.camera('two')['host']),('192.168.2.6','192.168.2.5'))

    def test_manual_destination_collision_blocks_rebind(self):
        self.add()
        self.archive.add_camera({'id':'manual','host':'192.168.2.6'})
        result=self.apply({MAC_A:'192.168.2.6'})
        self.assertEqual(result['conflicts'],['one'])
        self.assertEqual(self.camera()['host'],'192.168.2.5')
        self.assertEqual(self.camera()['identity_error'],'sd_identity_conflict')

    def test_unresolved_destination_camera_blocks_rebind(self):
        self.add()
        self.add('two','192.168.2.6',MAC_B)
        result=self.apply({MAC_A:'192.168.2.6'})
        self.assertEqual(result['conflicts'],['one'])
        self.assertEqual(result['unresolved'],['two'])
        self.assertEqual(self.camera()['host'],'192.168.2.5')

    def test_collision_dependency_chain_does_not_create_duplicate_hosts(self):
        self.add()
        self.add('two','192.168.2.6',MAC_B)
        self.archive.add_camera({'id':'manual','host':'192.168.2.7'})
        result=self.apply({MAC_A:'192.168.2.6',MAC_B:'192.168.2.7'})
        self.assertEqual(set(result['conflicts']),{'one','two'})
        self.assertEqual((self.camera()['host'],self.camera('two')['host']),('192.168.2.5','192.168.2.6'))

    def test_old_ip_reassigned_to_different_mac_does_not_verify_camera(self):
        self.add()
        result=self.apply({MAC_B:'192.168.2.5'})
        self.assertEqual(result['unresolved'],['one'])
        self.assertEqual(self.camera()['identity_status'],'not_found')
        self.assertEqual(self.camera()['identity_error'],'sd_identity_not_found')

    def test_same_host_refresh_retains_probe(self):
        self.add()
        with self.archive.conn:self.archive.conn.execute('UPDATE cameras SET probe_json=? WHERE id=?',('{"tcp":{"8000":"open"}}','one'))
        result=self.apply({MAC_A:'192.168.2.5'})
        self.assertEqual(result['updated'],[])
        self.assertEqual(result['verified'],['one'])
        self.assertEqual(self.camera()['probe'],{'tcp':{'8000':'open'}})

    def test_stale_future_nonfinite_observations_never_update_host(self):
        self.add()
        for stamp in (time.time()-121,time.time()+30,float('nan'),float('inf'),True,0):
            with self.subTest(stamp=stamp),self.assertRaises(ValueError):self.apply({MAC_A:'192.168.2.9'},stamp)
        self.assertEqual(self.camera()['host'],'192.168.2.5')

    def test_invalid_or_ambiguous_mapping_is_atomic(self):
        self.add()
        for mapping in ({MAC_A:'192.168.3.9'},{MAC_A:'192.168.2.0'},
                        {MAC_A:'192.168.2.9',MAC_B:'192.168.2.9'},{'invalid':'192.168.2.9'}):
            with self.subTest(mapping=mapping),self.assertRaises(ValueError):self.apply(mapping)
        self.assertEqual(self.camera()['host'],'192.168.2.5')
        self.assertEqual(self.camera()['identity_status'],'pending')

    def test_editing_binding_clears_verified_status(self):
        self.add()
        self.apply({MAC_A:'192.168.2.5'})
        camera=self.archive.update_camera('one',{'mac_address':MAC_B})
        self.assertEqual(camera['identity_status'],'pending')
        self.assertIsNone(camera['identity_checked_at'])
        self.assertIsNone(camera['identity_error'])

    def test_running_job_rejects_network_identity_changes(self):
        from archive_app.sync import SyncQueue
        self.add()
        queue=SyncQueue(self.archive)
        queue.enqueue('one')
        queue._claim()
        with self.assertRaises(ValueError):self.archive.update_camera('one',{'host':'192.168.2.9'})
        self.assertEqual(self.archive.update_camera('one',{'name':'Rename'})['name'],'Rename')

    def test_identity_status_fields_are_read_only(self):
        self.add()
        for field in ('identity_status','identity_error','identity_checked_at'):
            with self.subTest(field=field),self.assertRaises(ValueError):self.archive.update_camera('one',{field:'verified'})

    def test_restart_keeps_binding_and_observation(self):
        self.add()
        self.apply({MAC_A:'192.168.2.9'})
        self.archive.close()
        self.archive=Archive(self.settings)
        camera=self.camera()
        self.assertEqual((camera['host'],camera['mac_address'],camera['identity_status']),('192.168.2.9',MAC_A,'verified'))

    def test_delayed_fresh_snapshot_cannot_revert_newer_dhcp_swap(self):
        self.add()
        self.add('two','192.168.2.6',MAC_B)
        stamp=time.time()
        self.apply({MAC_A:'192.168.2.6',MAC_B:'192.168.2.5'},stamp)
        with self.assertRaisesRegex(ValueError,'older than stored'):
            self.apply({MAC_A:'192.168.2.5',MAC_B:'192.168.2.6'},stamp-1)
        self.assertEqual((self.camera()['host'],self.camera('two')['host']),('192.168.2.6','192.168.2.5'))
        self.assertEqual((self.camera()['identity_checked_at'],self.camera('two')['identity_checked_at']),(stamp,stamp))

    def test_delayed_snapshot_is_rejected_before_any_camera_update(self):
        self.add()
        self.add('two','192.168.2.6',MAC_B)
        stamp=time.time()
        with self.archive.conn:self.archive.conn.execute('UPDATE cameras SET identity_checked_at=? WHERE id=?',(stamp,'two'))
        with self.assertRaises(ValueError):self.apply({MAC_A:'192.168.2.8',MAC_B:'192.168.2.9'},stamp-1)
        self.assertEqual((self.camera()['host'],self.camera('two')['host']),('192.168.2.5','192.168.2.6'))
        self.assertIsNone(self.camera()['identity_checked_at'])


class CameraIdentitySettingsTests(unittest.TestCase):
    def load(self,**values):
        with patch.dict(os.environ,values,clear=True):return Settings.from_env()

    def test_default_has_no_metadata_observer(self):
        settings=self.load()
        self.assertEqual((settings.lan_identity_url,settings.lan_identity_token_file),('',''))

    def test_metadata_observer_url_and_token_path_load_without_secret_read(self):
        settings=self.load(LAN_IDENTITY_URL='http://100.92.56.98:8790/',LAN_IDENTITY_TOKEN_FILE='/run/secrets/lan_identity')
        self.assertEqual(settings.lan_identity_url,'http://100.92.56.98:8790')
        self.assertEqual(settings.lan_identity_token_file,'/run/secrets/lan_identity')

    def test_url_needs_token_file(self):
        with self.assertRaises(ValueError):self.load(LAN_IDENTITY_URL='http://100.92.56.98:8790')

    def test_url_rejects_remote_or_credential_bearing_target(self):
        for value in ('http://8.8.8.8:8790','http://100.92.56.98','http://secret@100.92.56.98:8790',
                      'http://100.92.56.98:8790/media'):
            with self.subTest(url=value),self.assertRaises(ValueError):
                self.load(LAN_IDENTITY_URL=value,LAN_IDENTITY_TOKEN_FILE='/run/secrets/lan_identity')


class SDIdentityGateTests(unittest.TestCase):
    setUp=CameraIdentityCoreTests.setUp
    tearDown=CameraIdentityCoreTests.tearDown
    add=CameraIdentityCoreTests.add

    def config(self):
        return {'id':'one','host':'192.168.2.5','mac_address':MAC_A,'auto_ip':True,
                'identity_subnet':SUBNET,'sd_password':'synthetic-secret','sd_username':'admin'}

    def test_resolution_happens_before_credentials_are_read(self):
        events=[]
        self.add(sd_password='synthetic-secret')
        original=self.archive.camera_sd_config
        def resolve(archive,slug):
            events.append('resolve')
            archive.apply_camera_identity(SUBNET,{MAC_A:'192.168.2.9'},time.time())
        def credentials(slug):
            events.append('credentials')
            return original(slug)
        with patch('archive_app.camera_identity.resolve_camera',side_effect=resolve),patch.object(self.archive,'camera_sd_config',side_effect=credentials):
            source=SDSource(self.archive,'one')
        self.assertEqual(events,['resolve','credentials'])
        self.assertEqual(source.config['host'],'192.168.2.9')

    def test_failed_identity_never_reads_password_or_builds_provider(self):
        from archive_app.camera_identity import CameraIdentityError
        self.add()
        with patch('archive_app.camera_identity.resolve_camera',side_effect=CameraIdentityError('sd_identity_not_found')),patch.object(self.archive,'camera_sd_config') as credentials:
            with self.assertRaises(SDSourceError) as caught:SDSource(self.archive,'one')
        self.assertEqual(caught.exception.code,'sd_identity_not_found')
        credentials.assert_not_called()

    def test_guard_runs_before_native_provider_can_receive_credentials(self):
        from archive_app.camera_identity import CameraIdentityError
        source=SDSource(self.archive,'one',credentials=self.config())
        with patch('archive_app.camera_identity.guard_camera',side_effect=CameraIdentityError('sd_identity_mismatch')),patch('archive_app.sd_source.HCNetSDKSource') as provider:
            with self.assertRaises(SDSourceError) as caught:source._provider()
        self.assertEqual(caught.exception.code,'sd_identity_mismatch')
        provider.assert_not_called()

    def test_guard_can_run_in_download_thread_without_sqlite(self):
        source=SDSource(self.archive,'one',credentials=self.config())
        owner=threading.get_ident()
        callers=[]
        with patch('archive_app.camera_identity.guard_camera',side_effect=lambda *_args,**_kwargs:callers.append(threading.get_ident())):
            worker=threading.Thread(target=source._verify_bound_identity)
            worker.start();worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(callers),1)
        self.assertNotEqual(callers[0],owner)

    def test_guard_blocks_search_after_mac_ip_changes(self):
        from archive_app.camera_identity import CameraIdentityError
        source=SDSource(self.archive,'one',credentials=self.config())
        provider=Mock()
        with patch('archive_app.camera_identity.guard_camera',side_effect=CameraIdentityError('sd_identity_mismatch')):
            with self.assertRaises(SDSourceError):source._search_history(provider,datetime.now(timezone.utc)-timedelta(hours=1),datetime.now(timezone.utc))
        provider.search.assert_not_called()

    def test_guard_rechecks_serial_download_after_successful_search(self):
        from archive_app.camera_identity import CameraIdentityError
        self.add()
        now=datetime.now(timezone.utc)
        item={'record_id':'fixture-file','start_time':(now-timedelta(hours=1)).isoformat(),
              'end_time':(now-timedelta(minutes=59)).isoformat(),'size':8}
        provider=Mock(backend='fixture')
        provider.__enter__=Mock(return_value=provider)
        provider.__exit__=Mock(return_value=None)
        provider.search.return_value=[item]
        source=SDSource(self.archive,'one',credentials=self.config())
        with patch.object(source,'_provider',return_value=provider),patch('archive_app.camera_identity.guard_camera',
                side_effect=[None,CameraIdentityError('sd_identity_mismatch')]):
            with self.assertRaises(SDSourceError) as caught:source.sync(now=now)
        self.assertEqual(caught.exception.code,'sd_identity_mismatch')
        provider.search.assert_called_once()
        provider.download.assert_not_called()
        self.assertEqual(list(self.settings.cache_dir.rglob('*.part')),[])

    def test_parallel_download_worker_rechecks_before_media_transfer(self):
        from archive_app.camera_identity import CameraIdentityError
        self.add(sd_download_threads=2)
        now=datetime.now(timezone.utc)
        item={'record_id':'fixture-file','start_time':(now-timedelta(hours=1)).isoformat(),
              'end_time':(now-timedelta(minutes=59)).isoformat(),'size':8}
        providers=[]
        def provider_factory():
            provider=Mock(backend='fixture')
            provider.__enter__=Mock(return_value=provider)
            provider.__exit__=Mock(return_value=None)
            provider.search.return_value=[item]
            providers.append(provider)
            return provider
        source=SDSource(self.archive,'one',credentials={**self.config(),'sd_download_threads':2})
        with patch.object(source,'_provider',side_effect=provider_factory),patch('archive_app.camera_identity.guard_camera',
                side_effect=[None,CameraIdentityError('sd_identity_mismatch')]):
            with self.assertRaises(SDSourceError) as caught:source.sync(now=now)
        self.assertEqual(caught.exception.code,'sd_identity_mismatch')
        self.assertGreaterEqual(len(providers),2)
        for provider in providers:provider.download.assert_not_called()
        self.assertEqual(list(self.settings.cache_dir.rglob('*.part')),[])


if __name__=='__main__':unittest.main()
