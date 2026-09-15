"""Agentic trading package."""

from __future__ import annotations

import sys
from pathlib import Path

# Editable / console-script installs need repo-root modules (paper_scalper).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_root = str(_REPO_ROOT)
if _root not in sys.path:
    sys.path.insert(0, _root)

__version__ = "0.1.0"
