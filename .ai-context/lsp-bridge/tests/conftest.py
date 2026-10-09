"""
conftest.py — makes lsp-bridge/*.py importable by bare module name in tests,
matching how this component's own modules import each other (flat namespace,
no __init__.py — same convention as watcher/, see that component's
conftest.py for the fuller rationale).
"""
import sys
from pathlib import Path

_LSP_BRIDGE_DIR = Path(__file__).resolve().parent.parent
if str(_LSP_BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(_LSP_BRIDGE_DIR))
