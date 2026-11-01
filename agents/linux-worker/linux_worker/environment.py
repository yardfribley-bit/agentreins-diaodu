"""Host facts the relay join call is allowed to carry.

The Hub schema accepts only OS name, version, architecture, shell names and
the Python version. Do not add hostname, username, or environment variables.
"""

from __future__ import annotations

import platform
import shutil


def host_environment() -> dict:
    shells = [name for name in ("bash", "sh", "zsh") if shutil.which(name)]
    system = platform.system() or "Unknown"
    version = platform.release() or "Unknown"
    if system == "Darwin":
        system, version = "macOS", platform.mac_ver()[0] or version
    elif system == "Windows":
        version = " / ".join(filter(None, platform.win32_ver()[:2])) or version
    elif system == "Linux":
        try:
            distro = platform.freedesktop_os_release().get("PRETTY_NAME", "")
            version = (distro + "; kernel " + version) if distro else version
        except (OSError, AttributeError):
            pass
    return {
        "os_name": system,
        "os_version": version[:160],
        "architecture": platform.machine() or "Unknown",
        "shells": shells,
        "python_version": platform.python_version(),
    }


def os_summary(info: dict | None = None) -> str:
    info = info or host_environment()
    return f"{info['os_name']} {info['os_version']} {info['architecture']}"
