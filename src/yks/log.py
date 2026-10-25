from __future__ import annotations

import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # discord.py's gateway chatter is noisy at DEBUG.
    logging.getLogger("discord.gateway").setLevel(max(logging.INFO, logging.root.level))
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
