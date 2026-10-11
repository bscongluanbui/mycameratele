"""Persistent bounded pipeline concurrency, authenticated HTTP and camera controls."""
import os
import unittest
from unittest.mock import patch

import test_camera_removal as fixture
from archive_app.core import Archive,Settings


class PipelineSettingsTests(fixture.RemovalFixture):
    def test_environment_defaults_validated(self):
        with patch.dict(os.environ,{'SD_DOWNLOAD_WORKERS':'5','SD_DOWNLOAD_THREADS':'2'},clear=True):
            settings=Settings.from_env()
        self.assertEqual((settings.sd_download_workers,settings.sd_download_threads),(5,2))
        for value in ('0','3','true','2.0','-1',''):
            with self.subTest(value=value),patch.dict(os.environ,{'SD_DOWNLOAD_THREADS':value},clear=True):
                with self.assertRaises(ValueError):Settings.from_env()

    def test_global_persistence_and_shared_connection_visibility(self):
        self.assertEqual(self.archive.pipeline_settings(),{'sd_download_workers':1,'environment_default':1,'applies_to':'next_jobs'})
        self.archive.update_pipeline_settings({'sd_download_workers':5})
        other=Archive(self.settings)
        try:
            self.assertEqual(other.pipeline_settings()['sd_download_workers'],5)
            other.update_pipeline_settings({'sd_download_workers':2})
            self.assertEqual(self.archive.pipeline_settings()['sd_download_workers'],2)
        finally:other.close()
        self.settings.sd_download_workers=4
        self.assertEqual(self.archive.pipeline_settings()['sd_download_workers'],2)
        self.assertEqual(self.archive.pipeline_settings()['environment_default'],4)

    def test_invalid_runtime_values_never_change_setting(self):
        for data in ({},{'unknown':2},{'sd_download_workers':False},{'sd_download_workers':'2'},
                     {'sd_download_workers':0},{'sd_download_workers':6},{'sd_download_workers':2.0},
                     {'sd_download_workers':2,'extra':True}):
            with self.subTest(data=data),self.assertRaises(ValueError):self.archive.update_pipeline_settings(data)
        self.assertEqual(self.archive.pipeline_settings()['sd_download_workers'],1)

    def test_per_camera_bounds_default_and_edit(self):
        self.assertEqual(self.archive.cameras()[0]['sd_download_threads'],1)
        camera=self.archive.update_camera('cash',{'sd_download_threads':2})
        self.assertEqual(camera['sd_download_threads'],2)
        self.assertEqual(self.archive.camera_sd_config('cash')['sd_download_threads'],2)
        for value in (0,3,-1,False,'2',2.0,None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.archive.update_camera('cash',{'sd_download_threads':value})
        self.settings.sd_download_threads=2
        self.assertEqual(self.archive.add_camera({'id':'next'})['sd_download_threads'],2)


class PipelineSettingsHTTPTests(fixture.CameraRemovalHTTPTests):
    # Reuse the real authenticated HTTP fixture without inheriting its test cases.
    test_delete_obeys_auth_initial_password_csrf_and_origin=None
    test_delete_body_query_confirmation_and_missing_validation=None
    test_success_and_busy_responses=None

    def test_pipeline_auth_csrf_origin_and_validation(self):
        self.assertEqual(self.request('GET','/api/pipeline-settings')[0],401)
        self.assertEqual(self.request('PATCH','/api/pipeline-settings',{'sd_download_workers':2})[0],401)
        self.login()
        self.assertEqual(self.request('PATCH','/api/pipeline-settings',{'sd_download_workers':2},{'X-CSRF-Token':'bad'})[0],403)
        self.assertEqual(self.request('PATCH','/api/pipeline-settings',{'sd_download_workers':2},{'Origin':'http://elsewhere.example'})[0],403)
        self.assertEqual(self.request('GET','/api/pipeline-settings?unknown=1')[0],400)
        self.assertEqual(self.request('PATCH','/api/pipeline-settings',{'sd_download_workers':6})[0],400)
        status,body,_=self.request('PATCH','/api/pipeline-settings',{'sd_download_workers':5})
        self.assertEqual(status,200);self.assertEqual(body['sd_download_workers'],5)
        self.assertEqual(self.request('GET','/api/pipeline-settings')[1]['sd_download_workers'],5)
        self.assertEqual(self.request('PATCH','/api/cameras/cash',{'sd_download_threads':2})[1]['sd_download_threads'],2)


if __name__=='__main__':unittest.main()
