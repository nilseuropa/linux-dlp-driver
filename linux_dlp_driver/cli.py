"""Connect a DLP display and forward a Linux DRM monitor's framebuffer."""
import argparse
import json
import logging
import math
import signal
import threading
import time

import serial
from PIL import Image, ImageDraw

from . import evdi
from .edid import make_edid
from .pixels import pack_frame, pack_image
from .protocol import DisplayLink, FORMAT_NAMES, GET_STATUS, MONO1, RGB565, TcpStream, tcp_endpoint
from .refresh import refresh_arg, resolve_refresh

LOG = logging.getLogger("linux-dlp-driver")


class FrameSender:
    """One in-flight upload and one replaceable latest frame, never a backlog.

    DLP can block during panel presentation. A separate worker keeps EVDI's
    event thread responsive. The worker alone owns the configured DLP client.
    Unchanged frames still check GET_SYNC so resets/other writers are detected.
    """
    def __init__(self, link, fps, *, dither=False, invert=False):
        self.link, self.fps = link, fps
        self.dither, self.invert = dither, invert
        self.condition = threading.Condition()
        self.latest = None
        self.stopped = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name="dlp-upload", daemon=True)

    def submit(self, data, width, height):
        with self.condition:
            self.latest = (data, width, height)
            self.condition.notify()

    def start(self):
        self.thread.start()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify()
        # DLP operations have transport and presentation deadlines.
        self.thread.join()

    def _run(self):
        deadline = 0
        reported = False
        try:
            while True:
                with self.condition:
                    while not self.stopped:
                        delay = deadline - time.monotonic()
                        if self.latest is not None and delay <= 0:
                            frame = self.latest
                            break
                        if self.latest is None:
                            self.condition.wait(1)
                            if self.latest is None and not self.stopped:
                                frame = None
                                break
                        else:
                            self.condition.wait(max(0, delay))
                    else:
                        return
                if frame is None:
                    # Detect unplug/reset even before a compositor enables the
                    # new output and submits its first desktop framebuffer.
                    self.link.command(GET_STATUS)
                    continue
                data, width, height = frame
                pixels = pack_frame(data, width, height, (self.link.width, self.link.height),
                                    pixel_format=self.link.pixel_format,
                                    dither=self.dither, invert=self.invert)
                self.link.send_frame(pixels)
                if not reported:
                    LOG.info("First desktop frame presented on DLP display")
                    reported = True
                deadline = time.monotonic() + 1 / self.fps
        except Exception as error:
            self.error = error


def open_transport(args):
    if args.tcp:
        return TcpStream(args.tcp)
    # exclusive prevents cooperating Linux clients from opening the same tty.
    return serial.Serial(args.port, args.baud, timeout=.05, write_timeout=2, exclusive=True)


def connect(port, args):
    link = DisplayLink(port, compact=not args.legacy_cells, pixel_format=args.pixel_format)
    if args.no_window:
        link.requested_window = 0
    link.connect()
    LOG.info("DLP %dx%d %s, framing=%s, window=%d", link.width, link.height,
             FORMAT_NAMES[link.pixel_format].upper(),
             "compact" if link.compact else "fixed", link.window_size)
    return link


def run_monitor(link, args, stop, library):
    width, height = link.width * args.scale, link.height * args.scale
    sender = FrameSender(link, args.fps, dither=args.dither, invert=args.invert)
    with evdi.Monitor(width, height, device=args.evdi_device, refresh=args.refresh,
                      color=link.pixel_format == RGB565, library=library) as monitor:
        LOG.info("DRM monitor connected: %dx%d. Enable DLP Monitor in display settings.", width, height)
        sender.start()
        try:
            while not stop.is_set():
                if sender.error:
                    raise sender.error
                frame = monitor.poll()
                if frame is not None:
                    sender.submit(frame, width, height)
        finally:
            # Disconnect first, before waiting for a slow panel's final upload.
            monitor.close()
            sender.stop()


