# DLP client provenance

`linux_dlp_driver/protocol.py` is the client and image packer from
`pico_display_link/scripts/display_link.py`, copied on 2026-10-18 from
checkout HEAD `387d8fb364c18e2664671bf1bf2b7a2856a91cc2` (working tree source).
The source file SHA-256 was `d0c67d5c3443dcd775f7c130ea1be57108285b32bdc3bc951a8527bcf8db3c66`.
The CLI and image/demo iterators were omitted. The DLP implementation is MIT
licensed, copyright 2026 SolarOS contributors; see LICENSE.

Local changes add MONO1/RGB565 negotiation, format-aware row sizes and partial
rectangles, and format validation when recovering a session. The original
MONO1 `protocol.pack_image` helper remains for compatibility; the daemon uses
the color-aware packer in `pixels.py`.

EVDI is a separate dependency, not bundled. The ctypes ABI follows
DisplayLink/evdi `library/evdi_lib.h` (libevdi 1.14/1.15).
EVDI's module is GPL-2.0-only. Its library files carry LGPL-2.1-only or
LGPL-2.1-or-later notices; the separate upstream source retains those notices.
https://github.com/DisplayLink/evdi
