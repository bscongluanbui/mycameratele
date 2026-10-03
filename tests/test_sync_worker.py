"""Real worker loop with local SQLite and bounded mocked transport/clock."""
from contextlib import redirect_stdout
from io import StringIO
import json,os,shutil,signal,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from archive_app.__main__ import main
from archive_app.core import Archive,Settings
from archive_app.sync import SyncQueue

class AutomaticSyncWorkerTests(unittest.TestCase):
    def setUp(self):
        self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
        self.root=self.parent/('.tmp-auto-worker-'+uuid.uuid4().hex);self.root.mkdir()
        self.env={'STATE_DIR':str(self.root/'state'),'CACHE_DIR':str(self.root/'cache'),
                  'INPUT_DIR':str(self.root/'input'),'DISPLAY_TIMEZONE':'UTC+07:00',
                  'SD_SYNC_INTERVAL_SECONDS':'300','SCAN_INTERVAL_SECONDS':'1','CACHE_MIN_FREE_GB':'0'}
    def tearDown(self):
        self.assertEqual(self.root.resolve().parent,self.parent);shutil.rmtree(self.root)
    def test_worker_automatically_enqueues_and_finishes_explicit_missing_source_status(self):
        with patch.dict(os.environ,self.env,clear=True):settings=Settings.from_env()
        a=Archive(settings);a.add_camera({'id':'c6n','host':'192.168.31.166'});a.close()
        handlers={};out=StringIO()
        def register(sig,handler):handlers[sig]=handler
        def sleep(_):handlers[signal.SIGTERM]()
        with patch.dict(os.environ,self.env,clear=True),patch('sys.argv',['archive','run']),redirect_stdout(out), \
             patch('archive_app.__main__.signal.signal',side_effect=register), \
             patch('archive_app.__main__.time.sleep',side_effect=sleep), \
             patch('archive_app.core.Archive.probe_camera',return_value={'tcp':{'8000':'open'}}):
            self.assertEqual(main(),0)
        events=[json.loads(line)['event'] for line in out.getvalue().splitlines()]
        self.assertIn('sync_scheduled',events);self.assertIn('sync_job_finished',events);self.assertEqual(events[-1],'stopped')
        a=Archive(settings)
        try:
            job=SyncQueue(a).status('c6n')['latest']['c6n']
            self.assertEqual(job['source'],'automatic');self.assertEqual(job['state'],'blocked')
            self.assertIn(job['code'],('sd_credentials_missing','sd_source_missing'))
        finally:a.close()
    def test_invalid_interval_is_rejected(self):
        with patch.dict(os.environ,{**self.env,'SD_SYNC_INTERVAL_SECONDS':'0'},clear=True),patch('sys.argv',['archive','run']), \
             patch('archive_app.__main__.signal.signal'):
            with self.assertRaises(ValueError):main()
