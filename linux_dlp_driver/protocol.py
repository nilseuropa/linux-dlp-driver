#!/usr/bin/env python3
"""DLP byte-stream client with MONO1 and RGB565 color negotiation.

Derived from the Pico/SolarOS image sender; see THIRD_PARTY.md. Supports
changed rectangles, RLE, upload windows and presentation completion.
"""
import argparse
from dataclasses import dataclass
import itertools
from pathlib import Path
import secrets
import socket
import struct
import sys
import time
import zlib

CELL_SIZE = 256
OK, BUSY, BAD_SESSION, STALE_BOOT = 0, 4, 5, 6
GET_INFO, CONFIGURE, GET_STATUS = 1, 2, 3
GET_EXTENSIONS, GET_SYNC, RECT_RLE = 4, 5, 0x15
GET_CODECS, RECT_LZ4, LZ4 = 8, 0x16, 128
NEGOTIATE_WINDOW, WINDOW_SYNC, PUSH = 6, 7, 3
WINDOW_VERSION, MAX_WINDOW = 2, 16
PRESERVE_CONTENT, RLE, SYNC = 1, 32, 64
BEGIN_FRAME, BEGIN_RECT, RECT_DATA, PRESENT, ABORT_FRAME = range(0x10, 0x15)
HEADER = struct.Struct("<4sBBBBIIHHQ4x")
MONO1, RGB565 = 0, 4
FORMAT_NAMES = {MONO1: "mono1", RGB565: "rgb565"}


def row_bytes(width, pixel_format):
    return width * 2 if pixel_format == RGB565 else (width + 7) // 8


def tcp_endpoint(text):
    host, separator, port = text.rpartition(":")
    if not separator or not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError("TCP endpoint must be HOST:PORT (port 1..65535)")
    return host, int(port)


class TcpStream:
    """Raw TCP adapter with bounded reads and partial-write backpressure."""
    def __init__(self, endpoint):
        self.socket = socket.create_connection(endpoint, timeout=2.0)
        try:
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.settimeout(.05)
        except BaseException:
            self.socket.close()
            raise

    def read(self, size):
        try:
            data = self.socket.recv(size)
        except TimeoutError:
            return b""
        if not data:
            raise OSError("DLP TCP peer disconnected")
        return data

    def write(self, data):
        try:
            return self.socket.send(data)
        except TimeoutError:
            return 0

    def __enter__(self): return self
    def __exit__(self, *args): self.socket.close()


class ProtocolError(RuntimeError):
    def __init__(self, opcode, result):
        super().__init__(f"DLP command 0x{opcode:02x} returned result {result}")
        self.result = result


@dataclass
class Cell:
    kind: int
    opcode: int
    session: int
    sequence: int
    result: int
    boot: int
    payload: bytes


@dataclass
class FrameStats:
    """Host wall times; present_wait includes final-batch transfer and completion."""
    upload_ms: float = 0.0
    present_wait_ms: float = 0.0
    total_ms: float = 0.0
    presented: bool = False


def encode_cell(opcode, session, sequence, boot, payload=b"", compact=False, kind=1):
    if len(payload) > 220 or not 0 < sequence <= 0xffffffff or kind not in (1, PUSH):
        raise ValueError("Invalid SDLP request")
    packet = bytearray(CELL_SIZE)
    HEADER.pack_into(packet, 0, b"SDLP", 1, 0, kind, opcode, session, sequence,
                     0, len(payload), boot)
    packet[32:32 + len(payload)] = payload
    if compact:
        packet = packet[:32 + len(payload)]
        packet[:4] = b"DLP1"
        return bytes(packet) + struct.pack("<I", zlib.crc32(packet))
    struct.pack_into("<I", packet, 252, zlib.crc32(packet[:252]))
    return bytes(packet)


