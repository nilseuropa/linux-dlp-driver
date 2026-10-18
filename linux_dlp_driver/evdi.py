"""Minimal libevdi 1.14/1.15 binding. All EVDI calls stay on the event thread.

The library composites the cursor by default. Callback errors are deferred to
Python callers: exceptions must never escape through a ctypes C callback.
"""
import ctypes as C
import ctypes.util
import select
import sys

from .edid import make_edid


class Rect(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("x1", "y1", "x2", "y2")]


class Mode(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("width", "height", "refresh_rate", "bits_per_pixel")] + [("pixel_format", C.c_uint)]


class Buffer(C.Structure):
    _fields_ = [("id", C.c_int), ("buffer", C.c_void_p), ("width", C.c_int),
                ("height", C.c_int), ("stride", C.c_int),
                ("rects", C.POINTER(Rect)), ("rect_count", C.c_int)]


class Ddcci(C.Structure):
    _fields_ = [("address", C.c_uint16), ("flags", C.c_uint16),
                ("buffer_length", C.c_uint32), ("buffer", C.POINTER(C.c_ubyte))]


class Version(C.Structure):
    _fields_ = [(name, C.c_int) for name in ("major", "minor", "patch")]


IntCallback = C.CFUNCTYPE(None, C.c_int, C.c_void_p)
ModeCallback = C.CFUNCTYPE(None, Mode, C.c_void_p)
DdcciCallback = C.CFUNCTYPE(None, Ddcci, C.c_void_p)


class Events(C.Structure):
    _fields_ = [("dpms", IntCallback), ("mode", ModeCallback),
                ("update", IntCallback), ("crtc", IntCallback),
                ("cursor_set", C.c_void_p), ("cursor_move", C.c_void_p),
                ("ddcci", DdcciCallback), ("user_data", C.c_void_p)]


def load_library():
    if sys.byteorder != "little":
        raise RuntimeError("The XRGB8888 capture backend requires a little-endian host")
    try:
        lib = C.CDLL(ctypes.util.find_library("evdi") or "libevdi.so.1")
    except OSError as error:
        raise RuntimeError("libevdi is missing; install libevdi1 and evdi-dkms (see README)") from error
    signatures = {
        "evdi_get_lib_version": (None, [C.POINTER(Version)]),
        "evdi_open": (C.c_void_p, [C.c_int]),
        "evdi_open_attached_to": (C.c_void_p, [C.c_char_p]),
        "evdi_close": (None, [C.c_void_p]),
        "evdi_connect": (None, [C.c_void_p, C.c_void_p, C.c_uint, C.c_uint32]),
        "evdi_connect2": (None, [C.c_void_p, C.c_void_p, C.c_uint, C.c_uint32, C.c_uint32]),
        "evdi_disconnect": (None, [C.c_void_p]),
        "evdi_register_buffer": (None, [C.c_void_p, Buffer]),
        "evdi_unregister_buffer": (None, [C.c_void_p, C.c_int]),
        "evdi_request_update": (C.c_bool, [C.c_void_p, C.c_int]),
        "evdi_grab_pixels": (None, [C.c_void_p, C.POINTER(Rect), C.POINTER(C.c_int)]),
        "evdi_handle_events": (None, [C.c_void_p, C.POINTER(Events)]),
        "evdi_get_event_ready": (C.c_int, [C.c_void_p]),
        "evdi_ddcci_response": (None, [C.c_void_p, C.c_void_p, C.c_uint32, C.c_bool]),
    }
    for name, (result, args) in signatures.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = result, args
    if hasattr(lib, "evdi_open_attached_to_fixed"):
        lib.evdi_open_attached_to_fixed.restype = C.c_void_p
        lib.evdi_open_attached_to_fixed.argtypes = [C.c_char_p, C.c_size_t]
    version = Version()
    lib.evdi_get_lib_version(C.byref(version))
    if version.major != 1 or version.minor not in (14, 15):
        raise RuntimeError(f"Unsupported libevdi ABI {version.major}.{version.minor}; use 1.14 or 1.15")
    return lib


