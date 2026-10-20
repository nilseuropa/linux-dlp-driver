# Linux DLP driver

Expose a Display Link Protocol (DLP) display as a Linux DRM/KMS monitor over
USB CDC serial or raw TCP. The userspace daemon receives desktop pixels from
the open-source EVDI kernel driver, converts XRGB8888 to RGB565 color or monochrome, and sends
DLP frames. No proprietary DisplayLink userspace driver is needed.

The DLP endpoint supplies its dimensions and pixel formats during discovery.
The default `--format auto` selects RGB565 color when advertised, otherwise
MONO1. RGB565-only and MONO1-only endpoints are supported. Use `--format mono1`
to reduce bandwidth on a receiver that accepts both, or `--format rgb565` to
require color. GRAY2/GRAY4/GRAY8-only endpoints are currently unsupported.
USB CDC is a serial
transport; USB video/UVC is not required. TCP expects the same raw DLP byte
stream, with a receiver already listening (no telnet, HTTP or TLS wrapper).

## Install

Python 3.10+, Pillow, pyserial, lz4, libevdi 1.14/1.15, and a matching EVDI kernel
module are required. On Debian/Ubuntu:

```sh
sudo apt install python3-venv evdi-dkms libevdi1 linux-headers-$(uname -r)
python3 -m venv .venv
.venv/bin/pip install -e .
sudo modprobe evdi initial_device_count=1
```

The distribution's EVDI package must support the running kernel. Ubuntu's
1.14.2 package fails to build against Linux 7.0. For that kernel, use the
included helper to build pinned upstream EVDI 1.15.1 instead:

```sh
sudo apt install build-essential git pkg-config libdrm-dev dkms linux-headers-$(uname -r)
scripts/build-evdi.sh
sudo make -C .deps/evdi/module install_dkms
sudo make -C .deps/evdi/library install PREFIX=/usr/local
sudo ldconfig
sudo modprobe evdi initial_device_count=1
```

The helper only downloads/builds into `.deps/`; it never installs or uses
sudo. Its module and library build were checked against Linux 7.0.0-38-generic.
Secure Boot may require signing/enrolling the DKMS module.

Run the daemon as the desktop user, with access to the EVDI `/dev/dri/cardN`
node and the serial port. On distributions using `video`/`dialout` groups:

```sh
sudo usermod -aG video,dialout "$USER"
```

Log out and in after changing groups. Device ACLs can also grant access. Only
one daemon may own an endpoint/card at a time. `--evdi-device N` selects an
existing EVDI card explicitly; otherwise libevdi finds an available one.

## Connect

```sh
# Check the receiver first; these modes do not require EVDI.
.venv/bin/linux-dlp-driver --port /dev/ttyACM1 --probe
.venv/bin/linux-dlp-driver --port /dev/ttyACM1 --test-pattern --frames 3

# Advertise the panel's native resolution as a Linux monitor.
.venv/bin/linux-dlp-driver --port /dev/ttyACM1

# Enable negotiated lossless LZ4 compression.
.venv/bin/linux-dlp-driver --port /dev/ttyACM1 --compressed

# A color receiver; auto negotiation selects RGB565 when supported.
.venv/bin/linux-dlp-driver --port /dev/ttyACM0

# A 240x64 panel becomes a 960x256 desktop, downsampled to the panel.
.venv/bin/linux-dlp-driver --port /dev/ttyACM1 --scale 4

# A DLP TCP receiver already listening on port 7777.
.venv/bin/linux-dlp-driver --tcp 192.168.1.50:7777
```

Enable **DLP Monitor** in your desktop display settings, then choose placement
or mirroring. The EDID advertises one preferred mode. Its desktop refresh
defaults to `--refresh auto`: match the primary Xorg display's current refresh
(24–240 Hz), or use 60 Hz when detection is unavailable. Set `--refresh 240`,
for example, to select it explicitly. The desktop refresh controls compositor
timing; it does not set the physical panel's update rate.

`--fps` limits actual DLP uploads independently (default 10); panel presentation
and link speed can lower the achieved rate. `--scale` is an integer and defaults to 1.
Tiny native modes can be rejected by a desktop; increasing scale gives the
desktop more space but shrinks text when downsampled. `--dither` improves
monochrome gradients; the default threshold conversion keeps text and flat
areas stable. Dithering does not apply to RGB565. `--invert` inverts desktop
colors or luminance. Color uses two bytes per pixel; it can update more slowly
than monochrome on the same link.

`--compressed` enables independent lossless LZ4 blocks when the receiver
supports them. Each block uses raw, RLE, or LZ4 according to what fits best;
incompressible data stays raw. Without the option, the existing raw/RLE path
is retained. Older receivers fall back automatically. The option works over
USB and TCP and is reapplied on reconnect. It changes neither color depth nor
pixel values. The receiver needs no compression setting.

EVDI integration depends on your desktop/compositor. On Xorg, if the output
does not appear automatically, inspect `xrandr --listproviders`. Connect the
EVDI provider's output source to your GPU's source provider, using the IDs
from that command:

```sh
xrandr --setprovideroutputsource EVDI_PROVIDER_ID GPU_SOURCE_PROVIDER_ID
xrandr --query
xrandr --output DVI-I-1-1 --auto --right-of eDP-1
```

Replace both output names with those reported on your system. Wayland needs
a compositor that supports EVDI/multiple DRM devices; loading EVDI alone does
not guarantee desktop integration. The daemon never changes your existing
monitor layout automatically.

## Lifecycle and protocol

Ctrl-C/SIGTERM disconnects the virtual monitor and closes the transport. Link
failure disconnects the monitor, retries every two seconds, discovers a new
session and repaints fully. Use `--once` to exit on the first failure. A panel
presentation already in progress can delay shutdown until its bounded DLP
deadline. The last completed image remains on the panel after disconnect.

The event loop requests and consumes ready EVDI buffers before waiting for
notifications. EVDI releases desktop page-flip completion when those pixels
are consumed, so this path stays independent of slow panel uploads. A single-slot
mailbox retains the newest frame instead of building a latency queue. EVDI
composites the mouse cursor. DLP supports compact framing with fixed-cell
fallback, exact command retries, negotiated upload windows, RLE, changed
rectangles and presentation completion. Retained pixels are committed only
after presentation succeeds. Unchanged frames check retained-content status
so another writer or panel reset cannot silently strand the cached image.
`--legacy-cells` and `--no-window` support troubleshooting older endpoints.

## Development

```sh
python3 -m unittest discover -s tests -v
# Also compare all ctypes structure sizes/offsets with the real EVDI C header:
EVDI_INCLUDE=.deps/evdi/library python3 -m unittest discover -s tests -v
python3 -m linux_dlp_driver --help
```

Tests cover EDID timing/checksum, desktop refresh selection, immediate capture
of ready EVDI frames, format negotiation, RGB565 channel order and quantization,
pixel packing/padding, color partial rectangles and RLE, real local TCP and
PTY serial transports, protocol retries and EVDI callback/buffer ownership
using an injected library. Tests do not require a loaded kernel module.
See [THIRD_PARTY.md](THIRD_PARTY.md) for the vendored DLP client provenance.
