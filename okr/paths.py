"""Resolve deployment resources independently of the working directory.

The package lives one directory below the existing deployment root. Keeping
resources there preserves the host application's configuration and database.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
STATIC_DIR = PROJECT_ROOT / "static" / "okr"
