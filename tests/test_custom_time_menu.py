"""Custom date ranges are viewer-bound, frozen, and require no media I/O."""
import json
from datetime import datetime
import unittest
from unittest.mock import patch

import test_time_menu as fixtures
from archive_app.telegram_menu import TimeMenus


class CustomTimeMenuTests(unittest.TestCase):
    NOW = fixtures.TimeMenuTests.NOW
    setUp = fixtures.TimeMenuTests.setUp
    tearDown = fixtures.TimeMenuTests.tearDown
    camera = fixtures.TimeMenuTests.camera
    recording = fixtures.TimeMenuTests.recording
    callbacks = staticmethod(fixtures.TimeMenuTests.callbacks)

    def select(self, actor=43, start='02/10/2026 23:30', end='03/10/2026 02:30'):
        self.menus.begin(self.archive, actor)
        self.menus.accept(self.archive, actor, start)
        return self.menus.accept(self.archive, actor, end)

    def test_parses_two_explicit_local_datetime_formats_and_rejects_bad_dates(self):
        expected=fixtures.stamp('2026-10-03T02:30:00+07:00')
        self.assertEqual(self.menus.parse_timestamp('03/10/2026 02:30'), expected)
        self.assertEqual(self.menus.parse_timestamp('2026-10-03 02:30'), expected)
        for value in ('31/02/2026 10:00','03/10/2026 25:00','31/02/2026','10/03/26 02:30',
                      '2026-10-03T02:30+00:00','01/01/2500 00:00','01/01/1960 00:00','x'*33,None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.menus.parse_timestamp(value)

    def test_dd_mm_yy_dates_start_at_local_midnight_with_explicit_century(self):
        self.assertEqual(self.menus.parse_timestamp('03/10/26'),fixtures.stamp('2026-10-03T00:00:00+07:00'))
        self.assertEqual(self.menus.parse_timestamp('03/10/2026'),fixtures.stamp('2026-10-03T00:00:00+07:00'))
        self.assertEqual(self.menus.parse_timestamp('01/01/69'),fixtures.stamp('2069-01-01T00:00:00+07:00'))
        self.assertEqual(self.menus.parse_timestamp('29/02/28'),fixtures.stamp('2028-02-29T00:00:00+07:00'))
        for value in ('31/02/26','29/02/26','03/13/26','3/10/26','10/3/26','03/10/2','03/10/026'):
            with self.subTest(value=value),self.assertRaises(ValueError):self.menus.parse_timestamp(value)
        self.assertIn('DD/MM/YY',self.menus.begin(self.archive,43)[0])

    def test_date_only_same_day_includes_entire_end_date_and_excludes_next_day(self):
        self.camera()
        included=self.recording('2026-10-03T23:59:00+07:00','2026-10-04T00:00:00+07:00')
        self.recording('2026-10-02T23:59:00+07:00','2026-10-03T00:00:00+07:00')
        self.recording('2026-10-04T00:00:00+07:00','2026-10-04T00:01:00+07:00')
        title,cameras=self.select(start='03/10/26',end='03/10/26')
        self.assertIn('03/10/26 → 03/10/26',title)
        session=self.menus._session(self.archive,43)
        self.assertTrue(session['date_only'])
        self.assertEqual((session['start_ms'],session['end_ms']),
                         (fixtures.stamp('2026-10-03T00:00:00+07:00'),fixtures.stamp('2026-10-04T00:00:00+07:00')))
        _,clips=self.menus.menu(self.archive,self.callbacks(cameras,'wqc:')[0],actor=43)
        self.assertEqual(self.callbacks(clips,'v:'),['v:'+included[:32]])

    def test_date_only_cross_month_and_thirty_one_calendar_day_limit(self):
        title,_=self.select(start='30/09/26',end='03/10/26')
        self.assertIn('30/09/26 → 03/10/26',title)
        title,_=self.select(start='01/10/26',end='31/10/26')
        self.assertIn('Camera',title)
        self.assertEqual(self.menus._session(self.archive,43)['end_ms'],fixtures.stamp('2026-11-01T00:00:00+07:00'))
        for value in ('30/09/26','01/11/26'):
            self.menus.begin(self.archive,43)
            self.menus.accept(self.archive,43,'01/10/26')
            title,_=self.menus.accept(self.archive,43,value)
            self.assertTrue('sau giờ bắt đầu' in title or 'tối đa 31 ngày' in title)
            self.assertEqual(self.menus._session(self.archive,43)['step'],'end')

    def test_date_end_and_datetime_start_keep_compatibility(self):
        self.select(start='02/10/2026 23:30',end='03/10/26')
        session=self.menus._session(self.archive,43)
        self.assertFalse(session['date_only'])
        self.assertEqual(session['end_ms'],fixtures.stamp('2026-10-04T00:00:00+07:00'))

    def test_date_custom_bulk_window_matches_all_pages_and_actor_bound_selection(self):
        self.camera()
        for minute in range(12):self.recording(f'2026-10-03T01:{minute:02}:00+07:00',f'2026-10-03T01:{minute:02}:30+07:00')
        _,cameras=self.select(start='03/10/26',end='03/10/26')
        _,clips=self.menus.menu(self.archive,self.callbacks(cameras,'wqc:')[0],actor=43)
        bulk=self.callbacks(clips,'bwq:')[0]
        selection=self.menus.download_window(self.archive,bulk,43)
        self.assertEqual(selection['camera'],'Front_Camera')
        self.assertEqual(selection['order'],'asc')
        self.assertEqual(self.archive.list_window(selection['start_ms'],selection['end_ms'],camera=selection['camera'])['total'],12)
        self.assertIn('⬇ Tải toàn bộ (12)',[b['text'] for row in clips for b in row])
        self.assertIsNone(self.menus.download_window(self.archive,self.callbacks(cameras,'bwq:')[0],43)['camera'])
        with self.assertRaises(ValueError):self.menus.download_window(self.archive,bulk,42)
        self.clock_mock.return_value=self.NOW+TimeMenus.RANGE_TTL+1
        with self.assertRaises(ValueError):self.menus.download_window(self.archive,bulk,43)

    def test_date_only_dst_days_and_thirty_one_day_fall_range_use_local_midnights(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:zone=ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:self.skipTest('OS fixture has no DST zone database')
        self.settings.timezone='America/New_York'
        with patch('archive_app.telegram_menu.get_zone',return_value=zone):
            for day,hours in (('08/03/26',23),('01/11/26',25)):
                self.select(start=day,end=day)
                session=self.menus._session(self.archive,43)
                self.assertEqual(session['end_ms']-session['start_ms'],hours*3600000)
            title,_=self.select(start='15/10/26',end='14/11/26')
            self.assertIn('Camera',title)
            session=self.menus._session(self.archive,43)
            self.assertEqual(session['end_ms']-session['start_ms'],31*86400000+3600000)
            self.assertEqual(self.menus._custom_window(self.archive,43,session['token'])[:2],
                             (session['start_ms'],session['end_ms']))

    def test_future_date_menus_do_not_use_platform_time_t_fromtimestamp(self):
        class NarrowTimeDateTime(datetime):
            @classmethod
            def fromtimestamp(cls, *args, **kwargs):
                raise OverflowError('synthetic 32-bit time_t overflow')

        self.camera()
        key=self.recording('2069-01-01T08:00:00+07:00','2069-01-01T08:01:00+07:00')
        self.clock_mock.return_value=fixtures.stamp('2069-01-01T12:00:00+07:00')//1000
        with patch('archive_app.telegram_menu.datetime',NarrowTimeDateTime),patch('archive_app.core.datetime',NarrowTimeDateTime):
            self.assertEqual(self.menus.parse_timestamp('01/01/69'),fixtures.stamp('2069-01-01T00:00:00+07:00'))
            title,cameras=self.select(start='01/01/69',end='02/01/69')
            self.assertIn('01/01/69 → 02/01/69',title)
            _,clips=self.menus.menu(self.archive,self.callbacks(cameras,'wqc:')[0],actor=43)
            self.assertEqual(self.callbacks(clips,'v:'),['v:'+key[:32]])
            self.assertIn('01/01 08:00:00 → 01/01 08:01:00',self.menus.menu(self.archive,self.callbacks(cameras,'wqc:')[0],actor=43)[0])
            selected=self.menus.download_window(self.archive,self.callbacks(clips,'bwq:')[0],43)
            self.assertEqual(selected['end_ms'],fixtures.stamp('2069-01-03T00:00:00+07:00'))
            title,_=self.select(start='01/01/2069 00:00',end='02/01/2069 00:00')
            self.assertIn('01/01/2069 00:00 → 02/01/2069 00:00',title)
            for preset in ('today','yesterday','last6h','thisweek','lastweek'):
                self.assertIn('Camera',self.menus.menu(self.archive,preset)[0])

    def test_custom_cross_midnight_range_lists_cameras_then_only_overlapping_video(self):
        self.camera()
        first=self.recording('2026-10-02T23:29:00+07:00','2026-10-02T23:31:00+07:00')
        second=self.recording('2026-10-03T02:00:00+07:00','2026-10-03T02:01:00+07:00')
        self.recording('2026-10-02T23:00:00+07:00','2026-10-02T23:30:00+07:00')
        self.recording('2026-10-03T02:30:00+07:00','2026-10-03T02:31:00+07:00')
        title,cameras=self.select()
        self.assertIn('02/10/2026 23:30 → 03/10/2026 02:30',title)
        selected=self.callbacks(cameras,'wqc:')[0]
        title,clips=self.menus.menu(self.archive,selected,actor=43)
        self.assertEqual(self.callbacks(clips,'v:'),['v:'+key[:32] for key in (first,second)])
        self.assertEqual(len(self.callbacks(clips,'f:')),2)
        self.assertEqual(len(self.callbacks(clips,'x:')),2)
        self.assertTrue(all(len(data.encode()) <= 64 for data in self.callbacks(cameras)+self.callbacks(clips)))

    def test_invalid_input_keeps_same_stage_and_can_be_corrected(self):
        self.menus.begin(self.archive,43)
        title,_=self.menus.accept(self.archive,43,'wrong')
        self.assertIn('Ngày giờ chưa đúng',title)
        self.assertEqual(self.menus._session(self.archive,43)['step'],'start')
        self.menus.accept(self.archive,43,'03/10/2026 02:30')
        for value in ('03/10/2026 02:30','02/10/2026 02:30','04/11/2026 02:30'):
            title,_=self.menus.accept(self.archive,43,value)
            self.assertTrue('sau giờ bắt đầu' in title or 'tối đa 31 ngày' in title)
            self.assertEqual(self.menus._session(self.archive,43)['step'],'end')
        title,_=self.menus.accept(self.archive,43,'03/10/2026 03:00')
        self.assertIn('Camera',title)

    def test_maximum_thirty_one_day_range_is_accepted(self):
        title,_=self.select(start='01/10/2026 00:00',end='01/11/2026 00:00')
        self.assertIn('Camera',title)
        self.assertEqual(self.menus._session(self.archive,43)['end_ms']-self.menus._session(self.archive,43)['start_ms'],TimeMenus.MAX_RANGE_MS)

    def test_input_expires_and_cancel_clears_only_own_session(self):
        self.menus.begin(self.archive,42)
        self.menus.begin(self.archive,43)
        self.menus.cancel(self.archive,42)
        self.assertEqual(self.menus._session(self.archive,42),{})
        self.assertEqual(self.menus._session(self.archive,43)['step'],'start')
        self.clock_mock.return_value=self.NOW+TimeMenus.INPUT_TTL+1
        title,buttons=self.menus.accept(self.archive,43,'03/10/2026 02:30')
        self.assertIn('Hết thời gian',title)
        self.assertIn('custom-time',self.callbacks(buttons))
        self.assertEqual(self.menus._session(self.archive,43),{})

    def test_completed_window_expires_and_callback_is_bound_to_actor(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00','2026-10-03T01:01:00+07:00')
        _,buttons=self.select()
        callback=self.callbacks(buttons,'wqc:')[0]
        with self.assertRaises(ValueError):self.menus.menu(self.archive,callback,actor=42)
        with self.assertRaises(ValueError):self.menus.menu(self.archive,callback,actor=999)
        self.clock_mock.return_value=self.NOW+TimeMenus.RANGE_TTL+1
        with self.assertRaises(ValueError):self.menus.menu(self.archive,callback,actor=43)

    def test_allowlist_changes_and_missing_actor_reject_direct_custom_access(self):
        for actor in (None,True,999):
            with self.subTest(actor=actor),self.assertRaises(ValueError):self.menus.begin(self.archive,actor)
        self.menus.begin(self.archive,43)
        self.settings.allowed_users=(42,)
        with self.assertRaises(ValueError):self.menus.accept(self.archive,43,'03/10/2026 02:30')

    def test_two_viewers_inputs_never_interfere(self):
        self.menus.begin(self.archive,42)
        self.menus.begin(self.archive,43)
        self.menus.accept(self.archive,42,'01/10/2026 00:00')
        self.menus.accept(self.archive,43,'03/10/2026 00:00')
        self.menus.accept(self.archive,42,'02/10/2026 00:00')
        self.assertEqual(self.menus._session(self.archive,43)['step'],'end')
        self.menus.accept(self.archive,43,'04/10/2026 00:00')
        self.assertNotEqual(self.menus._session(self.archive,42)['token'],self.menus._session(self.archive,43)['token'])
        self.assertNotEqual(self.menus._session(self.archive,42)['start_ms'],self.menus._session(self.archive,43)['start_ms'])

    def test_sort_paging_back_and_other_commands_retain_exact_range(self):
        self.camera()
        keys=[self.recording(f'2026-10-03T01:{minute:02}:00+07:00',f'2026-10-03T01:{minute:02}:30+07:00') for minute in range(12)]
        _,cameras=self.select()
        selected=self.callbacks(cameras,'wqc:')[0]
        self.menus.dismiss_input(self.archive,43)
        self.clock_mock.return_value=self.NOW+3600
        _,clips=self.menus.menu(self.archive,selected,actor=43)
        sort=next(b['callback_data'] for row in clips for b in row if b['text']=='Mới → cũ')
        _,descending=self.menus.menu(self.archive,sort,actor=43)
        self.assertEqual(self.callbacks(descending,'v:'),['v:'+key[:32] for key in keys[::-1][:10]])
        page=next(b['callback_data'] for row in descending for b in row if b['text']=='Video →')
        _,second=self.menus.menu(self.archive,page,actor=43)
        self.assertEqual(self.callbacks(second,'v:'),['v:'+key[:32] for key in keys[::-1][10:]])
        back=self.callbacks(second,'wq:')[0]
        _,again=self.menus.menu(self.archive,back,actor=43)
        self.assertEqual(self.callbacks(again,'wqc:')[0].split(':')[1],selected.split(':')[1])

    def test_new_range_revokes_old_custom_bookmark_and_input_cancel_halts_consumption(self):
        self.camera()
        self.recording('2026-10-03T01:00:00+07:00','2026-10-03T01:01:00+07:00')
        _,cameras=self.select()
        old=self.callbacks(cameras,'wqc:')[0]
        self.select()
        with self.assertRaises(ValueError):self.menus.menu(self.archive,old,actor=43)
        self.menus.begin(self.archive,43)
        self.menus.dismiss_input(self.archive,43)
        self.assertIsNone(self.menus.accept(self.archive,43,'03/10/2026 02:30'))

    def test_custom_callback_tampering_and_missing_user_are_rejected(self):
        self.select()
        for data in ('wq:000000000000:a:0','wq:'+self.menus._session(self.archive,43)['token']+':a:1',
                     'wq:'+self.menus._session(self.archive,43)['token']+':asc:0','wq:bad:a:0','wqc:'+'0'*65):
            with self.subTest(data=data),self.assertRaises(ValueError):self.menus.menu(self.archive,data,actor=43)

    def test_corrupt_nonfinite_session_expiry_is_rejected(self):
        self.menus.begin(self.archive,43)
        session=self.menus._session(self.archive,43);session['expires']=float('nan')
        self.menus._save_session(self.archive,43,session)
        title,_=self.menus.accept(self.archive,43,'03/10/2026 02:30')
        self.assertIn('Hết thời gian',title)
        self.select()
        session=self.menus._session(self.archive,43);session['expires']=float('inf')
        self.menus._save_session(self.archive,43,session)
        with self.assertRaises(ValueError):self.menus.menu(self.archive,'wq:'+session['token']+':a:0',actor=43)

    def test_alternative_display_zone_rejects_dst_gap_and_repeated_clock_time(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:zone=ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:self.skipTest('OS fixture has no DST zone database')
        self.settings.timezone='America/New_York'
        self.assertIn('America/New_York',self.menus.begin(self.archive,43)[0])
        with patch('archive_app.telegram_menu.get_zone',return_value=zone):
            for value in ('08/03/2026 02:30','01/11/2026 01:30'):
                with self.subTest(value=value),self.assertRaises(ValueError):self.menus.parse_timestamp(value)


if __name__ == '__main__':
    unittest.main()
