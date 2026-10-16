"""Authenticated dashboard MAC import/API regression tests."""
import time
import unittest
from unittest.mock import patch
import test_dashboard_v2 as fixture
from archive_app.camera_identity import CameraIdentityError


class IdentityDashboardTests(unittest.TestCase):
    setUp=fixture.DashboardTests.setUp
    tearDown=fixture.DashboardTests.tearDown
    request=fixture.DashboardTests.request
    login=fixture.DashboardTests.login

    def test_mac_create_and_partial_edit(self):
        self.login()
        data={'id':'mac-test','name':'New camera','host':'192.168.2.5','mac_address':'94-EC-13-14-D2-A0','auto_ip':True,'identity_subnet':'192.168.2.0/24'}
        code,row,_=self.request('POST','/api/cameras',data)
        self.assertEqual(code,201);self.assertEqual(row['mac_address'],'94:ec:13:14:d2:a0');self.assertEqual(row['identity_status'],'pending')
        self.assertEqual(self.request('PATCH','/api/cameras/mac-test',{'name':'Renamed'})[0],200)
        current=self.request('GET','/api/cameras')[1]['cameras'][0]
        self.assertEqual(current['mac_address'],row['mac_address']);self.assertEqual(current['id'],data['id'])

    def test_auto_requires_mac_subnet_and_mac_has_validation(self):
        self.login()
        for fields in ({'auto_ip':True},{'mac_address':'ff:ff:ff:ff:ff:ff'},{'auto_ip':True,'mac_address':'94:ec:13:14:d2:a0','identity_subnet':'192.168.2.0/16'}):
            self.assertEqual(self.request('POST','/api/cameras',{'id':'bad','name':'Bad','host':'192.168.2.5',**fields})[0],400)

    def test_snapshot_requires_login_and_keeps_manual_mode_when_missing(self):
        self.assertEqual(self.request('GET','/api/identity/snapshot')[0],401)
        self.login();code,data,_=self.request('GET','/api/identity/snapshot')
        self.assertEqual(code,200);self.assertFalse(data['configured']);self.assertEqual(data['records'],[])

    def test_snapshot_failure_is_sanitized(self):
        self.login()
        with patch('archive_app.dashboard.public_snapshot',side_effect=CameraIdentityError('sd_identity_unavailable','raw-secret')):
            code,data,_=self.request('GET','/api/identity/snapshot')
        self.assertEqual(code,503);self.assertEqual(data['code'],'sd_identity_unavailable');self.assertNotIn('raw-secret',str(data))

    def test_import_snapshot_enriches_verified_mac(self):
        self.login();candidate={'host':'192.168.2.5','device_port':8000,'rtsp_port':554,'http_port':80,'model':'','ports':[8000],'evidence':['device_port_open']}
        scan={'id':'synthetic-scan','target':'192.168.2.0/24','state':'completed','results':[candidate]}
        metadata={'configured':True,'records':[{'host':candidate['host'],'mac':'94:ec:13:14:d2:a0'}]}
        with patch.object(self.server.discovery,'snapshot',return_value=scan),patch('archive_app.dashboard.public_snapshot',return_value=metadata):
            code,data,_=self.request('GET','/api/discovery/scans/synthetic-scan')
            self.assertEqual(code,200);self.assertEqual(data['scan']['results'][0]['mac_address'],metadata['records'][0]['mac'])
            selected={k:candidate[k] for k in ('host','device_port','rtsp_port','http_port','model')}
            code,row,_=self.request('POST','/api/cameras',dict(selected,id='import-mac',name='Import MAC',mac_address=metadata['records'][0]['mac'],auto_ip=True,identity_subnet='192.168.2.0/24',discovery_scan_id=scan['id']))
            self.assertEqual(code,201);self.assertEqual(row['mac_address'],metadata['records'][0]['mac']);self.assertTrue(row['auto_ip'])

    def bound(self):
        return self.archive.add_camera({'id':'bound','name':'Bound','host':'192.168.2.5',
            'mac_address':'94:EC:13:14:D2:A0','auto_ip':True,'identity_subnet':'192.168.2.0/24'})

    def metadata(self,host='192.168.2.5',stamp=None):
        stamp=time.time() if stamp is None else stamp
        return {'configured':True,'version':1,'subnet':'192.168.2.0/24','observed_at':stamp,
                'records':[{'host':host,'mac':'94:ec:13:14:d2:a0','observed_at':stamp,'evidence':'tcp+netlink'}],
                'conflicts':[]}

    def test_camera_get_uses_fresh_row_proof_without_rewriting_database(self):
        self.login();self.bound()
        old_stamp=time.time()-901
        with self.archive.conn:
            self.archive.conn.execute("UPDATE cameras SET identity_status='verified',identity_checked_at=? WHERE id='bound'",(old_stamp,))
        before=dict(self.archive.conn.execute("SELECT * FROM cameras WHERE id='bound'").fetchone())
        metadata=self.metadata();metadata['observed_at']+=1
        with patch('archive_app.dashboard.public_snapshot',return_value=metadata) as snapshot:
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200);snapshot.assert_called_once_with(self.settings)
        row=data['cameras'][0]
        self.assertEqual(row['identity_status'],'verified')
        self.assertIsNone(row['identity_error'])
        self.assertEqual(row['identity_checked_at'],metadata['records'][0]['observed_at'])
        self.assertNotEqual(row['identity_checked_at'],metadata['observed_at'])
        self.assertEqual(dict(self.archive.conn.execute("SELECT * FROM cameras WHERE id='bound'").fetchone()),before)

    def test_camera_get_marks_changed_host_stale_without_rebinding(self):
        self.login();self.bound()
        with patch('archive_app.dashboard.public_snapshot',return_value=self.metadata(host='192.168.2.17')):
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200);row=data['cameras'][0]
        self.assertEqual((row['host'],row['identity_status'],row['identity_error']),
                         ('192.168.2.5','stale','sd_identity_changed'))
        self.assertEqual(self.archive.cameras()[0]['host'],'192.168.2.5')

    def test_camera_get_marks_missing_mac_not_found(self):
        self.login();self.bound();metadata=self.metadata();metadata['records']=[]
        with patch('archive_app.dashboard.public_snapshot',return_value=metadata):
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200)
        self.assertEqual((data['cameras'][0]['identity_status'],data['cameras'][0]['identity_error']),
                         ('not_found','sd_identity_not_found'))

    def test_camera_get_observer_failure_is_sanitized_and_does_not_mutate(self):
        self.login();self.bound()
        before=dict(self.archive.conn.execute("SELECT * FROM cameras WHERE id='bound'").fetchone())
        for error in (CameraIdentityError('sd_identity_invalid','SENSITIVE'),RuntimeError('SENSITIVE')):
            with self.subTest(error=type(error).__name__),patch('archive_app.dashboard.public_snapshot',side_effect=error):
                code,data,_=self.request('GET','/api/cameras')
            self.assertEqual(code,200);row=data['cameras'][0]
            self.assertEqual((row['identity_status'],row['identity_error']),('unavailable','sd_identity_unavailable'))
            self.assertNotIn('SENSITIVE',str(data))
        self.assertEqual(dict(self.archive.conn.execute("SELECT * FROM cameras WHERE id='bound'").fetchone()),before)

    def test_camera_get_does_not_hide_saved_binding_conflict(self):
        self.login();self.bound()
        with self.archive.conn:
            self.archive.conn.execute("UPDATE cameras SET identity_status='conflict',identity_error='sd_identity_conflict' WHERE id='bound'")
        for value in (self.metadata(),None):
            with self.subTest(value=value),patch('archive_app.dashboard.public_snapshot',return_value=value):
                code,data,_=self.request('GET','/api/cameras')
            self.assertEqual(code,200)
            self.assertEqual((data['cameras'][0]['identity_status'],data['cameras'][0]['identity_error']),
                             ('conflict','sd_identity_conflict'))

    def test_camera_get_leaves_manual_mode_unchanged_and_skips_observer(self):
        self.login();self.archive.add_camera({'id':'manual','host':'192.168.2.5'})
        before=self.archive.cameras()[0]
        with patch('archive_app.dashboard.public_snapshot') as snapshot:
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200);snapshot.assert_not_called()
        current=data['cameras'][0];current.pop('sync')
        self.assertEqual(current,before)

    def test_camera_get_does_not_match_mac_from_other_subnet(self):
        self.login();self.bound();metadata=self.metadata();metadata['subnet']='192.168.3.0/24'
        with patch('archive_app.dashboard.public_snapshot',return_value=metadata):
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200)
        self.assertEqual((data['cameras'][0]['identity_status'],data['cameras'][0]['identity_error']),
                         ('unavailable','sd_identity_subnet'))

    def test_camera_get_keeps_manual_rows_unchanged_in_mixed_catalog(self):
        self.login();self.bound();self.archive.add_camera({'id':'manual','name':'Manual','host':'192.168.2.9'})
        before=next(row for row in self.archive.cameras() if row['id']=='manual')
        with patch('archive_app.dashboard.public_snapshot',return_value=self.metadata()):
            code,data,_=self.request('GET','/api/cameras')
        self.assertEqual(code,200)
        current=next(row for row in data['cameras'] if row['id']=='manual');current.pop('sync')
        self.assertEqual(current,before)
