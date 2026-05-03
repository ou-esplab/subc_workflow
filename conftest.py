"""
Root conftest.py — adds stage subdirectories to sys.path so that test modules
that import from moved Python files (forecast, validate_realtime, etc.) still
work after the repo was reorganized into stage-based subdirectories.
"""
import sys
from pathlib import Path

_root = Path(__file__).parent
for _subdir in ("products", "preprocess"):
    _p = str(_root / _subdir)
    if _p not in sys.path:
        sys.path.insert(0, _p)
