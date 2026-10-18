"""EVDI XRGB8888 pixels to DLP MONO1 or big-endian RGB565."""
from PIL import Image, ImageChops, ImageOps

from .protocol import MONO1, RGB565

REVERSE_INVERT = bytes(sum(((~value >> bit) & 1) << (7 - bit)
                          for bit in range(8)) for value in range(256))


def pack_frame(data, width, height, target, *, pixel_format=MONO1, dither=False, invert=False):
    if len(data) != width * height * 4:
        raise ValueError("Invalid XRGB8888 framebuffer size")
    image = Image.frombytes("RGB", (width, height), data, "raw", "BGRX")
    return pack_image(image, target, pixel_format=pixel_format, dither=dither, invert=invert)


def pack_image(image, target, *, pixel_format=MONO1, dither=False, invert=False):
    if pixel_format not in (MONO1, RGB565):
        raise ValueError("Unsupported pixel format")
    image = ImageOps.pad(image.convert("RGB" if pixel_format == RGB565 else "L"), target,
                         method=Image.Resampling.LANCZOS,
                         color=(255, 255, 255) if pixel_format == RGB565 else 255)
    if invert:
        image = ImageOps.invert(image)
    if pixel_format == RGB565:
        # Channel LUTs and merging run in Pillow's native code, avoiding a
        # Python loop over every color pixel on the desktop capture thread.
        red, green, blue = image.split()
        high = ImageChops.add(red.point([v & 0xf8 for v in range(256)]),
                              green.point([v >> 5 for v in range(256)]))
        low = ImageChops.add(green.point([(v & 0x1c) << 3 for v in range(256)]),
                             blue.point([v >> 3 for v in range(256)]))
        return Image.merge("LA", (high, low)).tobytes()
    image = image.convert("1", dither=Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE)
    # Pillow packs white=1, MSB first; DLP wants black=1, LSB first.
    output = bytearray(image.tobytes().translate(REVERSE_INVERT))
    w, h = target
    if w % 8:
        stride = (w + 7) // 8
        for y in range(h):
            output[(y + 1) * stride - 1] &= (1 << (w % 8)) - 1
    return bytes(output)
