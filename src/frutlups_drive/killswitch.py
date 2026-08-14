"""Kill-switch sentinel: ``.frutlups_drive/STOP`` (architecture contract §8.1).

Existence means: stop gracefully this tick. Creation is idempotent and safe;
liveness is never probed with ``os.kill(pid, 0)`` or any other process signal.
"""

from __future__ import annotations

from pathlib import Path

STOP_SENTINEL_NAME = "STOP"


def stop_requested(store_root: Path) -> bool:
    return (Path(store_root) / STOP_SENTINEL_NAME).exists()


def request_stop(store_root: Path) -> Path:
    root = Path(store_root)
    root.mkdir(parents=True, exist_ok=True)
    sentinel = root / STOP_SENTINEL_NAME
    try:
        with open(sentinel, "xb") as stream:
            stream.write(b"stop requested\n")
    except FileExistsError:
        pass
    return sentinel
