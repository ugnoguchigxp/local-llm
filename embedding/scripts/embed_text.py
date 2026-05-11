#!/usr/bin/env python3
"""Backward-compatible wrapper for the e5embed CLI."""

import sys
from pathlib import Path

# Add the parent directory to sys.path to discover 'e5embed'
script_dir = Path(__file__).parent.absolute()
sys.path.append(str(script_dir.parent))

from e5embed.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
