import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from archive_app.core import Archive,Settings
from archive_app.__main__ import main


class MultiCliTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=Path(tempfile.mkdtemp(prefix='.tmp-multi-cli-',dir=parent))
        self.env={'STATE_DIR':str(self.root/'data'),'CACHE_DIR':str(self.root/'cache'),
                  'INPUT_DIR':str(self.root/'input'),'DISPLAY_TIMEZONE':'UTC+07:00',
                  'MULTI_CHANNEL_ROUTING':'true','TELEGRAM_BOT_TOKEN':'700:synthetic'}
        with patch.dict(os.environ,self.env,clear=True):self.settings=Settings.from_env()
        self.archive=Archive(self.settings);self.archive.add_camera({'id':'A','name':'A','channel_chat_id':-100111})
        with self.archive.conn:
            self.archive.conn.execute("""INSERT INTO recordings(key,camera,record_id,start_ms,end_ms,source_path,status,created_at,storage_kind,storage_chat_id,storage_message_id,bot_id)
                VALUES(?,'A','fixture',1791594000000,1791594060000,'fixture','uploaded',1,'channel',-100111,17,700)""",('a'*64,))

    def tearDown(self):self.archive.close();shutil.rmtree(self.root)

    def call(self,*args):
        out=StringIO()
        with patch.dict(os.environ,self.env,clear=True),patch('sys.argv',['archive',*args]),redirect_stdout(out):
            result=main()
        return result,json.loads(out.getvalue())

    def test_rebuild_default_dryrun_does_not_need_worker_lock(self):
        with patch('archive_app.__main__.mutation_lock',side_effect=AssertionError('worker owns lock')):
            code,data=self.call('rebuild-index','--camera','A')
        self.assertEqual(code,0);self.assertTrue(data['dry_run']);self.assertEqual(data['recordings'],1)
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM channel_index_jobs').fetchone()[0],0)

    def test_rebuild_apply_only_queues_without_telegram(self):
        with patch('archive_app.telegram.Telegram.request',side_effect=AssertionError('No API now')):
            code,data=self.call('rebuild-index','--camera','A','--apply')
        self.assertEqual(code,0);self.assertFalse(data['dry_run'])
        self.assertEqual(self.archive.conn.execute('SELECT count(*) FROM channel_index_jobs').fetchone()[0],1)

    def test_reconcile_index_adopts_operator_confirmed_id_without_worker_lock(self):
        with self.archive.conn:
            self.archive.conn.execute("""INSERT INTO channel_index_messages(camera_id,channel_chat_id,index_type,period_key,state,updated_at)
                VALUES('A',-100111,'root','root','needs_reconcile',1)""")
        with patch('archive_app.__main__.mutation_lock',side_effect=AssertionError('worker owns lock')):
            code,data=self.call('reconcile-index','--camera','A','--chat-id','-100111','--type','root','--period','root','--message-id','100')
        self.assertEqual(code,0);self.assertEqual(data['message_id'],100)
        self.assertEqual(self.archive.conn.execute('SELECT tg_message_id FROM channel_index_messages').fetchone()[0],100)

    def test_reconcile_index_refuses_other_channel(self):
        with self.assertRaises(ValueError):self.call('reconcile-index','--camera','A','--chat-id','-100222','--type','root','--period','root','--message-id','100')


if __name__=='__main__':unittest.main()
