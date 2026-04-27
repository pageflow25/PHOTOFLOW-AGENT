"""
agent.py — Entrypoint do PhotoFlow Print Agent.

Modos:
    agent.py                       loop padrão de polling
    agent.py --test FOTO.jpg       imprime arquivo local e sai
    agent.py --list-printers       lista impressoras (marca Citizen detectada)
    agent.py --dry-run             polling normal mas sem imprimir
    agent.py --check-printer       só pré-checagem (status, DPI) e sai
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Set

from api_client import ApiClient, ApiError, QueuedPhoto
from config import Config
from logger import setup_logging
from printer import (
    PrinterError,
    autodetect_citizen,
    check_printer,
    list_installed_printers,
    print_image,
    resolve_printer_name,
)


log = logging.getLogger("agent")


# Estado global do shutdown
_stopping = threading.Event()


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        log.info("signal_received", extra={"event": "signal_received", "signum": int(signum)})
        _stopping.set()

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):
            # Em alguns contextos Windows SIGTERM não pode ser registrado
            pass


# --------------------------------------------------------------------------- #
# Heartbeat
# --------------------------------------------------------------------------- #

def _start_heartbeat(api: ApiClient, agent_id: str, interval: float) -> threading.Thread:
    def loop():
        while not _stopping.is_set():
            try:
                api.heartbeat(agent_id)
                log.debug("heartbeat", extra={"event": "heartbeat", "agentId": agent_id})
            except ApiError as e:
                log.warning("heartbeat_failed",
                            extra={"event": "heartbeat_failed", "error": str(e)})
            # Espera sensível ao shutdown
            _stopping.wait(timeout=interval)

    t = threading.Thread(target=loop, name="heartbeat", daemon=True)
    t.start()
    return t


# --------------------------------------------------------------------------- #
# Modos auxiliares
# --------------------------------------------------------------------------- #

def _mode_list_printers() -> int:
    printers = list_installed_printers()
    citizens = set(autodetect_citizen(printers))
    if not printers:
        print("(nenhuma impressora instalada)")
        return 1
    print("Impressoras instaladas:")
    for p in printers:
        marker = "  [CITIZEN auto-detect]" if p in citizens else ""
        print(f"  - {p}{marker}")
    return 0


def _mode_check_printer(cfg: Config) -> int:
    name = resolve_printer_name(cfg.printer_name)
    caps = check_printer(name)
    print(f"Impressora: {caps.name}")
    print(f"  Status:        {caps.status_text} (raw=0x{caps.status_raw:08X})")
    print(f"  DPI:           {caps.dpi_x} x {caps.dpi_y}")
    print(f"  Área (px):     {caps.horz_res} x {caps.vert_res}")
    print(f"  Físico (px):   {caps.phys_width} x {caps.phys_height}")
    if caps.is_offline_or_error:
        print("  >>> ATENÇÃO: status indica problema (offline/erro/papel/ribbon).")
        return 2
    return 0


def _mode_test(cfg: Config, foto_path: Path) -> int:
    if not foto_path.exists():
        print(f"Arquivo não encontrado: {foto_path}", file=sys.stderr)
        return 1
    name = resolve_printer_name(cfg.printer_name)
    caps = check_printer(name)
    log.info("agent_test_start", extra={
        "event": "agent_test_start", "printer": name,
        "dpi_x": caps.dpi_x, "dpi_y": caps.dpi_y, "status": caps.status_text,
    })
    img_bytes = foto_path.read_bytes()
    print_image(img_bytes, name, doc_name=f"PhotoFlow-Test:{foto_path.name}")
    print(f"OK — foto enviada para '{name}'.")
    return 0


# --------------------------------------------------------------------------- #
# Loop principal
# --------------------------------------------------------------------------- #

def _process_photo(
    api: ApiClient,
    foto: QueuedPhoto,
    printer_name: str,
    *,
    dry_run: bool,
    in_flight: Set[str],
) -> None:
    in_flight.add(foto.id)
    try:
        log.info("download_start",
                 extra={"event": "download_start", "fotoId": foto.id})
        img_bytes = api.download_image(foto.id, foto_url=foto.foto_url)
        log.info("download_done",
                 extra={"event": "download_done", "fotoId": foto.id, "bytes": len(img_bytes)})

        if dry_run:
            log.info("print_dry_run",
                     extra={"event": "print_dry_run", "fotoId": foto.id})
        else:
            try:
                print_image(img_bytes, printer_name, doc_name=f"PhotoFlow:{foto.id}")
            except PrinterError as e:
                log.error("print_error",
                          extra={"event": "print_error", "fotoId": foto.id, "error": str(e)})
                _safe_confirm(api, foto.id, success=False, error_message=str(e))
                return

        try:
            api.confirm(foto.id, success=True)
            log.info("confirm_success",
                     extra={"event": "confirm_success", "fotoId": foto.id})
        except ApiError as e:
            log.error("confirm_failure",
                      extra={"event": "confirm_failure", "fotoId": foto.id, "error": str(e)})

    except ApiError as e:
        log.error("download_error",
                  extra={"event": "download_error", "fotoId": foto.id, "error": str(e)})
        _safe_confirm(api, foto.id, success=False, error_message=f"download: {e}")
    except Exception as e:  # pragma: no cover — rede de segurança
        log.exception("photo_unexpected_error",
                      extra={"event": "photo_unexpected_error", "fotoId": foto.id})
        _safe_confirm(api, foto.id, success=False, error_message=f"unexpected: {e}")
    finally:
        in_flight.discard(foto.id)


def _safe_confirm(api: ApiClient, foto_id: str, *, success: bool, error_message: str = "") -> None:
    try:
        api.confirm(foto_id, success=success, error_message=error_message)
    except ApiError as e:
        log.error("confirm_failure",
                  extra={"event": "confirm_failure", "fotoId": foto_id, "error": str(e)})


def _run_loop(cfg: Config, *, dry_run: bool) -> int:
    # Resolve impressora + pré-checa (mesmo em dry-run, por sanidade)
    printer_name = resolve_printer_name(cfg.printer_name)
    caps = check_printer(printer_name)

    log.info("agent_start", extra={
        "event": "agent_start",
        "printer": printer_name,
        "agentId": cfg.agent_id,
        "appUrl": cfg.app_url,
        "dpi_x": caps.dpi_x, "dpi_y": caps.dpi_y,
        "status": caps.status_text,
        "dry_run": dry_run,
        "batchSize": cfg.batch_size,
        "pollIntervalSeconds": cfg.poll_interval,
        "claimTimeoutMinutes": cfg.claim_timeout_minutes,
    })
    if caps.is_offline_or_error:
        log.warning("printer_status_warning",
                    extra={"event": "printer_status_warning", "status": caps.status_text})

    api = ApiClient(cfg.app_url, cfg.api_key, timeout=cfg.http_timeout)
    _start_heartbeat(api, cfg.agent_id, cfg.heartbeat_interval)

    in_flight: Set[str] = set()
    cycle = 0
    last_status = caps.status_text

    while not _stopping.is_set():
        cycle += 1
        try:
            fotos = api.claim_queue(limit=cfg.batch_size, agent_id=cfg.agent_id)
        except ApiError as e:
            log.error("claim_error", extra={"event": "claim_error", "error": str(e)})
            _stopping.wait(timeout=cfg.poll_interval)
            continue

        if not fotos:
            log.debug("claim_empty", extra={"event": "claim_empty"})
        else:
            log.info("claim_success",
                     extra={
                         "event": "claim_success",
                         "count": len(fotos),
                         "ids":   [f.id for f in fotos],
                         # Mapa foto_id → lead_nome para o dashboard GUI
                         "leads": {f.id: f.lead_nome or "" for f in fotos},
                     })
            for foto in fotos:
                if _stopping.is_set():
                    # Devolve as restantes
                    remaining = [f.id for f in fotos if f.id not in in_flight]
                    _release_quietly(api, remaining)
                    break
                _process_photo(api, foto, printer_name,
                               dry_run=dry_run, in_flight=in_flight)

        # Re-checa status periodicamente (~1 minuto)
        if cycle % 12 == 0:
            try:
                caps2 = check_printer(printer_name)
                if caps2.status_text != last_status:
                    log.info("printer_status_change", extra={
                        "event": "printer_status_change",
                        "old": last_status, "new": caps2.status_text,
                    })
                    last_status = caps2.status_text
            except PrinterError as e:
                log.warning("printer_check_failed",
                            extra={"event": "printer_check_failed", "error": str(e)})

        _stopping.wait(timeout=cfg.poll_interval)

    # Shutdown — espera fotos em andamento terminarem
    deadline = time.monotonic() + 60.0
    while in_flight and time.monotonic() < deadline:
        log.info("shutdown_wait",
                 extra={"event": "shutdown_wait", "in_flight": list(in_flight)})
        time.sleep(0.5)

    if in_flight:
        log.warning("shutdown_release",
                    extra={"event": "shutdown_release", "ids": list(in_flight)})
        _release_quietly(api, list(in_flight))

    log.info("agent_stop", extra={"event": "agent_stop"})
    return 0


def _release_quietly(api: ApiClient, ids: List[str]) -> None:
    if not ids:
        return
    try:
        api.release(ids)
        log.info("release_done", extra={"event": "release_done", "ids": ids})
    except ApiError as e:
        log.error("release_failed",
                  extra={"event": "release_failed", "ids": ids, "error": str(e)})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="PhotoFlow Print Agent (Citizen / Windows)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--test", metavar="FOTO", help="Imprime uma foto local e sai.")
    g.add_argument("--list-printers", action="store_true",
                   help="Lista impressoras instaladas e sai.")
    g.add_argument("--check-printer", action="store_true",
                   help="Roda pré-checagem da impressora e sai.")
    g.add_argument("--dry-run", action="store_true",
                   help="Polling normal, mas não imprime de fato.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    # Modo --list-printers não precisa de .env
    if args.list_printers:
        # Logging mínimo (stdout) — sem arquivo
        logging.basicConfig(level=logging.WARNING)
        return _mode_list_printers()

    try:
        cfg = Config.load()
    except Exception as e:
        print(f"Erro de configuração: {e}", file=sys.stderr)
        return 2

    setup_logging(cfg.log_dir, cfg.log_level)
    _install_signal_handlers()

    try:
        if args.test:
            return _mode_test(cfg, Path(args.test))
        if args.check_printer:
            return _mode_check_printer(cfg)
        return _run_loop(cfg, dry_run=args.dry_run)
    except PrinterError as e:
        log.error("printer_fatal", extra={"event": "printer_fatal", "error": str(e)})
        print(f"Erro de impressora: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        _stopping.set()
        log.info("agent_stop", extra={"event": "agent_stop", "reason": "KeyboardInterrupt"})
        return 0


if __name__ == "__main__":
    sys.exit(main())
