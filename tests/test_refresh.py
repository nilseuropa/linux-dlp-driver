import argparse
import os
import subprocess
import unittest
from unittest.mock import patch

from linux_dlp_driver.edid import make_edid
from linux_dlp_driver.refresh import refresh_arg, resolve_refresh, xrandr_refresh


class RefreshTests(unittest.TestCase):
    def test_primary_current_mode_not_available_maximum(self):
        self.assertEqual(xrandr_refresh("""Screen 0: current 2800 x 1600
eDP-1 connected primary 2560x1600+0+0
   2560x1600    240.00+  59.95*+
DVI-I-2-1 connected 240x64+2560+0
   240x64       239.91*+
"""), 60)

    def test_high_refresh_primary(self):
        self.assertEqual(xrandr_refresh("""DVI-I-2-1 connected 240x64+2560+0
   240x64        60.01*+
eDP-1 connected primary 2560x1600+0+0
   2560x1600    239.96*+ 60.00
"""), 240)

    def test_no_primary_uses_fastest_active(self):
        self.assertEqual(xrandr_refresh("""eDP-1 connected 2560x1600+0+0
   2560x1600    144.00*+
DVI-I-2-1 connected 240x64+2560+0
   240x64        60.01*+
"""), 144)
        self.assertIsNone(xrandr_refresh("DVI-I-2-1 disconnected\n"))

    def test_explicit_refresh_does_not_query_desktop(self):
        with patch("linux_dlp_driver.refresh.subprocess.run") as run:
            self.assertEqual(resolve_refresh(144), 144)
            run.assert_not_called()

    def test_auto_query_failure_and_wayland_fallback(self):
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}), \
             patch("linux_dlp_driver.refresh.subprocess.run", side_effect=subprocess.TimeoutExpired("xrandr", 2)):
            self.assertEqual(resolve_refresh(None), 60)
        with patch.dict(os.environ, {"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}), \
             patch("linux_dlp_driver.refresh.subprocess.run") as run:
            self.assertEqual(resolve_refresh(None), 60)
            run.assert_not_called()

    def test_argument_validation(self):
        self.assertIsNone(refresh_arg("auto"))
        self.assertEqual(refresh_arg("240"), 240)
        for text in ("0", "241", "nan", "59.94"):
            with self.assertRaises(argparse.ArgumentTypeError):
                refresh_arg(text)

    def test_native_high_refresh_edid(self):
        edid = make_edid(240, 64, 240)
        self.assertEqual(len(edid), 128)
        self.assertEqual(sum(edid) % 256, 0)
        self.assertEqual(edid[113:115], bytes((239, 241)))
