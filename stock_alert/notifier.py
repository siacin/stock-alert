from __future__ import annotations

import logging
import platform
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import WebhookConfig
from .models import AlertEvent


LOGGER = logging.getLogger(__name__)


class Notifier:
    def __init__(self, beep: bool, webhooks: tuple[WebhookConfig, ...], disabled: bool = False) -> None:
        self.beep = beep
        self.webhooks = webhooks
        self.disabled = disabled
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="notify")

    def send(self, event: AlertEvent) -> None:
        print(event.message, flush=True)
        LOGGER.info("alert type=%s severity=%s %s", event.event_type, event.severity, event.message)
        if self.disabled:
            return
        if self.beep and platform.system() == "Windows":
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception as exc:  # noqa: BLE001
                LOGGER.debug("系统提示音失败: %s", exc)
        if self.webhooks:
            for webhook in self.webhooks:
                self.executor.submit(self._send_webhook, webhook, event)

    def _send_webhook(self, webhook: WebhookConfig, event: AlertEvent) -> None:
        payload = self._payload(webhook.kind, event)
        try:
            response = requests.post(webhook.url, json=payload, timeout=webhook.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.warning("Webhook 通知失败 kind=%s error=%s", webhook.kind, exc)

    @staticmethod
    def _payload(kind: str, event: AlertEvent) -> dict:
        kind = kind.lower()
        if kind in {"wecom", "dingtalk"}:
            return {"msgtype": "text", "text": {"content": event.message}}
        if kind == "feishu":
            return {"msg_type": "text", "content": {"text": event.message}}
        return {
            "text": event.message,
            "event_type": event.event_type,
            "code": event.code,
            "name": event.name,
            "price": event.price,
            "line_price": event.line_price,
            "sources": list(event.sources),
            "severity": event.severity,
            "occurred_at": event.occurred_at.isoformat(),
            "metadata": event.metadata,
        }

    def close(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
