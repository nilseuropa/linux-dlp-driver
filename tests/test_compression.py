import random
import socket
import struct
import unittest

from linux_dlp_driver.cli import parser
from linux_dlp_driver.protocol import (Cell, DisplayLink, GET_CODECS, GET_INFO, GET_EXTENSIONS,
                                      NEGOTIATE_WINDOW, ProtocolError, RECT_DATA, RECT_LZ4, TcpStream)
from peer import Peer


class CompressionTests(unittest.TestCase):
    def test_flag_is_optional(self):
        self.assertFalse(parser().parse_args([]).compressed)
        self.assertTrue(parser().parse_args(["--compressed"]).compressed)

    def test_lossless_color_opt_in_and_legacy_fallback(self):
        for compact in (False, True):
            for supported in (False, True):
                for enabled in (False, True):
                    with self.subTest(compact=compact, supported=supported, enabled=enabled):
                        with socket.socket() as listener:
                            listener.bind(("127.0.0.1", 0))
                            listener.listen()
                            with TcpStream(listener.getsockname()) as port:
                                connection, _ = listener.accept()
                                connection.settimeout(.05)

                                def read(size):
                                    try:
                                        return connection.recv(size)
                                    except TimeoutError:
                                        return b""

                                peer = Peer(read, connection.sendall, connection.close,
                                            width=64, height=32, formats=17, rle=True, lz4=supported)
                                try:
                                    link = DisplayLink(port, timeout=.2, compact=compact, compressed=enabled)
                                    link.connect()
                                    peer.drop_present_once = True
                                    image = b"\xf8\0" * (64 * 32)
                                    link.send_frame(image)
                                    self.assertEqual(bytes(peer.pixels), image)
                                    self.assertEqual(peer.presented, 1)
                                    self.assertEqual(GET_CODECS in peer.requests, enabled)
                                    self.assertEqual(RECT_LZ4 in peer.requests, enabled and supported)
                                    after = bytearray(image)
                                    after[-1] ^= 1
                                    link.send_frame(after)
                                    self.assertEqual(bytes(peer.pixels), after)
                                    link.connect()
                                    start = len(peer.requests)
                                    noise = random.Random(713).randbytes(len(image))
                                    link.send_frame(noise)
                                    self.assertEqual(bytes(peer.pixels), noise)
                                    self.assertIn(RECT_DATA, peer.requests[start:])
                                    self.assertNotIn(RECT_LZ4, peer.requests[start:])
                                finally:
                                    peer.close()

    def test_malformed_codec_response_fails(self):
        class InvalidCodecLink(DisplayLink):
            def command(self, opcode, *args, **kwargs):
                payload = b""
                if opcode == GET_INFO:
                    payload = bytearray(128)
                    struct.pack_into("<H", payload, 0, 128)
                    struct.pack_into("<HHI", payload, 4, 13, 2, 1)
                    struct.pack_into("<I", payload, 28, 4)
                elif opcode in (GET_EXTENSIONS, NEGOTIATE_WINDOW):
                    raise ProtocolError(opcode, 1)
                elif opcode == GET_CODECS:
                    payload = self.invalid
                return Cell(2, opcode, self.session, 1, 0, 42, payload)
        for payload in (b"", b"\x80", struct.pack("<I", 256)):
            link = InvalidCodecLink(None, compressed=True)
            link.invalid = payload
            with self.assertRaisesRegex(ValueError, "Invalid DLP codecs"):
                link.connect()
