"""
conftest.py — makes watcher/*.py importable by bare module name in tests
(e.g. `import chunker`, `import symbol_extractor`), matching how the watcher's
own modules import each other (see chunker.py's own comment on why it uses a
plain `import symbol_extractor` rather than a package-relative import: this
is a flat-namespace codebase, not a package).
"""
import sys
from pathlib import Path

_WATCHER_DIR = Path(__file__).resolve().parent.parent
if str(_WATCHER_DIR) not in sys.path:
    sys.path.insert(0, str(_WATCHER_DIR))
