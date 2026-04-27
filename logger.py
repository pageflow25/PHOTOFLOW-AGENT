"""Setup de logging — JSON por linha + RotatingFileHandler + stdout."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Campos extras "padrão" do LogRecord — não devem ir parar no JSON do payload.
_RESERVED = set(vars(logging.LogRecord("x", 0, "", 0, "", None, None)).keys()) | {
    "message", "asctime"
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", record.name),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Anexa quaisquer extras (fotoId, printer, etc.)
        for k, v in record.__dict__.items():
            if k in _RESERVED or k == "event":
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Limpa handlers (evita duplicação se chamado mais de uma vez)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = JsonFormatter()

    file_handler = RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
