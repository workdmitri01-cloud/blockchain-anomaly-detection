"""Tiny Telegram Bot API sender (sendMessage over HTTPS, no extra deps)."""
from __future__ import annotations

import html
import logging
import time

import requests

from .config import TelegramConfig

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 4096


class TelegramError(Exception):
    pass


class TelegramSender:
    def __init__(self, cfg: TelegramConfig, session: requests.Session | None = None, dry_run: bool = False):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.dry_run = dry_run
        self._last_sent = 0.0
        # Telegram allows ~1 msg/s per chat and 20 msg/min per group.
        self.min_interval = 1.1 if str(cfg.chat_id).startswith("-") else 0.4

    def send(self, text: str) -> None:
        if self.dry_run:
            print("----- [dry-run telegram] -----\n" + text)
            return
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": text[:MAX_LEN],
            "parse_mode": "HTML",
            "disable_web_page_preview": self.cfg.disable_preview,
        }
        if self.cfg.thread_id:
            payload["message_thread_id"] = self.cfg.thread_id
        url = API_URL.format(token=self.cfg.bot_token)
        for attempt in range(5):
            wait = self.min_interval - (time.time() - self._last_sent)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = self.session.post(url, json=payload, timeout=20)
                self._last_sent = time.time()
                if resp.status_code == 429:
                    retry = resp.json().get("parameters", {}).get("retry_after", 5)
                    log.warning("Telegram rate limit, sleeping %ss", retry)
                    time.sleep(float(retry) + 0.5)
                    continue
                if resp.status_code >= 500:
                    raise TelegramError(f"HTTP {resp.status_code}")
                body = resp.json()
                if not body.get("ok"):
                    # 4xx other than 429 (bad chat id, bad token, bad HTML) won't fix itself.
                    raise TelegramError(f"Telegram rejected message: {body.get('description')}")
                return
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
            except TelegramError as exc:
                if "HTTP 5" not in str(exc):
                    raise
                log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
        raise TelegramError("Telegram send failed after retries")


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_amount(value: float) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if value >= 10_000:
        return f"{value:,.0f}"
    if value >= 1:
        return f"{value:,.2f}"
    return f"{value:.6g}"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    return f"${value:,.0f}"


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}"