def test_pattern(link, args, stop):
    for number in range(args.frames):
        if stop.is_set():
            return
        start = time.monotonic()
        color = link.pixel_format == RGB565
        image = Image.new("RGB" if color else "L", (link.width, link.height),
                          (255, 255, 255) if color else 255)
        draw = ImageDraw.Draw(image)
        if color:
            colors = ("red", "lime", "blue", "cyan", "magenta", "yellow")
            for index, value in enumerate(colors):
                left, right = index * link.width // 6, (index + 1) * link.width // 6 - 1
                draw.rectangle((left, link.height // 2, right, link.height - 1), fill=value)
        draw.rectangle((0, 0, link.width - 1, link.height - 1), outline=0)
        draw.text((5, 4), "LINUX DLP MONITOR", fill=0)
        draw.text((5, 19), f"{link.width}x{link.height}  frame {number + 1}", fill=0)
        x = 3 + number * 7 % max(1, link.width - 20)
        draw.rectangle((x, max(32, link.height - 20), x + 10, link.height - 4), fill=0)
        frame = link.send_frame(pack_image(image, (link.width, link.height),
                                           pixel_format=link.pixel_format))
        LOG.info("Pattern frame %d completed in %.1f ms", frame, link.last_frame_stats.total_ms)
        stop.wait(max(0, 1 / args.fps - (time.monotonic() - start)))


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    transport = p.add_mutually_exclusive_group()
    transport.add_argument("--port", default="/dev/ttyACM1", help="USB CDC/serial device (default: /dev/ttyACM1)")
    transport.add_argument("--tcp", type=tcp_endpoint, metavar="HOST:PORT", help="Raw DLP TCP endpoint")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="Discover/configure DLP, print JSON, then exit; no EVDI needed")
    mode.add_argument("--test-pattern", action="store_true", help="Send a bounded animated pattern; no EVDI needed")
    p.add_argument("--frames", type=int, default=3, help="Number of test-pattern frames (default: 3)")
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument("--format", dest="pixel_format", choices=("auto", "mono1", "rgb565"),
                   default="auto", help="DLP pixel format (default: auto; prefer RGB565 color)")
    p.add_argument("--scale", type=int, default=1, help="Integer desktop-to-panel scale (default: 1, native resolution)")
    p.add_argument("--fps", type=float, default=10, help="Maximum DLP frame rate (default: 10)")
    p.add_argument("--refresh", type=refresh_arg, default=None, metavar="auto|HZ",
                   help="Virtual desktop refresh, independent of DLP fps (default: auto; match Xorg primary, otherwise 60)")
    p.add_argument("--dither", action="store_true", help="Use Floyd-Steinberg monochrome dithering")
    p.add_argument("--invert", action="store_true", help="Invert captured desktop colors/luminance")
    p.add_argument("--evdi-device", type=int, help="Existing EVDI card number, e.g. 3 for /dev/dri/card3")
    p.add_argument("--legacy-cells", action="store_true", help="Force fixed 256-byte SDLP framing")
    p.add_argument("--no-window", action="store_true", help="Disable window negotiation")
    p.add_argument("--once", action="store_true", help="Exit on disconnect/error instead of reconnecting")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if (not math.isfinite(args.fps) or args.fps <= 0 or args.scale < 1 or args.baud <= 0
            or args.frames < 1 or (args.evdi_device is not None and args.evdi_device < 0)):
        p.error("fps, scale, baud and frames must be positive; EVDI card must be nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    stop = threading.Event()
    old_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        old_handlers[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        library = None if args.probe or args.test_pattern else evdi.load_library()
        if library is not None:
            args.refresh = resolve_refresh(args.refresh)
        while not stop.is_set():
            try:
                with open_transport(args) as port:
                    link = connect(port, args)
                    if args.probe:
                        print(json.dumps(dict(width=link.width, height=link.height,
                                              pixel_format=FORMAT_NAMES[link.pixel_format],
                                              supported_formats=link.formats,
                                              compact=link.compact, window=link.window_size,
                                              capabilities=link.capabilities)))
                        return 0
                    if args.test_pattern:
                        test_pattern(link, args, stop)
                        return 0
                    # Configuration errors must fail, rather than retry forever.
                    make_edid(link.width * args.scale, link.height * args.scale, args.refresh,
                               color=link.pixel_format == RGB565)
                    run_monitor(link, args, stop, library)
            except (OSError, RuntimeError) as error:
                if args.once or args.probe or args.test_pattern:
                    raise
                LOG.error("%s; monitor disconnected, retrying in 2 s", error)
                stop.wait(2)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