class Monitor:
    def __init__(self, width, height, *, device=None, refresh=60, color=False, library=None):
        self.lib = library if library is not None else load_library()
        self.width, self.height = width, height
        self.handle = None
        self.buffer = None
        self.buffer_id = 0
        self.next_id = 0
        self.pending = self.ready = False
        self.powered = self.crtc_enabled = True
        self.swap_red_blue = False
        self.error = None
        # Strong references to C callbacks must outlive the event context.
        self.events = Events(IntCallback(self._dpms), ModeCallback(self._mode),
                             IntCallback(self._update), IntCallback(self._crtc),
                             None, None, DdcciCallback(self._ddcci), None)
        edid = make_edid(width, height, refresh, color=color)
        if device is not None:
            self.handle = self.lib.evdi_open(device)
        elif hasattr(self.lib, "evdi_open_attached_to_fixed"):
            self.handle = self.lib.evdi_open_attached_to_fixed(None, 0)
        else:
            # 1.14 lacks _fixed. An empty parent requests a generic device and
            # avoids NULL even if a vendor backports the strlen-based wrapper.
            self.handle = self.lib.evdi_open_attached_to(b"")
        if not self.handle:
            raise RuntimeError("Cannot open EVDI card; load evdi with initial_device_count=1 and check DRM access")
        try:
            self.edid = C.create_string_buffer(edid)
            # evdi_connect() imposes an implicit 60 Hz pixel-rate limit.
            self.lib.evdi_connect2(self.handle, self.edid, len(edid),
                                   width * height, width * height * (refresh + 1))
            self.fd = self.lib.evdi_get_event_ready(self.handle)
        except BaseException:
            self.close()
            raise

    def _dpms(self, state, _):
        self.powered = state == 0

    def _crtc(self, state, _):
        self.crtc_enabled = bool(state)

    def _ddcci(self, data, _):
        # No physical DDC bus exists on DLP; explicitly reject the request.
        self.lib.evdi_ddcci_response(self.handle, None, 0, False)

    def _mode(self, mode, _):
        try:
            self._unregister()
            if not mode.width or not mode.height:
                return
            if (mode.width, mode.height) != (self.width, self.height):
                raise RuntimeError(f"Unexpected monitor mode {mode.width}x{mode.height}; use the advertised preferred mode")
            # XR24/AR24/XB24/AB24, the four formats advertised by EVDI.
            if mode.bits_per_pixel != 32 or mode.pixel_format not in (
                    0x34325258, 0x34325241, 0x34324258, 0x34324241):
                raise RuntimeError("EVDI scanout must use a supported 32-bit RGB format")
            self.swap_red_blue = mode.pixel_format in (0x34324258, 0x34324241)
            self.buffer = C.create_string_buffer(mode.width * mode.height * 4)
            self.next_id += 1
            self.buffer_id = self.next_id
            self.lib.evdi_register_buffer(self.handle, Buffer(
                self.buffer_id, C.cast(self.buffer, C.c_void_p), mode.width,
                mode.height, mode.width * 4, None, 0))
        except Exception as error:
            self.error = error

    def _update(self, buffer_id, _):
        if buffer_id == self.buffer_id:
            self.pending, self.ready = False, True

    def _unregister(self):
        if self.buffer_id:
            self.lib.evdi_unregister_buffer(self.handle, self.buffer_id)
        self.buffer_id = 0
        self.buffer = None
        self.pending = self.ready = False

    def _events(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            self.lib.evdi_handle_events(self.handle, C.byref(self.events))
        if self.error:
            raise self.error

    def _request(self):
        if not self.powered or not self.crtc_enabled or self.buffer is None:
            return
        if not self.pending and not self.ready:
            self.pending = True
            if self.lib.evdi_request_update(self.handle, self.buffer_id):
                self.pending, self.ready = False, True

    def poll(self, timeout=0.05):
        # A dirty buffer returns ready synchronously, without an fd event.
        # GRABPIX releases the kernel's page-flip completion, so delaying it
        # can hold up the compositor frame clock on other monitors too.
        self._events(0)
        self._request()
        if not (self.powered and self.crtc_enabled and self.buffer is not None and self.ready):
            self._events(timeout)
            self._request()
        if not (self.powered and self.crtc_enabled and self.buffer is not None and self.ready):
            return None
        rects = (Rect * 16)()
        count = C.c_int(16)
        self.lib.evdi_grab_pixels(self.handle, rects, C.byref(count))
        self.ready = False
        if count.value <= 0:
            return None  # A mode/power transition can invalidate a pending grab.
        data = self.buffer.raw[:self.width * self.height * 4]
        if self.swap_red_blue:
            output = bytearray(data)
            output[0::4], output[2::4] = data[2::4], data[0::4]
            data = bytes(output)
        return data

    def close(self):
        if self.handle:
            self.lib.evdi_disconnect(self.handle)
            self._unregister()
            self.lib.evdi_close(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
