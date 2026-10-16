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
