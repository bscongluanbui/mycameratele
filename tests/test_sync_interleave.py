import os,shutil,tempfile,unittest,uuid
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
from archive_app.core import Archive,Settings
from archive_app.sync import SyncQueue
from archive_app.telegram import Telegram

class InterleavedTransferTests(unittest.TestCase):
 def setUp(self):
  self.parent=(Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())).resolve()
  self.root=self.parent/('.tmp-interleave-'+uuid.uuid4().hex);self.root.mkdir()
  self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',min_free_bytes=0,enable_upload=True,token='fixture',owner_user_id=42)
  self.settings.input_dir.mkdir();self.source=self.settings.input_dir/'original.dav';self.source.write_bytes(b'raw-camera-fixture-no-conversion')
  self.a=Archive(self.settings);self.a.add_camera({'id':'fixture'});self.a.state('telegram_owner_started:42','1');self.t=Telegram(self.settings)
 def tearDown(self):
  self.a.close();shutil.rmtree(self.root)
 def test_upload_occurs_before_sd_batch_returns_and_duplicate_snapshot_does_not_repeat(self):
  q=SyncQueue(self.a);q.enqueue('fixture');start=datetime.fromisoformat('2026-10-03T10:00:00+07:00')
  entry={'camera':'fixture','record_id':'raw-one','path':str(self.source),'start_time':start.isoformat(),'end_time':(start+timedelta(minutes=1)).isoformat()}
  def fake(*,progress):
   self.a.ingest_entry(entry)
   snapshot={'phase':'sd_download','backend':'hcnetsdk','searched':2,'downloaded':1,'imported':1}
   progress(snapshot)
   self.assertEqual(self.a.status()['counts']['uploaded'],1)
   progress(snapshot)
   self.assertEqual(transport.call_count,1)
   return snapshot
  response={'chat':{'id':42,'type':'private'},'message_id':1,'document':{'file_id':'fixture-id','file_unique_id':'fixture-unique'}}
  with patch.object(Archive,'probe_camera',return_value={'tcp':{}}),patch('archive_app.sd_source.SDSource.sync',side_effect=fake),patch.object(self.t,'request',return_value=response) as transport:
   q.run_once(self.t)
  self.assertEqual(q.status('fixture')['latest']['fixture']['statistics']['uploaded'],1)
  self.assertEqual(transport.call_args.args[0],'sendDocument')
  row=self.a.conn.execute('SELECT * FROM recordings').fetchone();self.assertEqual(row['processing_method'],'passthrough')
  self.assertEqual(Path(row['local_path']).read_bytes(),self.source.read_bytes())

if __name__=='__main__':unittest.main()
