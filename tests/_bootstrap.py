"""Make the in-repo package importable when the product tests run uninstalled.

Importing this module prepends ``08_pkg/src`` to ``sys.path``; with an
editable install present it resolves to the same source tree.
"""

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
