"""One preferred detailed timing, without fallback established/standard modes."""
import struct
import math


def make_edid(width, height, refresh=60, *, color=False):
    if not 64 <= width <= 3840 or not 64 <= height <= 2160:
        raise ValueError("Monitor geometry must be 64..3840 by 64..2160; increase --scale for smaller panels")
    if not 24 <= refresh <= 240:
        raise ValueError("Monitor refresh must be 24..240 Hz")
    hblank, vblank = 160, 45
    # EDID decoders reserve sub-10 MHz descriptor clocks. A virtual sink has
    # no physical blanking constraint: enlarge the vertical porch for tiny
    # native modes while preserving the requested refresh and active pixels.
    vblank = max(vblank, math.ceil(10_000_000 / ((width + hblank) * refresh)) - height)
    clock = round((width + hblank) * (height + vblank) * refresh / 10000)
    if not 1 <= clock <= 65535:
        raise ValueError("Monitor pixel clock exceeds EDID limits")
    edid = bytearray(128)
    edid[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    # DLP manufacturer code; deterministic product/serial identity per geometry.
    struct.pack_into(">H", edid, 8, (4 << 10) | (12 << 5) | 16)
    struct.pack_into("<HI", edid, 10, 1, width << 16 | height)
    edid[16:25] = bytes((1, 36, 1, 3, 0x80, 0, 0, 120, 0x06))
    if color:
        edid[24] |= 0x08  # EDID 1.3 RGB color display rather than grayscale.
    # sRGB chromaticity, standard timings explicitly unused.
    edid[25:35] = bytes.fromhex("ee91a3544c99260f5054")
    edid[38:54] = b"\x01\x01" * 8
    timing = bytearray(18)
    struct.pack_into("<H", timing, 0, clock)
    timing[2:8] = bytes((width & 255, hblank & 255,
                         (width >> 8) << 4 | hblank >> 8,
                         height & 255, vblank & 255,
                         (height >> 8) << 4 | vblank >> 8))
    timing[8:12] = bytes((48, 32, 0x35, 0))
    timing[17] = 0x1a  # separate sync, positive horizontal, negative vertical
    edid[54:72] = timing
    for offset, tag, text in ((72, 0xfc, "DLP Monitor"),
                              (90, 0xff, f"{width}x{height}")):
        edid[offset:offset + 18] = b"\x00\x00\x00" + bytes((tag, 0)) + (text + "\n").encode().ljust(13)
    hfreq = clock * 10 / (width + hblank)
    edid[108:126] = b"\x00\x00\x00\xfd\x00" + bytes((
        refresh - 1, refresh + 1, math.floor(hfreq), math.ceil(hfreq),
        math.ceil(clock / 1000), 0, 10)) + b" " * 6
    edid[127] = -sum(edid) & 255
    return bytes(edid)
