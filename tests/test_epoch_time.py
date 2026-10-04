"""UTC arithmetic must work without a platform 32-bit C time_t conversion."""
from datetime import datetime,timezone
import unittest
from unittest.mock import patch

from archive_app.core import from_epoch_ms,get_zone
import test_bot_controls as fixtures


class NoPlatformTime(datetime):
    @classmethod
    def fromtimestamp(cls,*args,**kwargs):raise OverflowError('simulated 32-bit time_t')


class EpochArithmeticTests(unittest.TestCase):
    def test_future_2069_2099_and_negative_epoch_use_utc_arithmetic(self):
        with patch('archive_app.core.datetime',NoPlatformTime):
            for year in (1969,2038,2069,2099):
                expected=datetime(year,1,1,tzinfo=timezone.utc)
                actual=from_epoch_ms(int(expected.timestamp()*1000),get_zone('UTC+07:00'))
                self.assertEqual(actual.year,year)
                self.assertEqual(actual.hour,7)
                self.assertEqual(actual.timestamp(),expected.timestamp())


class FutureCatalogTests(unittest.TestCase):
    setUp=fixtures.BotControlTests.setUp
    tearDown=fixtures.BotControlTests.tearDown
    fake=fixtures.BotControlTests.fake

    def test_future_calendar_menu_and_caption_do_not_use_time_t(self):
        zone=get_zone('UTC+07:00')
        start=int(datetime(2069,1,1,10,tzinfo=zone).timestamp()*1000)
        with self.archive.conn:
            self.archive.conn.execute('UPDATE recordings SET start_ms=?,end_ms=? WHERE key=?',(start,start+60000,self.key))
        row=dict(self.archive.conn.execute('SELECT * FROM recordings WHERE key=?',(self.key,)).fetchone())
        with patch('archive_app.core.datetime',NoPlatformTime),patch('archive_app.telegram.datetime',NoPlatformTime):
            calendar=self.archive.calendar('front')
            self.assertEqual(calendar['years'][0]['year'],2069)
            self.assertIn('2069-01-01T10:00:00+07:00',self.telegram.caption(self.archive,row))
            text,buttons=self.telegram.menu(self.archive,'d:'+self.telegram.camera_token('front')+':2069-01-01:asc',actor=43)
            self.assertIn('2069-01-01',text)
            self.assertTrue(any(b.get('callback_data','').startswith('bd:') for line in buttons for b in line))


if __name__=='__main__':unittest.main()