def decode_cell(packet):
    if len(packet) < 36 or len(packet) > CELL_SIZE:
        raise ValueError("Wrong cell length")
    magic, version, reserved, kind, op, session, seq, result, n, boot = HEADER.unpack_from(packet)
    compact = magic == b"DLP1"
    expected = 36 + n if compact else CELL_SIZE
    if len(packet) != expected:
        raise ValueError("Wrong cell length")
    crc, = struct.unpack_from("<I", packet, len(packet) - 4)
    if (magic not in (b"SDLP", b"DLP1") or version != 1 or reserved or kind > PUSH or n > 220
            or any(packet[28:32]) or (not compact and any(packet[32 + n:252]))
            or crc != zlib.crc32(packet[:-4]) or (kind and not seq)
            or (kind == 0 and (op or seq or result or n))
            or (kind in (1, PUSH) and result)):
        raise ValueError("Invalid SDLP cell")
    return Cell(kind, op, session, seq, result, boot, packet[32:32 + n])


def rle_encode(data, capacity=206):
    output = bytearray()
    at = 0
    while at < len(data) and len(output) + 2 <= capacity:
        run = 1
        while at + run < len(data) and run < 130 and data[at + run] == data[at]:
            run += 1
        if run >= 3:
            output.extend((128 + run - 3, data[at]))
            at += run
        else:
            start = at
            while True:
                at += 1
                if (at == len(data) or at - start == 128
                        or at - start == capacity - len(output) - 1):
                    break
                if at + 2 < len(data) and data[at] == data[at + 1] == data[at + 2]:
                    break
            output.append(at - start - 1)
            output.extend(data[start:at])
    return bytes(output), at


def changed_rects(before, after, width, height, pixel_format=MONO1):
    stride = row_bytes(width, pixel_format)
    rectangles = []
    for y in range(0, height, 8):
        end = min(y + 8, height)
        left, right = stride, 0
        for row in range(y, end):
            old = before[row * stride:(row + 1) * stride]
            new = after[row * stride:(row + 1) * stride]
            if old == new:
                continue
            first = next(i for i, (a, b) in enumerate(zip(old, new)) if a != b)
            last = stride - next(i for i, (a, b) in enumerate(zip(reversed(old), reversed(new))) if a != b)
            left, right = min(left, first), max(right, last)
        if right:
            if pixel_format == RGB565:
                left, right = left // 2, (right + 1) // 2
            else:
                left, right = left * 8, min(width, right * 8)
            rectangles.append((left, y, right - left, end - y))
    return rectangles


