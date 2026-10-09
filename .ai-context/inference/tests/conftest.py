"""
conftest.py — makes inference/main.py (and inference/maxsim.py) importable
by bare module name in tests, matching the convention used by
watcher/lsp-bridge/scripts (flat namespace, no __init__.py).
"""
import sys
from pathlib import Path

_INFERENCE_DIR = Path(__file__).resolve().parent.parent
if str(_INFERENCE_DIR) not in sys.path:
    sys.path.insert(0, str(_INFERENCE_DIR))
