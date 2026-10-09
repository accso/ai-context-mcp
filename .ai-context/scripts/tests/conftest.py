"""
conftest.py — makes scripts/validate_config.py importable by bare module
name in tests. scripts/ has no sibling-import needs of its own today, but
this keeps the convention identical across all three Python components.
"""
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
