"""Outbound alert delivery.

The Alerts tab and the poller's alert cache both answer "what is wrong
right now" to whoever is looking. In an enterprise nobody is looking --
alerts have to arrive somewhere people already watch (Slack, Teams,
PagerDuty, an internal webhook receiver) or they may as well not exist.

This module is deliberately a generic JSON POST rather than one
integration per vendor: every destination above either accepts a plain
JSON body or sits behind something that does, and a single `text` field
alongside the structured payload is what makes the same POST render
correctly in Slack/Teams without a vendor-specific formatter here.

Two behaviours matter more than the transport:

  * Only *newly appearing* alerts are sent. The poller re-evaluates every
    cycle (30s by default), so sending the current alert list each time
    would mean a steady-state problem pages someone every 30 seconds
    until they fix it -- which trains people to mute the channel, i.e.
    strictly worse than no alerting.
  * A problem that persists is re-sent once per re-arm window, not never
    again. An alert that fired while nobody was on shift and then stayed
    silent forever is the other half of the same failure.

Delivery failures are logged and swallowed: a webhook receiver being down
must never stop the metrics poller that calls this.
"""
from __future__ import annotations

import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger("pgmon.notify")

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}

# (db_id, severity, title) -> epoch seconds it was last delivered. In-memory
# only, same tradeoff as the poller's own alert cache: a restart re-notifies
# whatever is still wrong, which is the safe direction to fail.
_last_sent: dict[tuple[str, str, str], float] = {}


def _alert_key(db_id: str, alert: dict) -> tuple[str, str, str]:
    # Keyed on title, not the detail text: the detail embeds live numbers
    # ("1.2GB of WAL", "running for 7 min") that change every poll, so
    # including it would make every cycle look like a brand-new alert and
    # defeat the de-duplication entirely.
    return (db_id, alert.get("severity", ""), alert.get("title", ""))


def _should_send(key: tuple[str, str, str], now: float) -> bool:
    last = _last_sent.get(key)
    if last is None:
        return True
    return (now - last) >= settings.ALERT_WEBHOOK_REARM_SECONDS


def _format_text(db_label: str, alert: dict) -> str:
    icon = {"error": "[ERROR]", "warning": "[WARNING]", "info": "[INFO]"}.get(alert.get("severity", ""), "[ALERT]")
    return f"{icon} pgmon / {db_label}: {alert.get('title', '')}\n{alert.get('detail', '')}"


async def send_alerts(db_id: str, db_label: str, alert_list: list[dict]) -> int:
    """POSTs newly-appearing alerts at or above the configured severity.

    Returns how many were actually delivered, so callers/tests can assert
    on the de-duplication behaviour rather than just that it didn't raise.
    """
    url = settings.ALERT_WEBHOOK_URL
    if not url:
        return 0

    min_rank = _SEVERITY_RANK.get(settings.ALERT_WEBHOOK_MIN_SEVERITY, 2)
    now = time.time()
    to_send = []
    for alert in alert_list:
        if _SEVERITY_RANK.get(alert.get("severity", ""), 0) < min_rank:
            continue
        key = _alert_key(db_id, alert)
        if _should_send(key, now):
            to_send.append((key, alert))

    if not to_send:
        return 0

    sent = 0
    timeout = httpx.Timeout(settings.ALERT_WEBHOOK_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for key, alert in to_send:
            payload = {
                # "text" is what Slack/Teams-style receivers render; the rest
                # is for anything doing its own routing//filtering on severity.
                "text": _format_text(db_label, alert),
                "severity": alert.get("severity"),
                "title": alert.get("title"),
                "detail": alert.get("detail"),
                "category": alert.get("category"),
                "database": db_label,
                "database_id": db_id,
                "source": "pgmon",
            }
            try:
                resp = await client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.warning("alert webhook returned %s for %r", resp.status_code, alert.get("title"))
                    continue
            except Exception as e:
                # Deliberately not logger.exception: an unreachable receiver
                # is retried on every poll cycle for every firing alert, so a
                # full traceback each time buries the actual application log
                # under thousands of identical stacks. The exception type and
                # message identify the problem; the stack never varies.
                logger.warning("alert webhook delivery failed for %r: %s: %s",
                               alert.get("title"), type(e).__name__, e)
                continue
            _last_sent[key] = now
            sent += 1
    return sent


def reset_state() -> None:
    """Clears de-duplication state -- used by tests, and on database removal
    so a re-registered database doesn't inherit a stale "already told you"
    record from its previous incarnation."""
    _last_sent.clear()


def forget_database(db_id: str) -> None:
    for key in [k for k in _last_sent if k[0] == db_id]:
        _last_sent.pop(key, None)
