"""
api_client.py — Wrapper HTTP da API do PhotoFlow.

- X-API-Key em todas as chamadas.
- Retry exponencial (3 tentativas) em 5xx e timeouts. Não retenta 4xx.
- Métodos: claim_queue, download_image, confirm, release, heartbeat.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


log = logging.getLogger(__name__)


@dataclass
class QueuedPhoto:
    id: str
    foto_url: str
    lead_nome: Optional[str]
    created_at: Optional[str]
    print_attempts: int

    @staticmethod
    def from_json(d: dict) -> "QueuedPhoto":
        return QueuedPhoto(
            id=d["id"],
            foto_url=d.get("fotoUrl") or "",
            lead_nome=d.get("leadNome"),
            created_at=d.get("createdAt"),
            print_attempts=int(d.get("printAttempts", 0) or 0),
        )


class ApiError(Exception):
    pass


class ApiClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = self._build_session(api_key)

    @staticmethod
    def _build_session(api_key: str) -> requests.Session:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=2,                 # 2, 4, 8s
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update({
            "X-API-Key": api_key,
            "User-Agent": "PhotoFlow-PrintAgent/1.0",
            "Accept": "application/json",
        })
        return s

    @staticmethod
    def _http_error_summary(response: requests.Response, max_len: int = 140) -> str:
        """Compacta resposta HTTP para evitar logs gigantes (ex.: HTML de 404)."""
        body = response.text or ""
        # Remove tags HTML e compacta espaços/quebras
        body = re.sub(r"<[^>]+>", " ", body)
        body = " ".join(body.replace("\r", " ").replace("\n", " ").split())
        if not body:
            return f"HTTP {response.status_code}"
        if len(body) > max_len:
            body = body[: max_len - 1] + "…"
        return f"HTTP {response.status_code} — {body}"

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #

    def claim_queue(self, *, limit: int, agent_id: str) -> List[QueuedPhoto]:
        url = f"{self.base_url}/api/print-queue"
        try:
            r = self._session.get(
                url,
                params={"limit": limit, "agentId": agent_id},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ApiError(f"claim_queue: erro de rede: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"claim_queue: {self._http_error_summary(r)}")
        try:
            data = r.json()
        except ValueError as e:
            raise ApiError(f"claim_queue: resposta não-JSON: {e}") from e
        if not isinstance(data, list):
            raise ApiError(f"claim_queue: esperava lista, recebi {type(data).__name__}")
        return [QueuedPhoto.from_json(d) for d in data]

    def download_image(self, foto_id: str, *, foto_url: Optional[str] = None) -> bytes:
        """
        Tenta baixar pela fotoUrl direta primeiro (geralmente Vercel Blob, sem auth).
        Se falhar (4xx/5xx/exception), faz fallback no endpoint autenticado.
        """
        # 1) fotoUrl direta
        if foto_url:
            try:
                # Sessão limpa pra não vazar X-API-Key pro Blob
                r = requests.get(foto_url, timeout=self.timeout)
                if r.status_code == 200 and r.content:
                    return r.content
                log.warning(
                    "download_url_fallback",
                    extra={"event": "download_url_fallback",
                           "fotoId": foto_id, "status": r.status_code},
                )
            except requests.RequestException as e:
                log.warning(
                    "download_url_fallback",
                    extra={"event": "download_url_fallback",
                           "fotoId": foto_id, "error": str(e)},
                )

        # 2) Endpoint autenticado
        url = f"{self.base_url}/api/fotos/{foto_id}/image"
        try:
            r = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"download_image: erro de rede: {e}") from e
        if r.status_code != 200:
            raise ApiError(f"download_image: {self._http_error_summary(r)}")
        if not r.content:
            raise ApiError("download_image: corpo vazio")
        return r.content

    def confirm(self, foto_id: str, *, success: bool, error_message: str = "") -> None:
        url = f"{self.base_url}/api/print-queue/confirm"
        body = {"fotoId": foto_id, "success": bool(success)}
        if not success and error_message:
            body["errorMessage"] = error_message[:1000]
        try:
            r = self._session.post(url, json=body, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"confirm: erro de rede: {e}") from e
        if r.status_code == 404:
            # Compatibilidade com ambientes onde a rota de confirmação ainda
            # não foi publicada. Evita sinalizar falha quando a foto já foi
            # impressa localmente.
            log.warning(
                "confirm_endpoint_missing",
                extra={
                    "event": "confirm_endpoint_missing",
                    "fotoId": foto_id,
                    "success": bool(success),
                },
            )
            return
        if r.status_code >= 400:
            raise ApiError(f"confirm: {self._http_error_summary(r)}")

    def release(self, foto_ids: List[str]) -> None:
        if not foto_ids:
            return
        url = f"{self.base_url}/api/print-queue/release"
        try:
            r = self._session.post(url, json={"fotoIds": foto_ids}, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"release: erro de rede: {e}") from e
        if r.status_code >= 400:
            raise ApiError(f"release: {self._http_error_summary(r)}")

    def heartbeat(self, agent_id: str) -> None:
        url = f"{self.base_url}/api/print-queue/heartbeat"
        try:
            r = self._session.post(url, json={"agentId": agent_id}, timeout=self.timeout)
        except requests.RequestException as e:
            raise ApiError(f"heartbeat: erro de rede: {e}") from e
        if r.status_code == 404:
            # Ambientes antigos podem não ter a rota de heartbeat publicada ainda.
            log.info("heartbeat_endpoint_missing", extra={"event": "heartbeat_endpoint_missing"})
            return
        if r.status_code >= 400:
            raise ApiError(f"heartbeat: {self._http_error_summary(r)}")
