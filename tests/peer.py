"""Small independent stop-and-wait MONO1 peer for real transport tests."""
import os
import select
import struct
import threading
import time
import zlib


class Peer:
    def __init__(self, read, write, close, *, width=13, height=2, formats=1,
                 max_rect_bytes=None, rle=False, lz4=False):
        self.read, self.write, self.close_io = read, write, close
        self.width, self.height, self.formats = width, height, formats
        self.format = 0
        self.rle = rle
        self.lz4 = lz4
        self.max_rect_bytes = max_rect_bytes or (width * 2 if formats & 16 else (width + 7) // 8) * height
        self.rectangles = []
        self.buffer = bytearray()
        self.pixels = bytearray((width + 7) // 8 * height)
        self.presented = 0
        self.drop_present_once = False
        self.requests = []
        self.replay = {}
        self.error = None
        self.closed = False
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def reply(self, request):
        magic, version, reserved, kind, op, session, seq, result, n, boot = struct.unpack_from("<4sBBBBIIHHQ", request)
        self.requests.append(op)
        key = (session, seq)
        if key in self.replay:
            return self.replay[key]
        p = request[32:32 + n]
        result, payload = 0, b""
        if op == 1:
            payload = bytearray(128)
            struct.pack_into("<H", payload, 0, 128)
            struct.pack_into("<HHI", payload, 4, self.width, self.height, self.formats)
            struct.pack_into("<I", payload, 16, 1)  # preserve
            struct.pack_into("<I", payload, 28, self.max_rect_bytes)
            struct.pack_into("<I", payload, 36, 100)
        elif op == 2:
            self.format, reserved = struct.unpack("<HH", p)
            assert reserved == 0 and self.formats & (1 << self.format)
            self.stride = self.width * 2 if self.format == 4 else (self.width + 7) // 8
            self.pixels = bytearray(self.stride * self.height)
        elif op == 4 and self.rle:
            payload = struct.pack("<I", 32)
        elif op == 8 and self.lz4:
            payload = struct.pack("<I", 128)
        elif op in (4, 6, 8):
            result = 1  # unsupported: exercise legacy fallback
        elif op == 0x10:
            self.frame = struct.unpack_from("<I", p)[0]
        elif op == 0x11:
            _, _, self.x, self.y, self.w, self.h = struct.unpack("<IIHHHH", p)
            self.rectangles.append((self.x, self.y, self.w, self.h))
        elif op in (0x12, 0x15, 0x16):
            _, _, offset = struct.unpack_from("<III", p)
            data = p[12:]
            if op == 0x15:
                count = struct.unpack_from("<H", data)[0]
                encoded, output, at = data[2:], bytearray(), 0
                while at < len(encoded):
                    tag = encoded[at]
                    at += 1
                    if tag & 128:
                        output.extend(bytes((encoded[at],)) * ((tag & 127) + 3))
                        at += 1
                    else:
                        output.extend(encoded[at:at + tag + 1])
                        at += tag + 1
                assert len(output) == count
                data = output
            elif op == 0x16:
                from lz4.block import decompress
                count = struct.unpack_from("<H", data)[0]
                assert 0 < count <= 1024
                data = decompress(data[2:], uncompressed_size=count)
                assert len(data) == count
            row = self.w * 2 if self.format == 4 else (self.w + 7) // 8
            xbyte = self.x * 2 if self.format == 4 else self.x // 8
            for at, value in enumerate(data, offset):
                dy, dx = divmod(at, row)
                assert dy < self.h
                self.pixels[(self.y + dy) * self.stride + xbyte + dx] = value
        elif op == 0x13:
            self.presented += 1
            self.completed = self.frame
        elif op == 3:
            payload = bytearray(24)
            struct.pack_into("<I", payload, 8, self.completed)
        header = struct.pack("<4sBBBBIIHHQ4x", magic, 1, 0, 2, op, session, seq,
                             result, len(payload), 42)
        wire = header + payload
        if magic == b"SDLP":
            wire += bytes(252 - len(wire))
        wire += struct.pack("<I", zlib.crc32(wire))
        self.replay[key] = wire
        if op == 0x13 and self.drop_present_once:
            self.drop_present_once = False
            return b""
        return wire

    def run(self):
        try:
            while not self.closed:
                data = self.read(256)
                if not data:
                    time.sleep(.001)
                    continue
                self.buffer.extend(data)
                while len(self.buffer) >= 32:
                    size = 36 + struct.unpack_from("<H", self.buffer, 18)[0] if self.buffer[:4] == b"DLP1" else 256
                    if len(self.buffer) < size:
                        break
                    request = bytes(self.buffer[:size])
                    del self.buffer[:size]
                    assert zlib.crc32(request[:-4]) == struct.unpack("<I", request[-4:])[0]
                    response = self.reply(request)
                    # Deliberately fragment replies across host reads.
                    for at in range(0, len(response), 7):
                        self.write(response[at:at + 7])
        except (OSError, EOFError):
            if not self.closed:
                self.error = RuntimeError("Peer transport closed unexpectedly")
        except BaseException as error:
            self.error = error

    def close(self):
        self.closed = True
        self.close_io()
        self.thread.join(2)