class DisplayLink:
    """Negotiated windowed DLP client; port supplies ordered read/write operations.

    Prefer compact framing and fall back to fixed v1 cells on discovery.
    Serial ports, USB CDC, socket/file adapters and in-process pipes all use
    the same client. Serialize calls; retained image state is per session.
    """
    def __init__(self, port, timeout=2.0, compact=True, pixel_format="auto", *, compressed=False):
        if pixel_format not in ("auto", *FORMAT_NAMES.values()):
            raise ValueError("Pixel format must be auto, mono1 or rgb565")
        self.requested_format = pixel_format
        self.compressed = compressed
        self._lz4 = None
        self.pixel_format = MONO1
        self.formats = 0
        self.port = port
        self.timeout = timeout
        self.buffer = bytearray()
        self.sequence = self.boot = self.session = self.frame = 0
        self.width = self.height = 0
        self.present_timeout = 30.0
        self.compact = compact
        self.capabilities = 0
        self.previous = None
        self.tx_bytes = self.rx_bytes = 0
        self.max_rect_bytes = 0
        self.last_frame_stats = FrameStats()
        self.requested_window = MAX_WINDOW
        self.window_size = self.push_sent = self.push_ack = self.push_grant = 0
        self.push_packets = []
        self.window_failed = False

    def _write_all(self, packet, deadline):
        offset = 0
        while offset < len(packet):
            n = self.port.write(packet[offset:])
            if n is None or n < 0 or n > len(packet) - offset:
                raise OSError("Invalid display stream write result")
            if time.monotonic() >= deadline:
                raise TimeoutError("Display stream write stalled")
            if not n:
                time.sleep(0.001)
                continue
            offset += n
            self.tx_bytes += n

    def _receive(self, deadline):
        while time.monotonic() < deadline:
            starts = [i for magic in (b"SDLP", b"DLP1")
                      if (i := self.buffer.find(magic)) >= 0]
            index = min(starts) if starts else -1
            if index < 0:
                self.buffer[:] = self.buffer[-3:]
            elif index:
                del self.buffer[:index]
            length = CELL_SIZE
            if self.buffer.startswith(b"DLP1") and len(self.buffer) >= 32:
                length = 36 + struct.unpack_from("<H", self.buffer, 18)[0]
                if length > CELL_SIZE:
                    del self.buffer[0]
                    continue
            if len(self.buffer) >= length:
                try:
                    cell = decode_cell(self.buffer[:length])
                except ValueError:
                    del self.buffer[0]
                    continue
                del self.buffer[:length]
                return cell
            waiting = getattr(self.port, "in_waiting", CELL_SIZE)
            data = self.port.read(max(1, min(CELL_SIZE, waiting)))
            self.rx_bytes += len(data)
            self.buffer.extend(data)
        raise TimeoutError("Timed out waiting for a display response")

    def command(self, opcode, payload=b"", attempts=3, *, prefix=b""):
        self.sequence += 1
        discovery = opcode == GET_INFO
        session = 0 if discovery else self.session
        packet = encode_cell(opcode, session, self.sequence,
                             0 if discovery else self.boot, payload, self.compact)
        # Retries retain the same sequence and bytes, so device replay prevents
        # duplicate writes or a second PRESENT after a lost response.
        for attempt in range(attempts):
            self.buffer.clear()  # Discard incomplete replies before exact retry.
            wait = self.timeout
            if opcode == WINDOW_SYNC and struct.unpack_from("<I", payload, 4)[0]:
                wait = self.present_timeout
            deadline = time.monotonic() + wait
            # Coalesce a window and its checkpoint into one stream write.
            # Control retries repeat only the exact control packet; missing
            # uploads are resent after a fresh cumulative progress snapshot.
            self._write_all((prefix if attempt == 0 else b"") + packet, deadline)
            try:
                while True:
                    answer = self._receive(deadline)
                    if (answer.kind == 2 and answer.opcode == opcode
                            and answer.sequence == self.sequence
                            and answer.session == session):
                        if answer.result != OK:
                            raise ProtocolError(opcode, answer.result)
                        if not discovery and answer.boot != self.boot:
                            raise RuntimeError("Peripheral restarted; reconnect the display client")
                        return answer
            except TimeoutError:
                if attempt == attempts - 1:
                    raise

    def connect(self):
        self.previous = None
        self.frame = 0
        self.window_size = self.push_sent = self.push_ack = self.push_grant = 0
        self.push_packets.clear()
        self.window_failed = False
        original_timeout = self.timeout
        try:
            if self.compact:
                self.timeout = min(original_timeout, 0.4)
            answer = self.command(GET_INFO, attempts=1 if self.compact else 3)
        except TimeoutError:
            if not self.compact:
                raise
            self.compact = False
            self.buffer.clear()
            self.timeout = original_timeout
            answer = self.command(GET_INFO)
        finally:
            self.timeout = original_timeout
        p = answer.payload
        if len(p) != 128 or struct.unpack_from("<H", p)[0] != 128:
            raise ValueError("Invalid display descriptor")
        self.width, self.height, self.formats = struct.unpack_from("<HHI", p, 4)
        if not self.width or not self.height:
            raise ValueError("Invalid display geometry")
        choices = (RGB565, MONO1) if self.requested_format == "auto" else (
            next(f for f, name in FORMAT_NAMES.items() if name == self.requested_format),)
        self.pixel_format = next((f for f in choices if self.formats & (1 << f)), None)
        if self.pixel_format is None:
            raise ValueError(f"Display does not support requested format {self.requested_format}; requires MONO1 or RGB565")
        self.present_timeout = struct.unpack_from("<I", p, 36)[0] / 1000 + 2
        self.capabilities = struct.unpack_from("<I", p, 16)[0]
        self.max_rect_bytes = struct.unpack_from("<I", p, 28)[0]
        if self.max_rect_bytes < row_bytes(self.width, self.pixel_format):
            raise ValueError("Invalid rectangle limit")
        self.boot = answer.boot
        self.session = secrets.randbelow(0xffffffff) + 1
        until = time.monotonic() + self.present_timeout
        while True:
            try:
                self.command(CONFIGURE, struct.pack("<HH", self.pixel_format, 0))
                break
            except ProtocolError as error:
                if error.result != BUSY or time.monotonic() >= until:
                    raise
                time.sleep(0.01)
        try:
            extensions = self.command(GET_EXTENSIONS).payload
            if len(extensions) != 4 or struct.unpack("<I", extensions)[0] & ~(RLE | SYNC):
                raise ValueError("Invalid DLP extensions")
            self.capabilities |= struct.unpack("<I", extensions)[0]
        except ProtocolError as error:
            if error.result != 1:
                raise
        if self.compressed:
            try:
                codecs = self.command(GET_CODECS).payload
                if len(codecs) != 4 or struct.unpack("<I", codecs)[0] & ~LZ4:
                    raise ValueError("Invalid DLP codecs")
                self.capabilities |= struct.unpack("<I", codecs)[0]
            except ProtocolError as error:
                if error.result != 1:
                    raise
            if self.capabilities & LZ4:
                try:
                    from lz4.block import compress
                except ImportError as error:
                    raise RuntimeError("LZ4 compression requires the Python lz4 package") from error
                self._lz4 = compress
        if self.requested_window:
            try:
                p = self.command(NEGOTIATE_WINDOW, struct.pack(
                    "<HH", WINDOW_VERSION, self.requested_window)).payload
                if len(p) != 4:
                    raise ValueError("Invalid DLP window negotiation")
                version, size = struct.unpack("<HH", p)
                if version != WINDOW_VERSION or not 1 <= size <= self.requested_window:
                    raise ValueError("Invalid DLP window negotiation")
                self.window_size = self.push_grant = size
            except ProtocolError as error:
                if error.result != 1:
                    raise
        return self.width, self.height

    def _flush_window(self, frame=0):
        until = time.monotonic() + (self.present_timeout if frame else self.timeout)
        while time.monotonic() < until:
            # One bounded batch avoids a TCP/BLE write for every tiny operation.
            p = self.command(WINDOW_SYNC, struct.pack("<II", self.push_sent, frame),
                             prefix=b"".join(self.push_packets)).payload
            if len(p) != 32:
                raise ValueError("Invalid DLP window progress")
            (version, size, ack, grant, failed, upload_error, frame_error,
             completed, failed_frame, need_full) = struct.unpack("<HHIIIHHIII", p)
            if (version != WINDOW_VERSION or size != self.window_size
                    or not self.push_ack <= ack <= self.push_sent
                    or grant != min(0xffffffff, ack + size) or need_full > 1
                    or bool(failed) != bool(upload_error) or failed > self.push_sent):
                raise ValueError("Invalid DLP window progress")
            if upload_error:
                raise ProtocolError(WINDOW_SYNC, upload_error)
            if frame and (failed_frame == frame or frame_error):
                raise RuntimeError(f"Frame {frame} failed, device error {frame_error}")
            removed = ack - self.push_ack
            if removed > len(self.push_packets):
                raise ValueError("Invalid DLP window acknowledgement")
            del self.push_packets[:removed]
            self.push_ack, self.push_grant = ack, grant
            if not self.push_packets:
                if frame and (completed != frame or need_full):
                    raise ValueError("DLP acknowledged upload without completing presentation")
                return
            # Retain exact packets and resend the suffix. A fresh control
            # sequence observes new progress; exact retries replay a snapshot.
        raise TimeoutError("DLP window made no progress before its deadline")

    def _upload_command(self, opcode, payload):
        if not self.window_size:
            return self.command(opcode, payload)
        if len(self.push_packets) == self.window_size:
            self._flush_window()
        if self.push_sent == 0xffffffff or self.push_sent >= self.push_grant:
            raise RuntimeError("DLP upload credit exhausted; reconnect")
        self.push_sent += 1
        self.push_packets.append(encode_cell(opcode, self.session, self.push_sent,
                                            self.boot, payload, self.compact, kind=PUSH))
        if opcode == PRESENT:
            self._flush_window(struct.unpack("<I", payload)[0])

    def send_frame(self, pixels, wait_timeout=10.0):
        started = time.monotonic()
        self.last_frame_stats = FrameStats()
        if self.window_failed:
            raise RuntimeError("DLP window failed; reconnect before sending another frame")
        stride = row_bytes(self.width, self.pixel_format)
        if not self.width or len(pixels) != stride * self.height:
            raise ValueError("Frame size does not match the discovered geometry")
        if self.pixel_format == MONO1 and self.width % 8:
            mask = 0xff << (self.width % 8)
            if any(pixels[y * stride + stride - 1] & mask for y in range(self.height)):
                raise ValueError("Nonzero row-padding bits")
        try:
            until = time.monotonic() + wait_timeout
            while True:
                try:
                    return self._upload_frame(pixels, stride, max(0, until - time.monotonic()))
                except ProtocolError as error:
                    contended = error.result == BUSY and time.monotonic() < until
                    lost_content = self.previous is not None and error.result in (2, 3)
                    if not self.window_size or not (contended or lost_content):
                        raise
                    # SPI and USB share the Pico framebuffer. A rejected PUSH
                    # fences its window, including BUSY from another source.
                    # Retry ownership in a new session with a full replacement.
                    if contended:
                        time.sleep(.01)
                    geometry = self.width, self.height, self.pixel_format
                    self.connect()
                    if (self.width, self.height, self.pixel_format) != geometry:
                        raise RuntimeError("Display geometry or pixel format changed; reconnect and prepare a new frame")
        except BaseException:
            self.previous = None
            self.window_failed = bool(self.window_size)
            raise
        finally:
            self.last_frame_stats.total_ms = (time.monotonic() - started) * 1000

    def _upload_frame(self, pixels, stride, wait_timeout):
        started = time.monotonic()
        whole = [(0, 0, self.width, self.height)]
        full = self.previous is None or not self.capabilities & PRESERVE_CONTENT
        rectangles = whole if full else changed_rects(
            self.previous, pixels, self.width, self.height, self.pixel_format)
        if not rectangles:
            if self.capabilities & SYNC:
                sync = self.command(GET_SYNC).payload
                if len(sync) != 4 or struct.unpack("<I", sync)[0] > 1:
                    raise ValueError("Invalid retained-content status")
                if not struct.unpack("<I", sync)[0]:
                    self.last_frame_stats.upload_ms = (time.monotonic() - started) * 1000
                    return self.frame
            full, rectangles = True, whole

        def bands(rectangles):
            for x, y, width, height in rectangles:
                row = row_bytes(width, self.pixel_format)
                count = self.max_rect_bytes // row
                for offset in range(0, height, count):
                    yield x, y + offset, width, min(count, height - offset)

        def cost(rectangles):
            return sum(1 + ((row_bytes(w, self.pixel_format) * h + 207) // 208)
                       for x, y, w, h in bands(rectangles))

        if not full and cost(rectangles) >= cost(whole):
            full, rectangles = True, whole
        self.frame += 1
        f = self.frame
        until = time.monotonic() + wait_timeout
        while True:
            try:
                self._upload_command(BEGIN_FRAME, struct.pack("<II", f, int(full)))
                break
            except ProtocolError as error:
                # A second writer or reset can invalidate retained content.
                if not full and error.result in (2, 3):
                    full, rectangles = True, whole
                    continue
                if error.result != BUSY or time.monotonic() >= until:
                    raise
                time.sleep(0.01)
        try:
            for rect, (x, y, width, height) in enumerate(bands(rectangles), 1):
                self._upload_command(BEGIN_RECT, struct.pack("<IIHHHH", f, rect, x, y, width, height))
                row = row_bytes(width, self.pixel_format)
                start = x * 2 if self.pixel_format == RGB565 else x // 8
                data = b"".join(pixels[(y + k) * stride + start:
                                       (y + k) * stride + start + row]
                                for k in range(height))
                offset = 0
                while offset < len(data):
                    raw = min(208, len(data) - offset)
                    used = raw
                    op, payload = RECT_DATA, data[offset:offset + raw]
                    if self.capabilities & RLE:
                        encoded, consumed = rle_encode(data[offset:offset + 1024])
                        if consumed > raw or (consumed == raw and len(encoded) + 2 < raw):
                            op, used = RECT_RLE, consumed
                            payload = struct.pack("<H", consumed) + encoded
                    if self.compressed and self.capabilities & LZ4:
                        limit = min(1024, len(data) - offset)
                        while True:
                            encoded = self._lz4(data[offset:offset + limit], store_size=False)
                            if len(encoded) <= 206 and (limit > used or (
                                    limit == used and len(encoded) + 2 < len(payload))):
                                op, used = RECT_LZ4, limit
                                payload = struct.pack("<H", limit) + encoded
                            if limit <= used:
                                break
                            limit = max(used, limit // 2)
                    self._upload_command(op, struct.pack("<III", f, rect, offset) + payload)
                    offset += used
            uploaded = time.monotonic()
            self.last_frame_stats.upload_ms = (uploaded - started) * 1000
            self._upload_command(PRESENT, struct.pack("<I", f))
            if self.window_size:
                self.last_frame_stats.present_wait_ms = (time.monotonic() - uploaded) * 1000
                self.last_frame_stats.presented = True
                self.previous = bytes(pixels)
                return f
            until = time.monotonic() + self.present_timeout
            while time.monotonic() < until:
                p = self.command(GET_STATUS).payload
                if len(p) != 24:
                    raise ValueError("Invalid status response")
                error = struct.unpack_from("<H", p, 2)[0]
                completed, failed = struct.unpack_from("<II", p, 8)
                if failed == f or p[0] == 4:
                    raise RuntimeError(f"Frame {f} failed, device error {error}")
                if completed == f:
                    self.last_frame_stats.present_wait_ms = (time.monotonic() - uploaded) * 1000
                    self.last_frame_stats.presented = True
                    self.previous = bytes(pixels)
                    return f
                time.sleep(0.005)
            raise TimeoutError(f"Frame {f} did not finish presenting")
        except BaseException:
            if not self.window_size:
                try:
                    self.command(ABORT_FRAME, struct.pack("<I", f))
                except (OSError, RuntimeError, TimeoutError):
                    pass
            raise


USBDisplay = DisplayLink  # Existing producers remain source compatible.


def pack_image(image, width, height):
    """Fit onto a white canvas and pack horizontal MONO1, black=1, LSB first."""
    from PIL import Image, ImageOps
    fitted = ImageOps.pad(image.convert("L"), (width, height),
                         method=Image.Resampling.LANCZOS, color=255).convert("1")
    stride = (width + 7) // 8
    output = bytearray(stride * height)
    pixels = fitted.load()
    for y in range(height):
        for x in range(width):
            if not pixels[x, y]:
                output[y * stride + x // 8] |= 1 << (x % 8)
    return bytes(output)
