from __future__ import annotations

import site
from pathlib import Path


def add_system_dist_packages() -> None:
    """Expose Raspberry Pi OS apt-installed Python packages inside a venv."""

    dist_packages = Path("/usr/lib/python3/dist-packages")
    if dist_packages.exists():
        site.addsitedir(str(dist_packages))
