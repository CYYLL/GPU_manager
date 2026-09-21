"""Resolve the host IPv4 address used for container access."""

import ipaddress
import os
import re
import subprocess
from typing import Optional


def get_host_ip() -> Optional[str]:
    """Use the default route's source address, with an explicit override."""
    configured = os.getenv("HOST_IP", "").strip()
    if configured:
        return str(ipaddress.IPv4Address(configured))

    try:
        output = subprocess.check_output(
            ["ip", "-4", "route", "get", "1.1.1.1"],
            text=True, timeout=3, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    match = re.search(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})\b", output)
    if not match:
        return None
    try:
        return str(ipaddress.IPv4Address(match.group(1)))
    except ipaddress.AddressValueError:
        return None
