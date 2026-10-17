"""Synthetic ICMP messages only; tests open no sockets or camera sessions."""
import socket
import struct
import unittest
from unittest.mock import patch

from archive_app.lan_echo import _checksum, echo_probe


TARGET = '192.168.2.23'
LOCAL = '192.168.2.11'
NONCE = b'0123456789abcdef'
SEQUENCE = 12345
IDENTIFIER = 53210


def reply(*, kind=0, code=0, identifier=IDENTIFIER, sequence=SEQUENCE,
          nonce=NONCE):
    packet = struct.pack('!BBHHH', kind, code, 0, identifier, sequence) + nonce
    return packet[:2] + struct.pack('!H', _checksum(packet)) + packet[4:]


class PingSocket:
    def __init__(self, packets=None, *, fail=None, send_size=24):
        self.packets = list(packets if packets is not None else [(reply(), (TARGET, 0))])
        self.fail = fail
        self.send_size = send_size
        self.timeouts = []
        self.receives = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def bind(self, address):
        self.bound = address
        if self.fail == 'bind':
            raise OSError('synthetic bind failure')

    def sendto(self, packet, address):
        self.sent, self.target = packet, address
        if self.fail == 'send':
            raise OSError('synthetic send failure')
        return self.send_size

    def getsockname(self):
        if self.fail == 'name':
            raise OSError('synthetic socket failure')
        return LOCAL, IDENTIFIER

    def recvfrom(self, size):
        self.receives += 1
        if self.fail == 'receive':
            raise OSError('synthetic receive failure')
        if self.fail == 'timeout' or not self.packets:
            raise socket.timeout()
        packet, peer = self.packets.pop(0)
        return packet[:size], peer


