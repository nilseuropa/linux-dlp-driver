import socket
import struct
import unittest

from PIL import Image

from linux_dlp_driver.edid import make_edid
from linux_dlp_driver.pixels import pack_frame, pack_image
from linux_dlp_driver.protocol import (Cell, DisplayLink, GET_INFO, GET_EXTENSIONS,
    MONO1, NEGOTIATE_WINDOW, ProtocolError, RGB565, TcpStream, changed_rects)
from peer import Peer


class ColorTests(unittest.TestCase):
    def test_primary_colors_are_big_endian_rgb565(self):
        colors = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (0, 0, 0))
        bgra = b"".join(bytes((b, g, r, 0)) for r, g, b in colors)
        self.assertEqual(pack_frame(bgra, 5, 1, (5, 1), pixel_format=RGB565),
                         bytes.fromhex("f800 07e0 001f ffff 0000"))
        self.assertEqual(pack_frame(bgra, 5, 1, (5, 1), pixel_format=RGB565, invert=True),
                         bytes.fromhex("07ff f81f ffe0 0000 ffff"))

    def test_channel_quantization(self):
        colors = [(i, 255 - i, i * 71 % 256) for i in range(256)]
        bgra = b"".join(bytes((b, g, r, 0)) for r, g, b in colors)
        expected = b"".join(struct.pack(">H", ((r >> 3) << 11) | ((g >> 2) << 5) | b >> 3)
                            for r, g, b in colors)
        self.assertEqual(pack_frame(bgra, 256, 1, (256, 1), pixel_format=RGB565), expected)

    def test_color_padding_is_white_and_resize_keeps_color(self):
        image = Image.new("RGB", (1, 1), "red")
        self.assertEqual(pack_image(image, (3, 1), pixel_format=RGB565),
                         bytes.fromhex("ffff f800 ffff"))
        self.assertEqual(pack_image(image, (3, 3), pixel_format=RGB565), b"\xf8\x00" * 9)

    def test_rgb565_changed_pixels_do_not_require_byte_alignment(self):
        before = bytes(13 * 10 * 2)
        after = bytearray(before)
        after[(9 * 13 + 11) * 2 + 1] = 1
        self.assertEqual(changed_rects(before, after, 13, 10, RGB565), [(11, 8, 1, 2)])
        self.assertEqual(changed_rects(after, after, 13, 10, RGB565), [])

    def test_color_edid(self):
        mono, color = make_edid(480, 320), make_edid(480, 320, color=True)
        self.assertEqual(mono[24] & 0x18, 0)
        self.assertEqual(color[24] & 0x18, 8)
        self.assertEqual(sum(color) % 256, 0)

    def test_real_tcp_color_uploads_partial_rectangles_rle_and_retry(self):
        for compact in (False, True):
            for rle in (False, True):
                with self.subTest(compact=compact, rle=rle):
                    listener = socket.socket()
                    listener.bind(("127.0.0.1", 0))
                    listener.listen()
                    try:
                        with TcpStream(listener.getsockname()) as port:
                            conn, _ = listener.accept()
                            conn.settimeout(.02)
                            def read(size):
                                try: return conn.recv(size)
                                except TimeoutError: return b""
                            peer = Peer(read, conn.sendall, conn.close, width=13, height=10,
                                        formats=17, max_rect_bytes=78, rle=rle)
                            try:
                                link = DisplayLink(port, timeout=.15, compact=compact)
                                self.assertEqual(link.connect(), (13, 10))
                                self.assertEqual(link.pixel_format, RGB565)
                                self.assertEqual(peer.format, RGB565)
                                peer.drop_present_once = True
                                frame = bytes(260) if rle else bytes(i % 256 for i in range(260))
                                link.send_frame(frame)
                                self.assertEqual(peer.presented, 1)
                                self.assertEqual(peer.pixels, frame)
                                if rle:
                                    self.assertIn(0x15, peer.requests)
                                peer.rectangles.clear()
                                after = bytearray(frame)
                                after[(9 * 13 + 11) * 2 + 1] ^= 1
                                link.send_frame(after)
                                self.assertEqual(peer.pixels, after)
                                self.assertEqual(peer.rectangles, [(11, 8, 1, 2)])
                                self.assertIsNone(peer.error)
                            finally:
                                peer.close()
                    finally:
                        listener.close()

    def test_negotiation_overrides_and_unsupported_formats(self):
        class DescriptorLink(DisplayLink):
            def __init__(self, formats, **kwargs):
                super().__init__(None, **kwargs)
                self.advertised = formats
                self.configured = None
            def command(self, opcode, payload=b"", **kwargs):
                if opcode == GET_INFO:
                    p = bytearray(128)
                    struct.pack_into("<H", p, 0, 128)
                    struct.pack_into("<HHI", p, 4, 13, 2, self.advertised)
                    struct.pack_into("<I", p, 28, 52)
                    return Cell(2, opcode, 0, 1, 0, 42, p)
                if opcode in (GET_EXTENSIONS, NEGOTIATE_WINDOW):
                    raise ProtocolError(opcode, 1)
                self.configured = struct.unpack("<HH", payload)[0]
                return Cell(2, opcode, self.session, 2, 0, 42, b"")
        for formats, requested, expected in ((1, "auto", MONO1), (16, "auto", RGB565),
                (17, "auto", RGB565), (17, "mono1", MONO1), (17, "rgb565", RGB565)):
            link = DescriptorLink(formats, pixel_format=requested)
            link.connect()
            self.assertEqual(link.pixel_format, expected)
            self.assertEqual(link.configured, expected)
        for formats, requested in ((1, "rgb565"), (16, "mono1"), (8, "auto")):
            with self.assertRaises(ValueError):
                DescriptorLink(formats, pixel_format=requested).connect()
