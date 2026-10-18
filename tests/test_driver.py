import ctypes as C
import os
import select
import socket
import struct
import threading
import time
import unittest
from unittest.mock import Mock, patch

import serial
from PIL import Image

from linux_dlp_driver.cli import FrameSender, main
from linux_dlp_driver.edid import make_edid
from linux_dlp_driver.evdi import Buffer, Ddcci, Events, Mode, Monitor, Rect
from linux_dlp_driver.pixels import pack_frame
from linux_dlp_driver.protocol import DisplayLink, TcpStream, decode_cell, encode_cell, tcp_endpoint
from peer import Peer


class EdidTests(unittest.TestCase):
    def test_modes(self):
        for width, height in ((240, 64), (960, 256), (1920, 1080), (3840, 2160)):
            with self.subTest(width=width):
                edid = make_edid(width, height)
                self.assertEqual(len(edid), 128)
                self.assertEqual(sum(edid) % 256, 0)
                self.assertEqual(edid[:8], b"\x00\xff\xff\xff\xff\xff\xff\x00")
                timing = edid[54:72]
                self.assertEqual(timing[2] + (timing[4] >> 4) * 256, width)
                self.assertEqual(timing[5] + (timing[7] >> 4) * 256, height)
                vblank = timing[6] + (timing[7] & 15) * 256
                hz = struct.unpack_from("<H", timing)[0] * 10000 / ((width + 160) * (height + vblank))
                self.assertAlmostEqual(hz, 60, delta=.12)
                self.assertGreaterEqual(struct.unpack_from("<H", timing)[0], 1000)
                self.assertEqual(edid[35:38], bytes(3))
                self.assertEqual(edid[38:54], b"\x01\x01" * 8)

    def test_invalid(self):
        for dims in ((0, 64), (240, 0), (4000, 64), (240, 3000)):
            with self.assertRaises(ValueError):
                make_edid(*dims)


class PixelTests(unittest.TestCase):
    def test_lsb_polarity_and_padding(self):
        data = b"".join(bytes((v, v, v, 0)) for v in (0, 255, 0, 255, 255, 255, 255, 255, 0))
        self.assertEqual(pack_frame(data, 9, 1, (9, 1)), b"\x05\x01")
        self.assertEqual(pack_frame(data, 9, 1, (9, 1), invert=True), b"\xfa\x00")

    def test_rows_and_scale(self):
        data = bytes(4 * 13) + b"\xff\xff\xff\x00" * 13
        self.assertEqual(pack_frame(data, 13, 2, (13, 2)), b"\xff\x1f\x00\x00")
        self.assertEqual(pack_frame(bytes(4 * 26 * 4), 26, 4, (13, 2)), b"\xff\x1f" * 2)

    def test_bad_length(self):
        with self.assertRaises(ValueError):
            pack_frame(b"", 1, 1, (1, 1))

    def test_dither_is_valid(self):
        for dither in (False, True):
            data = bytes((128, 128, 128, 0)) * 26
            frame = pack_frame(data, 13, 2, (13, 2), dither=dither)
            self.assertEqual(len(frame), 4)
            self.assertFalse(frame[1] & 0xe0 or frame[3] & 0xe0)


