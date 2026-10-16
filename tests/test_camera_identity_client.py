"""MAC metadata validation and pre-login identity checks; synthetic peers only."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock

from archive_app.camera_identity import (CameraIdentityError,IdentityClient,
    normalize_mac,normalize_subnet,validate_url,validate_snapshot,guard_camera,
    resolve_camera,public_snapshot)
from archive_app.core import Archive,Settings

MAC='94:ec:13:14:d2:a0'
def snapshot(now=None,host='192.168.2.17'):
    now=time.time() if now is None else now
    return {'version':1,'subnet':'192.168.2.0/24','observed_at':now,'records':[
        {'host':host,'mac':MAC,'observed_at':now,'evidence':'tcp+netlink'}],'conflicts':[]}


class ValidationTests(unittest.TestCase):
    def test_normalize_mac_accepted_formats(self):
        for value in ('94:EC:13:14:D2:A0','94-ec-13-14-d2-a0','94ec1314d2a0'):
            self.assertEqual(normalize_mac(value),MAC)
        self.assertEqual(normalize_mac(' '),'')

    def test_bad_mac(self):
        for value in (None,4,'00:00:00:00:00:00','ff:ff:ff:ff:ff:ff','01:23:45:67:89:ab','94:ec-13:14:d2:a0','short'):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_mac(value)

    def test_subnet(self):
        self.assertEqual(normalize_subnet('192.168.2.77/24'),'192.168.2.0/24')
        for value in ('192.168.2.0/23','192.168.2.1/32','8.8.8.0/24','100.64.0.0/24','192.168.2.0','::/24'):
            with self.subTest(value=value),self.assertRaises(ValueError):normalize_subnet(value)

    def test_private_or_tailnet_explicit_url_only(self):
        self.assertEqual(validate_url('http://100.92.56.98:8790/'),'http://100.92.56.98:8790')
        for value in ('http://example.com:8790','http://8.8.8.8:8790','http://100.92.56.98','http://u:p@100.92.56.98:8790','http://100.92.56.98:8790/x','http://100.92.56.98:8790?q=x'):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_url(value)

    def test_fresh_snapshot_and_cache_revalidation(self):
        data=validate_snapshot(snapshot(1000),1001)
        self.assertEqual(data['mappings'],{MAC:'192.168.2.17'})
        self.assertEqual(validate_snapshot(data,1002)['records'],data['records'])

    def test_reject_stale_or_future_timestamps(self):
        for value in (0,879,1006,float('nan'),float('inf'),True,'1000'):
            data=snapshot(1000);data['observed_at']=value
            with self.subTest(value=value),self.assertRaises(CameraIdentityError):validate_snapshot(data,1000)

    def test_row_age_also_matters(self):
        data=snapshot(1000);data['records'][0]['observed_at']=800
        with self.assertRaises(CameraIdentityError):validate_snapshot(data,1000)

    def test_invalid_scope_evidence_conflict_error_version(self):
        for key,value in (('host','192.168.1.17'),('host','192.168.2.0'),('host','192.168.2.255'),('evidence','arp'),('mac','ff:ff:ff:ff:ff:ff')):
            data=snapshot(1000);data['records'][0][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(CameraIdentityError):validate_snapshot(data,1000)
        for key,value in (('conflicts',[MAC]),('error','observer_scan_failed'),('version',True)):
            data=snapshot(1000);data[key]=value
            with self.subTest(key=key),self.assertRaises(CameraIdentityError):validate_snapshot(data,1000)

    def test_duplicate_host_or_mac(self):
        for changed in ({'host':'192.168.2.18'},{'mac':'94:ec:13:14:d2:a2'},{}):
            data=snapshot(1000);data['records'].append({**data['records'][0],**changed})
            with self.subTest(changed=changed),self.assertRaises(CameraIdentityError):validate_snapshot(data,1000)


class ClientTests(unittest.TestCase):
    def setUp(self):self.client=IdentityClient('http://100.92.56.98:8790','unused',clock=lambda:1000)

    def test_singleflight_cached_snapshot(self):
        with patch.object(self.client,'_request',return_value=snapshot(1000)) as request:
            self.client.snapshot();self.client.snapshot();self.assertEqual(request.call_count,1)
            self.client.clock=lambda:1121
            with self.assertRaises(CameraIdentityError):self.client.snapshot()

    def test_guard_manual_mode_does_not_connect(self):
        with patch('archive_app.camera_identity.client_for') as client:
            guard_camera({'host':'192.168.2.5'},settings=object());client.assert_not_called()

    def test_guard_moved_ip_no_login_verification_to_old_ip(self):
        config={'auto_ip':True,'mac_address':MAC,'host':'192.168.2.7','identity_subnet':'192.168.2.0/24'}
        with patch('archive_app.camera_identity.client_for',return_value=self.client),patch.object(self.client,'_request',return_value=snapshot(1000)) as req:
            with self.assertRaises(CameraIdentityError) as error:guard_camera(config,settings=object())
            self.assertEqual(error.exception.code,'sd_identity_changed');self.assertEqual(req.call_count,1)

    def test_verify_correct_response_cached_one_second(self):
        reply={'version':1,'host':'192.168.2.17','mac':MAC,'verified':True,'observed_at':1000}
        with patch.object(self.client,'_request',return_value=reply) as req:
            self.client.verify(reply['host'],MAC);self.client.verify(reply['host'],MAC);self.assertEqual(req.call_count,1)
            self.client.clock=lambda:1002;self.client.verify(reply['host'],MAC);self.assertEqual(req.call_count,2)

    def test_verify_mismatch_never_positive_cached(self):
        base={'version':1,'host':'192.168.2.17','mac':MAC,'verified':True,'observed_at':1000}
        for key,value in (('version',True),('verified',False),('host','192.168.2.7'),('mac','94:ec:13:14:d2:a2'),('observed_at',990)):
            with self.subTest(key=key),patch.object(self.client,'_request',return_value={**base,key:value}),self.assertRaises(CameraIdentityError):self.client.verify(base['host'],MAC)
        self.assertEqual(self.client.verified,{})

    def test_transport_does_not_expose_secret_or_follow_redirect(self):
        with tempfile.TemporaryDirectory() as root:
            token=Path(root)/'token';token.write_text('synthetic-private-token-12345678901234567890')
            self.client.token_file=str(token)
            with patch('archive_app.camera_identity.build_opener') as opener:
                opener.return_value.open.side_effect=OSError('untrusted error secret')
                with self.assertRaises(CameraIdentityError) as err:self.client._request('/v1/neighbors')
                self.assertNotIn('secret',str(err.exception));self.assertNotIn('synthetic-private-token',str(err.exception))
                handlers=opener.call_args.args;self.assertEqual(handlers[0].proxies,{})
                self.assertIsNone(handlers[1].redirect_request(None,None,None,None,None,None))

    def test_unconfigured_public_status(self):self.assertEqual(public_snapshot(SimpleNamespace(lan_identity_url='')),{'configured':False,'records':[]})


class ResolveTests(unittest.TestCase):
    def test_rebind_then_observer_offline_marks_unavailable_preserves_identity(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);settings=Settings(p/'data',p/'cache',p/'input','UTC',min_free_bytes=0)
            a=Archive(settings)
            try:
                a.add_camera({'id':'stable','name':'Between rooms','host':'192.168.2.7','mac_address':MAC,'auto_ip':True,'identity_subnet':'192.168.2.0/24'})
                client=Mock();client.snapshot.return_value=validate_snapshot(snapshot())
                with patch('archive_app.camera_identity.client_for',return_value=client):resolve_camera(a,'stable')
                row=a.cameras()[0];self.assertEqual(row['host'],'192.168.2.17');self.assertEqual(row['identity_status'],'verified')
                client.snapshot.side_effect=CameraIdentityError('sd_identity_unavailable')
                with patch('archive_app.camera_identity.client_for',return_value=client),self.assertRaises(CameraIdentityError):resolve_camera(a,'stable')
                after=a.cameras()[0];self.assertEqual(after['host'],row['host']);self.assertEqual(after['identity_checked_at'],row['identity_checked_at']);self.assertEqual(after['identity_status'],'unavailable')
            finally:a.close()
