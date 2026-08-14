"""``python -m frutlups_drive`` entry point: delegates to the CLI.

``--version`` behavior is unchanged from M001; all other behavior is owned by
:mod:`frutlups_drive.cli` with the frozen exit codes.
"""

from __future__ import annotations

from frutlups_drive.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
