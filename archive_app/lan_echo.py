"""Bounded IPv4 echo proof using Linux unprivileged ICMP datagram sockets.

The observer sends one random challenge and never opens a TCP connection or a
raw socket.  A reply proves only current IP reachability; the caller separately
checks its LAN neighbour mapping before publishing an IP/MAC identity.
"""
import hmac
import ipaddress
import math
import secrets
import socket
import struct
import time


_HEADER = struct.Struct('!BBHHH')
_NONCE_SIZE = 16
_PACKET_SIZE = _HEADER.size + _NONCE_SIZE
_MAX_PACKETS = 8


def _checksum(packet):
    """Return the RFC 1071 one's-complement checksum in network byte order."""
    if len(packet) % 2:
        packet += b'\0'
    total = sum(struct.unpack('!' + 'H' * (len(packet) // 2), packet))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return (~total) & 0xffff


def echo_probe(host, local_address, timeout):
    """Match one fresh ICMP echo reply within at most one second; fail closed.

    Numeric IPv4 endpoints avoid DNS resolution.  Linux assigns the ICMP ID to
    the datagram socket's local port and rewrites the outgoing ID/checksum; the
    reply must match that kernel ID, our random sequence and our full nonce.
    Missing ping-socket permission is an ordinary failed probe, not a reason to
    fall back to raw sockets or camera TCP connections.
    """
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or timeout <= 0):
        return False
    deadline = time.monotonic() + min(timeout, 1.0)
    try:
        target = ipaddress.IPv4Address(host)
        source = ipaddress.IPv4Address(local_address)
    except (ipaddress.AddressValueError, TypeError):
        return False
    if (target.is_unspecified or target.is_multicast or
            source.is_unspecified or source.is_multicast or
            int(target) == 0xffffffff or int(source) == 0xffffffff):
        return False
    target, source = str(target), str(source)
    nonce = secrets.token_bytes(_NONCE_SIZE)
    sequence = secrets.randbelow(0x10000)
    packet = _HEADER.pack(8, 0, 0, 0, sequence) + nonce
    packet = _HEADER.pack(8, 0, _checksum(packet), 0, sequence) + nonce
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP) as connection:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            connection.settimeout(remaining)
            connection.bind((source, 0))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            connection.settimeout(remaining)
            if connection.sendto(packet, (target, 0)) != len(packet):
                return False
            identifier = connection.getsockname()[1]
            if not 1 <= identifier <= 0xffff:
                return False
            for _ in range(_MAX_PACKETS):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                connection.settimeout(remaining)
                reply, peer = connection.recvfrom(_PACKET_SIZE + 1)
                if peer[0] != target or len(reply) != _PACKET_SIZE:
                    continue
                kind, code, _, reply_id, reply_sequence = _HEADER.unpack_from(reply)
                if (kind == 0 and code == 0 and reply_id == identifier and
                        reply_sequence == sequence and _checksum(reply) == 0 and
                        hmac.compare_digest(reply[_HEADER.size:], nonce)):
                    return time.monotonic() <= deadline
    except OSError:
        return False
    return False
