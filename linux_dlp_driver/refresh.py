"""Choose a virtual refresh independently of the physical DLP upload rate."""
import argparse
import logging
import math
import os
import subprocess

LOG = logging.getLogger("linux-dlp-driver")


def refresh_arg(value):
    if value == "auto":
        return None
    try:
        rate = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("refresh must be auto or an integer from 24 to 240") from None
    if not 24 <= rate <= 240:
        raise argparse.ArgumentTypeError("refresh must be from 24 to 240 Hz")
    return rate


def xrandr_refresh(output):
    primary = False
    active = []
    for line in output.splitlines():
        if line and not line[0].isspace():
            primary = " connected primary " in line
        elif "*" in line:
            for token in line.split():
                if "*" not in token:
                    continue
                try:
                    rate = float(token.rstrip("*+"))
                except ValueError:
                    continue
                if math.isfinite(rate) and rate > 0:
                    active.append((primary, rate))
    if not active:
        return None
    selected = next((rate for primary, rate in active if primary),
                    max(rate for _, rate in active))
    return min(240, max(24, round(selected)))


def resolve_refresh(requested):
    if requested is not None:
        return requested
    if os.environ.get("DISPLAY") and os.environ.get("XDG_SESSION_TYPE") != "wayland":
        try:
            result = subprocess.run(["xrandr", "--current"], capture_output=True,
                                    text=True, timeout=2, check=True)
            rate = xrandr_refresh(result.stdout)
            if rate is not None:
                LOG.info("Virtual monitor refresh: %d Hz (matched to desktop)", rate)
                return rate
        except (OSError, subprocess.SubprocessError):
            pass
    LOG.info("Virtual monitor refresh: 60 Hz (auto detection unavailable; use --refresh to override)")
    return 60