class TransportTests(unittest.TestCase):
    def exercise(self, port, peer, compact):
        link = DisplayLink(port, timeout=.15, compact=compact)
        self.assertEqual(link.connect(), (13, 2))
        self.assertEqual(link.window_size, 0)
        peer.drop_present_once = True
        first, second = b"\xa5\x00\x5a\x00", b"\x81\x10\x42\x01"
        link.send_frame(first)
        self.assertEqual(peer.presented, 1)  # retry must not PRESENT twice
        self.assertGreaterEqual(peer.requests.count(0x13), 2)
        link.send_frame(second)
        self.assertEqual(peer.pixels, second)
        self.assertEqual(peer.presented, 2)
        self.assertIsNone(peer.error)

    def test_tcp_fragmentation_retry_and_legacy(self):
        for compact in (True, False):
            with self.subTest(compact=compact):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                try:
                    with TcpStream(listener.getsockname()) as port:
                        conn, _ = listener.accept()
                        conn.settimeout(.05)
                        def read(size):
                            try:
                                return conn.recv(size)
                            except TimeoutError:
                                return b""
                        peer = Peer(read, conn.sendall, conn.close)
                        try:
                            self.exercise(port, peer, compact)
                        finally:
                            peer.close()
                finally:
                    listener.close()

    def test_serial_pty(self):
        master, slave = os.openpty()
        try:
            with serial.Serial(os.ttyname(slave), 115200, timeout=.02, write_timeout=1, exclusive=True) as port:
                def read(size):
                    return os.read(master, size) if select.select([master], [], [], .02)[0] else b""
                peer = Peer(read, lambda data: os.write(master, data), lambda: None)
                try:
                    self.exercise(port, peer, True)
                finally:
                    peer.close()
        finally:
            os.close(master)
            os.close(slave)

    def test_tcp_disconnect(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            with TcpStream(listener.getsockname()) as port:
                conn, _ = listener.accept()
                conn.close()
                with self.assertRaisesRegex(OSError, "disconnected"):
                    port.read(1)
        finally:
            listener.close()

    def test_crc_rejects_corruption(self):
        data = bytearray(encode_cell(1, 0, 1, 0, compact=True))
        data[-1] ^= 1
        with self.assertRaises(ValueError):
            decode_cell(data)


class FakeEvdi:
    def __init__(self):
        self.calls = []
        self.buffers = {}
        self.read_fd, self.write_fd = os.pipe()
        self.request_ready = True

    def evdi_open_attached_to(self, parent):
        assert parent == b""
        return 123
    def evdi_open(self, device): return 123
    def evdi_connect(self, *args): self.calls.append("connect")
    def evdi_connect2(self, *args): self.calls.append("connect")
    def evdi_get_event_ready(self, handle): return self.read_fd
    def evdi_disconnect(self, handle): self.calls.append("disconnect")
    def evdi_close(self, handle): self.calls.append("close")
    def evdi_register_buffer(self, handle, buffer):
        self.buffers[buffer.id] = buffer
        self.calls.append(("register", buffer.id))
    def evdi_unregister_buffer(self, handle, id):
        del self.buffers[id]
        self.calls.append(("unregister", id))
    def evdi_request_update(self, handle, id): return self.request_ready
    def evdi_grab_pixels(self, handle, rects, count):
        buffer = next(iter(self.buffers.values()))
        C.memset(buffer.buffer, 0x7f, buffer.width * buffer.height * 4)
        C.cast(count, C.POINTER(C.c_int))[0] = 1
    def evdi_ddcci_response(self, *args): self.calls.append("ddcci")
    def close(self):
        os.close(self.read_fd)
        os.close(self.write_fd)


class EvdiTests(unittest.TestCase):
    def setUp(self):
        self.lib = FakeEvdi()
        self.monitor = Monitor(240, 64, library=self.lib)

    def tearDown(self):
        self.monitor.close()
        self.lib.close()

    def mode(self, format=0x34325258):
        self.monitor.events.mode(Mode(240, 64, 60, 32, format), None)

    def test_prefers_fixed_open_api(self):
        self.monitor.close()
        self.lib.evdi_open_attached_to_fixed = Mock(return_value=123)
        self.lib.evdi_open_attached_to = Mock(side_effect=AssertionError("deprecated API called"))
        self.monitor = Monitor(240, 64, library=self.lib)
        self.lib.evdi_open_attached_to_fixed.assert_called_once_with(None, 0)

    def test_ready_buffer_does_not_wait_for_an_event(self):
        self.mode()
        with patch("linux_dlp_driver.evdi.select.select", return_value=([], [], [])) as wait:
            self.assertIsNotNone(self.monitor.poll(.05))
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(wait.call_args.args[-1], 0)

    def test_pixel_rate_limit_matches_high_refresh(self):
        self.monitor.close()
        self.lib.evdi_connect2 = Mock()
        self.monitor = Monitor(240, 64, refresh=240, library=self.lib)
        self.assertEqual(self.lib.evdi_connect2.call_args.args[-2:],
                         (240 * 64, 240 * 64 * 241))

    def test_capture_mode_changes_and_teardown(self):
        self.assertIsNone(self.monitor.poll(0))
        self.mode()
        self.assertEqual(len(self.monitor.poll(0)), 240 * 64 * 4)
        first_id = self.monitor.buffer_id
        self.mode()
        self.assertNotEqual(first_id, self.monitor.buffer_id)
        self.monitor.events.update(first_id, None)
        self.assertFalse(self.monitor.ready)  # stale async completion ignored
        self.monitor.close()
        self.monitor.close()
        self.assertEqual(self.lib.calls.count("close"), 1)
        self.assertFalse(self.lib.buffers)

    def test_async_and_dpms(self):
        self.mode()
        self.lib.request_ready = False
        self.assertIsNone(self.monitor.poll(0))
        self.assertTrue(self.monitor.pending)
        self.monitor.events.update(self.monitor.buffer_id, None)
        self.assertIsNotNone(self.monitor.poll(0))
        self.monitor.events.dpms(3, None)
        self.monitor.events.crtc(1, None)
        self.assertIsNone(self.monitor.poll(0))

    def test_callback_error_is_deferred(self):
        self.monitor.events.mode(Mode(640, 480, 60, 32, 0x34325258), None)
        with self.assertRaisesRegex(RuntimeError, "Unexpected monitor mode"):
            self.monitor.poll(0)

    def test_format_validation(self):
        self.mode(123)
        with self.assertRaisesRegex(RuntimeError, "32-bit"):
            self.monitor.poll(0)

    def test_all_evdi_rgb_formats(self):
        for format in (0x34325258, 0x34325241, 0x34324258, 0x34324241):
            self.mode(format)
            self.assertIsNotNone(self.monitor.poll(0))


class SenderTests(unittest.TestCase):
    def test_unplug_detected_before_first_desktop_frame(self):
        class Link:
            def command(self, opcode):
                raise OSError("link unplugged before modeset")
        sender = FrameSender(Link(), 10)
        sender.start()
        try:
            sender.thread.join(2)
            self.assertIsInstance(sender.error, OSError)
        finally:
            sender.stop()

    def test_slow_upload_keeps_only_latest_and_propagates_errors(self):
        started, release = threading.Event(), threading.Event()
        frames = []
        class Link:
            width, height = 8, 1
            pixel_format = 0
            def send_frame(self, pixels):
                frames.append(pixels)
                if len(frames) == 1:
                    started.set()
                    release.wait(2)
                else:
                    raise OSError("link unplugged")
        sender = FrameSender(Link(), 1000)
        sender.start()
        try:
            sender.submit(bytes(32), 8, 1)
            self.assertTrue(started.wait(1))
            for _ in range(100):
                sender.submit(bytes(32), 8, 1)
            sender.submit(b"\xff\xff\xff\x00" * 8, 8, 1)
            release.set()
            sender.thread.join(2)
            self.assertEqual(frames, [b"\xff", b"\x00"])
            self.assertIsInstance(sender.error, OSError)
        finally:
            release.set()
            sender.stop()


class LifecycleTests(unittest.TestCase):
    def test_reconnect_closes_old_transport_and_starts_new_session(self):
        stop = threading.Event()
        stop.wait = Mock(return_value=False)
        calls = []
        class Port:
            def __enter__(self):
                calls.append("open")
                return self
            def __exit__(self, *_): calls.append("close")
        class Link:
            width, height = 240, 64
            pixel_format = 0
        def run(link, args, event, library):
            calls.append("monitor")
            if calls.count("monitor") == 1:
                raise OSError("cable unplugged")
            event.set()
        with patch("linux_dlp_driver.cli.threading.Event", return_value=stop), \
             patch("linux_dlp_driver.cli.evdi.load_library", return_value=object()), \
             patch("linux_dlp_driver.cli.open_transport", side_effect=lambda args: Port()), \
             patch("linux_dlp_driver.cli.connect", return_value=Link()) as connect, \
             patch("linux_dlp_driver.cli.run_monitor", side_effect=run):
            self.assertEqual(main([]), 0)
        self.assertEqual(calls, ["open", "monitor", "close", "open", "monitor", "close"])
        self.assertEqual(connect.call_count, 2)
        stop.wait.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
