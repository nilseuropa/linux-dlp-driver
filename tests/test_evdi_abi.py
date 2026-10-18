"""Optional verification against the installed/upstream C header.

EVDI_INCLUDE=/path/to/evdi/library python3 -m unittest discover -s tests -v
"""
import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from linux_dlp_driver.evdi import Buffer, Ddcci, Events, Mode, Rect, Version


class AbiTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("EVDI_INCLUDE"), "set EVDI_INCLUDE to verify against evdi_lib.h")
    def test_header_layout(self):
        structs = {
            "evdi_rect": (Rect, {}), "evdi_mode": (Mode, {}),
            "evdi_buffer": (Buffer, {}), "evdi_ddcci_data": (Ddcci, {}),
            "evdi_lib_version": (Version, {"major": "version_major", "minor": "version_minor", "patch": "version_patchlevel"}),
            "evdi_event_context": (Events, {"dpms": "dpms_handler", "mode": "mode_changed_handler",
                "update": "update_ready_handler", "crtc": "crtc_state_handler", "cursor_set": "cursor_set_handler",
                "cursor_move": "cursor_move_handler", "ddcci": "ddcci_data_handler"}),
        }
        code = '#include <evdi_lib.h>\n#include <stddef.h>\n#include <stdio.h>\nint main(void) {\n'
        expected = []
        for name, (cls, names) in structs.items():
            code += f'printf("%zu\\n", sizeof(struct {name}));\n'
            expected.append(ctypes.sizeof(cls))
            for field, _ in cls._fields_:
                code += f'printf("%zu\\n", offsetof(struct {name}, {names.get(field, field)}));\n'
                expected.append(getattr(cls, field).offset)
        code += 'return 0; }\n'
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / "abi.c", Path(directory) / "abi"
            source.write_text(code)
            subprocess.run(["cc", "-Wall", "-Wextra", "-Werror", "-I" + os.environ["EVDI_INCLUDE"],
                            str(source), "-o", str(binary)], check=True)
            actual = list(map(int, subprocess.check_output([str(binary)], text=True).splitlines()))
        self.assertEqual(actual, expected)
