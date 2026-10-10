import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_app.__main__ import main


class NetworkRoutesCLITest(unittest.TestCase):
    def test_once_does_not_parse_archive_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            output = str(Path(temp) / 'subnets.json')
            routes = [{'cidr': '192.168.31.0/24', 'interface': 'tailscale0', 'source': 'tailscale', 'table': 52}]
            with patch('sys.argv', ['archive_app', 'network-routes', '--once', '--output', output]), \
                 patch('archive_app.__main__.Settings.from_env', side_effect=AssertionError('No settings needed')), \
                 patch('archive_app.network_routes.collect_subnets', return_value=routes), \
                 patch('archive_app.network_routes.write_snapshot') as write:
                self.assertEqual(main(), 0)
                write.assert_called_once_with(output, routes)

    def test_loop_receives_stop_event_and_interval(self):
        with patch('sys.argv', ['archive_app', 'network-routes', '--interval', '15']), \
             patch('archive_app.__main__.Settings.from_env', side_effect=AssertionError('No settings needed')), \
             patch('archive_app.__main__.signal.signal'), \
             patch('archive_app.network_routes.collect_loop') as loop:
            self.assertEqual(main(), 0)
            self.assertEqual(loop.call_args.args, ('/network/subnets.json',))
            self.assertEqual(loop.call_args.kwargs['interval'], 15)
            self.assertFalse(loop.call_args.kwargs['stop_event'].is_set())

    def test_unbounded_refresh_rejected(self):
        with patch('sys.argv', ['archive_app', 'network-routes', '--interval', '0']):
            with self.assertRaises(ValueError):
                main()
