"""Permission checks accept paused cameras without admitting uploads or media sends."""
import copy
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from archive_app.core import Archive, Settings
from archive_app.telegram import ApiRejected, ChannelCheckError, Telegram


class PausedChannelVerificationTests(unittest.TestCase):
    def setUp(self):
        parent=Path(__file__).parent if os.name=='nt' else Path(tempfile.gettempdir())
        self.root=Path(tempfile.mkdtemp(prefix='.tmp-paused-channel-',dir=parent))
        self.settings=Settings(self.root/'state',self.root/'cache',self.root/'input','UTC+07:00',
            token='700:synthetic',owner_user_id=42,allowed_users=(42,),enable_upload=True,
            min_free_bytes=0,multi_channel_routing=True)
        self.archive=Archive(self.settings)
        self.archive.add_camera({'id':'A','name':'Camera A','enabled':False,'upload_enabled':False,
                                 'channel_chat_id':-100111})
        self.telegram=Telegram(self.settings)
        self.responses={'getMe':{'id':700,'is_bot':True,'username':'fixture_bot'},
            'getChat':{'id':-100111,'type':'channel','title':'Private fixture'},
            'getChatMember':{'user':{'id':700,'is_bot':True},'status':'administrator',
                             'can_post_messages':True,'can_edit_messages':True}}
        self.calls=[]
        self.hook=None
        self.telegram.request=self.request

    def tearDown(self):
        self.archive.close()
        shutil.rmtree(self.root)

    def request(self,method,fields,**kwargs):
        self.calls.append((method,copy.deepcopy(fields)))
        if self.hook:self.hook(method,fields)
        if method not in self.responses:raise AssertionError('Unexpected Telegram send: '+method)
        return copy.deepcopy(self.responses[method])

    def camera(self,slug='A'):
        return next(camera for camera in self.archive.cameras() if camera['id']==slug)

    def assert_code(self,code,call):
        with self.assertRaises(ChannelCheckError) as caught:call()
        self.assertEqual(caught.exception.code,code)
        self.assertEqual(str(caught.exception),code)

    def test_paused_camera_verifies_without_enabling_sync_or_upload_or_sending_index(self):
        with (patch.object(self.archive,'resolve_camera_channel',side_effect=AssertionError('Upload admission must not be used')),
                patch.object(self.archive.channel_index,'enqueue_camera') as enqueue):
            self.assertEqual(self.telegram.verify_camera_channel(self.archive,'A'),(-100111,700))
            enqueue.assert_called_once_with('A')
        camera=self.camera()
        self.assertFalse(camera['enabled'])
        self.assertFalse(camera['upload_enabled'])
        self.assertTrue(camera['channel_enabled'])
        self.assertEqual(camera['channel_status'],'ready')
        self.assertEqual([method for method,_ in self.calls],['getMe','getChat','getChatMember'])
        with self.assertRaises(ValueError):self.archive.resolve_camera_channel('A')

    def test_already_ready_paused_camera_does_not_enqueue_index_again(self):
        self.archive.set_channel_status('A','ready')
        with patch.object(self.archive.channel_index,'enqueue_camera') as enqueue:
            self.telegram.verify_camera_channel(self.archive,'A')
            enqueue.assert_not_called()

    def test_verified_paused_camera_still_cannot_claim_or_upload_pending_recording(self):
        self.telegram.verify_camera_channel(self.archive,'A')
        self.archive.update_camera('A',{'upload_enabled':True})
        with self.archive.conn:
            self.archive.conn.execute('''INSERT INTO recordings
                (key,camera,record_id,start_ms,end_ms,source_path,status,created_at)
                VALUES('synthetic-key','A','record',1,2,'synthetic.mp4','downloaded',1)''')
        self.calls.clear()
        self.assertEqual(self.archive.pending_upload_cameras('A'),[])
        self.assertIsNone(self.archive.claim_upload('A',channel_chat_id=-100111))
        self.assertIsNone(self.telegram.upload_one(self.archive,'A'))
        self.assertEqual(self.calls,[])
        row=self.archive.conn.execute("SELECT status,attempt_id,upload_target_chat_id FROM recordings WHERE key='synthetic-key'").fetchone()
        self.assertEqual(tuple(row),('downloaded',None,None))

    def test_unknown_camera_remains_unknown_and_no_api_call_occurs(self):
        with self.assertRaises(KeyError):self.telegram.verify_camera_channel(self.archive,'missing')
        self.assertEqual(self.calls,[])

    def test_missing_channel_id_is_rejected_without_api_calls(self):
        self.archive.add_camera({'id':'B','enabled':False})
        self.assert_code('channel_invalid_id',lambda:self.telegram.verify_camera_channel(self.archive,'B'))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.camera('B')['channel_error'],'channel_invalid_id')

    def test_explicitly_disabled_channel_remains_blocked(self):
        self.archive.update_camera('A',{'channel_enabled':False})
        self.assert_code('channel_disabled',lambda:self.telegram.verify_camera_channel(self.archive,'A'))
        self.assertEqual(self.calls,[])
        self.assertFalse(self.camera()['channel_enabled'])
        self.assertFalse(self.camera()['enabled'])

    def test_invalid_channel_id_types_are_sanitized_before_api_requests(self):
        for channel in (None,True,-1000,1,'-100111',-100111111111111111111):
            with self.subTest(channel=channel):
                self.assert_code('channel_invalid_id',lambda:self.telegram.verify_channel(self.archive,channel))
        self.assertEqual(self.calls,[])

    def test_invalid_bot_identity_has_precise_code(self):
        self.responses['getMe']={'id':True,'is_bot':True,'username':'synthetic-token'}
        self.assert_code('channel_bot_identity_invalid',lambda:self.telegram.verify_camera_channel(self.archive,'A'))
        self.assertEqual(self.camera()['channel_error'],'channel_bot_identity_invalid')
        self.assertNotIn('synthetic-token',str(self.camera()))

    def test_channel_identity_and_public_channel_are_distinct(self):
        self.responses['getChat']['id']=-100222
        self.assert_code('channel_identity_mismatch',lambda:self.telegram.verify_camera_channel(self.archive,'A'))
        self.responses['getChat']['id']=-100111
        self.responses['getChat']['username']='public_fixture'
        self.assert_code('channel_not_private',lambda:self.telegram.verify_camera_channel(self.archive,'A'))
        self.responses['getChat'].pop('username')
        self.responses['getChat']['active_usernames']=['public_fixture']
        self.assert_code('channel_not_private',lambda:self.telegram.verify_camera_channel(self.archive,'A'))

    def test_wrong_member_identity_and_admin_post_edit_rights_have_distinct_codes(self):
        original=copy.deepcopy(self.responses['getChatMember'])
        cases=[({'user':{'id':701,'is_bot':True}},'channel_bot_identity_invalid'),
               ({'status':'member'},'channel_bot_not_admin'),
               ({'can_post_messages':False},'channel_post_permission_missing'),
               ({'can_edit_messages':False},'channel_edit_permission_missing')]
        for change,code in cases:
            with self.subTest(code=code):
                self.responses['getChatMember']={**original,**change}
                self.assert_code(code,lambda:self.telegram.verify_camera_channel(self.archive,'A'))
                self.assertEqual(self.camera()['channel_error'],code)

    def test_edit_right_is_optional_only_when_index_not_required(self):
        self.responses['getChatMember']['can_edit_messages']=False
        self.assertEqual(self.telegram.verify_camera_channel(self.archive,'A',require_index=False),(-100111,700))
        self.assert_code('channel_edit_permission_missing',lambda:self.telegram.verify_camera_channel(self.archive,'A',require_index=True))

    def test_mapping_changed_during_verification_keeps_new_mapping_unverified(self):
        def switch(method,fields):
            if method=='getChatMember':self.archive.update_camera('A',{'channel_chat_id':-100222})
        self.hook=switch
        with patch.object(self.archive.channel_index,'enqueue_camera') as enqueue:
            self.assert_code('channel_changed',lambda:self.telegram.verify_camera_channel(self.archive,'A'))
            # Only the explicit mapping edit enqueues, not the failed old check.
            enqueue.assert_called_once_with('A',commit=False)
        self.assertEqual(self.camera()['channel_chat_id'],-100222)
        self.assertEqual(self.camera()['channel_status'],'unconfigured')

    def test_failed_old_mapping_check_does_not_mark_new_mapping_error(self):
        def fail_after_switch(method,fields):
            if method=='getChatMember':
                self.archive.update_camera('A',{'channel_chat_id':-100222})
                raise ApiRejected(403,description='synthetic-secret-token /internal/path')
        self.hook=fail_after_switch
        with self.assertRaises(ApiRejected):self.telegram.verify_camera_channel(self.archive,'A')
        self.assertEqual(self.camera()['channel_chat_id'],-100222)
        self.assertEqual(self.camera()['channel_status'],'unconfigured')
        self.assertIsNone(self.camera()['channel_error'])

    def test_rate_limit_preserves_typed_api_error_retry_and_sanitized_status(self):
        def limited(method,fields):raise ApiRejected(429,37,'synthetic-secret-token')
        self.hook=limited
        with self.assertRaises(ApiRejected) as caught:self.telegram.verify_camera_channel(self.archive,'A')
        self.assertEqual((caught.exception.code,caught.exception.retry_after),(429,37))
        self.assertEqual(self.camera()['channel_error'],'channel_rate_limited')
        self.assertNotIn('synthetic-secret-token',str(caught.exception))

    def test_unknown_diagnostic_text_is_replaced_by_static_fallback(self):
        for raw in ('synthetic-token https://secret.invalid',None,{},['channel_invalid_id']):
            error=ChannelCheckError(raw)
            self.assertIsInstance(error,ValueError)
            self.assertEqual((error.code,str(error)),('channel_check_failed','channel_check_failed'))


if __name__=='__main__':unittest.main()
