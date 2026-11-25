"""Locations of data shared by the CLI, web console, and local game bridge."""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURES_DIR = PROJECT_ROOT / "captures"
LOCAL_DEPENDENCIES = PROJECT_ROOT / ".deps"
