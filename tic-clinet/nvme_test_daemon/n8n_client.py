"""n8n Webhook 通訊客戶端。"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


class N8nClient:
    """負責向 n8n 回傳 Log、Heartbeat 與 Alert。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=settings.http_timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, url: str, payload: Dict[str, Any]) -> bool:
        if not url:
            logger.error("Webhook URL 未設定，略過 POST")
            return False
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
            logger.debug("Webhook POST 成功: %s status=%s", url, resp.status_code)
            return True
        except httpx.HTTPError as exc:
            logger.error("Webhook POST 失敗 (%s): %s", url, exc)
            return False

    async def send_log(
        self,
        *,
        command_id: str,
        status: str,
        return_code: Optional[int],
        stdout: str,
        stderr: str,
        crashed: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "event": "command_result",
            "command_id": command_id,
            "status": status,
            "return_code": return_code,
            "stdout": stdout,
            "stderr": stderr,
            "crashed": crashed,
        }
        if extra:
            payload.update(extra)
        return await self._post(self._settings.n8n_log_webhook_url, payload)

    async def send_heartbeat(self, health: Dict[str, Any]) -> bool:
        payload = {"event": "heartbeat", **health}
        return await self._post(self._settings.n8n_heartbeat_webhook_url, payload)

    async def send_alert(
        self,
        *,
        alert_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        payload: Dict[str, Any] = {
            "event": "alert",
            "alert_type": alert_type,
            "message": message,
        }
        if details:
            payload["details"] = details
        return await self._post(self._settings.n8n_alert_webhook_url, payload)
