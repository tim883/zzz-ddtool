from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from zzz_ddtool.paths import logs_dir
from zzz_ddtool.retain import retain_logs

_configured = False


def get_logger(name: str = "zzz_ddtool") -> logging.Logger:
    global _configured
    if not _configured:
        _configured = True
        folder = logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / "ddtool.log",
            maxBytes=2 * 1024 * 1024,
            backupCount=6,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root = logging.getLogger("zzz_ddtool")
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        retain_logs(folder)
    return logging.getLogger(name)
