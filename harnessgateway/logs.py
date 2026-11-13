"""Logging: one JSON object per line (spec §22.3)."""

import json
import logging
import sys
import time


class _JsonLines(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line)


def setup(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonLines())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
