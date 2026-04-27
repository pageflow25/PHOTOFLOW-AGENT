"""Configuração do agente carregada de variáveis de ambiente / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    app_url: str
    api_key: str
    agent_id: str
    printer_name: str  # vazio = autodetect
    poll_interval: float
    batch_size: int
    log_level: str
    log_dir: Path
    heartbeat_interval: float
    http_timeout: float
    # Deve coincidir com CLAIM_TIMEOUT_MINUTES no servidor.
    # Após este tempo sem confirmação, o servidor recoloca a foto em fila.
    claim_timeout_minutes: int

    @staticmethod
    def load() -> "Config":
        load_dotenv()

        app_url = os.getenv("APP_URL", "").strip().rstrip("/")
        api_key = os.getenv("API_KEY", "").strip()
        if not app_url:
            raise RuntimeError("APP_URL não configurada (.env).")
        if not api_key:
            raise RuntimeError("API_KEY não configurada (.env).")

        return Config(
            app_url=app_url,
            api_key=api_key,
            agent_id=os.getenv("AGENT_ID", "stand-1").strip() or "stand-1",
            printer_name=os.getenv("PRINTER_NAME", "").strip(),
            poll_interval=float(os.getenv("POLL_INTERVAL_SECONDS", "5")),
            batch_size=int(os.getenv("BATCH_SIZE", "3")),
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            log_dir=Path(os.getenv("LOG_DIR", "./logs")).expanduser(),
            heartbeat_interval=float(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "30")),
            http_timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
            claim_timeout_minutes=int(os.getenv("CLAIM_TIMEOUT_MINUTES", "5")),
        )
