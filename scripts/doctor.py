#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


if __name__ == "__main__":
    status_script = Path(__file__).resolve().with_name("status.py")
    argv = sys.argv[1:]
    if "--strict" not in argv:
        argv = [*argv, "--strict"]
    raise SystemExit(subprocess.call([sys.executable, str(status_script), *argv]))
