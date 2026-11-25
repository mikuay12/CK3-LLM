"""Local CK3 multiplayer client and model tools."""

import sys

from .paths import LOCAL_DEPENDENCIES


if LOCAL_DEPENDENCIES.is_dir():
    sys.path.insert(0, str(LOCAL_DEPENDENCIES))