class EchoProbeTests(unittest.TestCase):
    def run_probe(self, connection=None, *, host=TARGET, local=LOCAL, timeout=0.35, clock=None):
        connection = connection or PingSocket()
        with (patch('archive_app.lan_echo.socket.socket', return_value=connection) as factory,
              patch('archive_app.lan_echo.secrets.token_bytes', return_value=NONCE) as random_nonce,
              patch('archive_app.lan_echo.secrets.randbelow', return_value=SEQUENCE) as random_sequence,
              patch('archive_app.lan_echo.time.monotonic', side_effect=clock,
                    return_value=100.0)):
            result = echo_probe(host, local, timeout)
        return result, connection, factory, random_nonce, random_sequence

    def test_exact_echo_proves_current_host_without_tcp_or_raw_socket(self):
        result, connection, factory, nonce, sequence = self.run_probe()
        self.assertTrue(result)
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
        self.assertEqual(connection.bound, (LOCAL, 0))
        self.assertEqual(connection.target, (TARGET, 0))
        self.assertEqual(connection.sent[8:], NONCE)
        self.assertEqual(struct.unpack('!BBHHH', connection.sent[:8]), (8, 0, _checksum(
            struct.pack('!BBHHH', 8, 0, 0, 0, SEQUENCE) + NONCE), 0, SEQUENCE))
        self.assertEqual(_checksum(connection.sent), 0)
        self.assertEqual(connection.receives, 1)
        self.assertTrue(connection.closed)
        nonce.assert_called_once_with(16)
        sequence.assert_called_once_with(65536)

    def test_checksum_known_vector_and_odd_size(self):
        self.assertEqual(_checksum(bytes.fromhex('0001f203f4f5f6f7')), 0x220d)
        self.assertEqual(_checksum(b'\x01'), 0xfeff)

    def test_rejects_wrong_peer(self):
        result, connection, *_ = self.run_probe(PingSocket([(reply(), ('192.168.2.24', 0))]))
        self.assertFalse(result)
        self.assertEqual(connection.receives, 2)

    def test_rejects_other_protocol_messages_and_wrong_echo_identity(self):
        for change in ({'kind': 8}, {'kind': 3}, {'code': 1},
                       {'identifier': IDENTIFIER - 1}, {'sequence': SEQUENCE - 1},
                       {'nonce': b'fedcba9876543210'}):
            with self.subTest(change=change):
                result, *_ = self.run_probe(PingSocket([(reply(**change), (TARGET, 0))]))
                self.assertFalse(result)

    def test_rejects_bad_checksum(self):
        packet = bytearray(reply())
        packet[2] ^= 1
        result, *_ = self.run_probe(PingSocket([(bytes(packet), (TARGET, 0))]))
        self.assertFalse(result)

    def test_rejects_truncated_or_oversized_packets(self):
        for packet in (b'', b'\0' * 7, reply()[:-1], reply() + b'x', reply() + b'x' * 500):
            with self.subTest(size=len(packet)):
                result, *_ = self.run_probe(PingSocket([(packet, (TARGET, 0))]))
                self.assertFalse(result)

    def test_skips_noise_then_accepts_exact_reply(self):
        packets = [(reply(sequence=1), (TARGET, 0)), (reply(), ('192.168.2.24', 0)),
                   (reply(), (TARGET, 0))]
        result, connection, *_ = self.run_probe(PingSocket(packets))
        self.assertTrue(result)
        self.assertEqual(connection.receives, 3)

    def test_noise_packet_count_is_bounded(self):
        packets = [(reply(sequence=1), (TARGET, 0))] * 8 + [(reply(), (TARGET, 0))]
        result, connection, *_ = self.run_probe(PingSocket(packets))
        self.assertFalse(result)
        self.assertEqual(connection.receives, 8)
        self.assertEqual(len(connection.packets), 1)

    def test_socket_errors_fail_closed_and_close_socket(self):
        for step in ('bind', 'send', 'name', 'receive', 'timeout'):
            with self.subTest(step=step):
                result, connection, *_ = self.run_probe(PingSocket(fail=step))
                self.assertFalse(result)
                self.assertTrue(connection.closed)

    def test_permission_or_platform_error_does_not_attempt_raw_or_tcp_fallback(self):
        with patch('archive_app.lan_echo.socket.socket', side_effect=PermissionError) as factory:
            self.assertFalse(echo_probe(TARGET, LOCAL, 0.1))
        factory.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)

    def test_short_send_fails_closed(self):
        result, connection, *_ = self.run_probe(PingSocket(send_size=23))
        self.assertFalse(result)
        self.assertEqual(connection.receives, 0)

    def test_zero_identifier_fails_closed(self):
        connection = PingSocket()
        connection.getsockname = lambda: (LOCAL, 0)
        result, connection, *_ = self.run_probe(connection)
        self.assertFalse(result)
        self.assertEqual(connection.receives, 0)

    def test_numeric_ipv4_addresses_only_no_dns(self):
        for host, local in (('camera.local', LOCAL), (TARGET, 'box.local'), ('::1', LOCAL),
                            ('', LOCAL), (TARGET, '0.0.0.0'), ('0.0.0.0', LOCAL),
                            ('224.0.0.1', LOCAL), ('255.255.255.255', LOCAL)):
            with self.subTest(host=host, local=local):
                result, _, factory, *_ = self.run_probe(host=host, local=local)
                self.assertFalse(result)
                factory.assert_not_called()

    def test_invalid_timeout_opens_no_socket(self):
        for timeout in (0, -1, float('nan'), float('inf'), True, '1', None):
            with self.subTest(timeout=timeout):
                result, _, factory, *_ = self.run_probe(timeout=timeout)
                self.assertFalse(result)
                factory.assert_not_called()

    def test_timeout_is_capped_at_one_second(self):
        result, connection, *_ = self.run_probe(timeout=30)
        self.assertTrue(result)
        self.assertTrue(connection.timeouts)
        self.assertTrue(all(0 < value <= 1 for value in connection.timeouts))

    def test_time_budget_decreases_before_each_receive(self):
        packets = [(reply(sequence=1), (TARGET, 0)), (reply(), (TARGET, 0))]
        result, connection, *_ = self.run_probe(PingSocket(packets), timeout=0.5,
                                              clock=[100, 100.1, 100.2, 100.3, 100.4, 100.49])
        self.assertTrue(result)
        for actual, expected in zip(connection.timeouts, (0.4, 0.3, 0.2, 0.1)):
            self.assertAlmostEqual(actual, expected)

    def test_expired_budget_does_not_send_or_receive(self):
        result, connection, *_ = self.run_probe(timeout=0.1, clock=[100, 100.11])
        self.assertFalse(result)
        self.assertFalse(hasattr(connection, 'sent'))
        self.assertEqual(connection.receives, 0)

    def test_expired_budget_after_send_does_not_receive(self):
        result, connection, *_ = self.run_probe(timeout=0.5, clock=[100, 100.1, 100.2, 100.6])
        self.assertFalse(result)
        self.assertEqual(connection.receives, 0)

    def test_reply_processed_after_deadline_is_not_accepted(self):
        result, connection, *_ = self.run_probe(timeout=0.5, clock=[100, 100.1, 100.2, 100.3, 100.6])
        self.assertFalse(result)
        self.assertEqual(connection.receives, 1)


if __name__ == '__main__':
    unittest.main()
