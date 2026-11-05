from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # discord.py's gateway chatter is noisy at DEBUG.
    logging.getLogger("discord.gateway").setLevel(max(logging.INFO, logging.root.level))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    if recent_logs not in logging.root.handlers:
        recent_logs.setFormatter(logging.Formatter("%(message)s"))
        logging.root.addHandler(recent_logs)


@dataclass(frozen=True)
class LogLine:
    created: float
    level: str
    levelno: int
    logger: str
    message: str


class RecentLogs(logging.Handler):
    """Keeps the last few hundred log lines in memory for the admin panel's status page."""

    def __init__(self, capacity: int = 300) -> None:
        super().__init__(level=logging.INFO)
        # Formatted lines rather than records, so log arguments aren't kept alive.
        self.lines: deque[LogLine] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self.lines.append(
            LogLine(record.created, record.levelname, record.levelno, record.name, message)
        )


recent_logs = RecentLogs()
