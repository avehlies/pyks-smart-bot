"""`yks-short-worker-health`: the Docker healthcheck. Exits 0 if the worker's heartbeat is recent.

The running worker touches HEARTBEAT_FILE every HEARTBEAT_INTERVAL_SECONDS while the database
answers (see `Worker.beat`).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from yks_short_worker.config import get_settings


def check(path: Path, max_age: float, *, now: float | None = None) -> str | None:
    """None if the heartbeat is fresh, otherwise why not."""
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return f"no heartbeat file at {path}"
    age = (time.time() if now is None else now) - modified
    if age > max_age:
        return f"last heartbeat {age:.0f}s ago (limit {max_age:.0f}s)"
    return None


def main() -> None:
    settings = get_settings()
    problem = check(Path(settings.heartbeat_file), settings.heartbeat_max_age_seconds)
    if problem:
        print(f"unhealthy: {problem}", file=sys.stderr)
        sys.exit(1)
    print("ok")


if __name__ == "__main__":
    main()
