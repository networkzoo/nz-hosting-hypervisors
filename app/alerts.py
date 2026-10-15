"""Edge-triggered alerting: notify when something goes down (after N consecutive failed polls)
and again when it recovers. No repeat spam while it stays down. "Stale" (no update seen for N
days) is the one exception: it repeats on a slow reminder cycle, since it's a nag, not an outage."""
import asyncio
import logging
import smtplib
import time
from email.message import EmailMessage

import httpx

from .config import Config

log = logging.getLogger("alerts")

TITLES = {"down": "DOWN", "stale": "UPDATE OVERDUE", "recovered": "OK"}
TEAMS_COLOUR = {"down": "Attention", "stale": "Warning", "recovered": "Good"}


class AlertEngine:
    def __init__(self, cfg: Config, db, notify=None):
        self.cfg = cfg
        self.db = db
        self.notify = notify or self._send
        self.fails: dict[str, int] = {}     # key -> consecutive failures
        self.alerted: set[str] = set()      # keys currently in alert state
        self.last_alert: dict[str, float] = {}

    async def evaluate(self, cluster: str, result: dict):
        # key -> (ok, kind when not ok, message when not ok, message on recovery)
        keys = {f"{cluster}|cluster": (result["reachable"], "down",
                                       f"{cluster} is unreachable: {result.get('error', '')}", f"{cluster} is back")}
        if result["reachable"]:
            for n in result["nodes"]:
                keys[f"{cluster}|node|{n['name']}"] = (
                    n["online"], "down", f"{cluster}: node {n['name']} is offline",
                    f"{cluster}: node {n['name']} is back online")
            bad = result.get("drives_unhealthy") or 0
            keys[f"{cluster}|drives"] = (bad == 0, "down", f"{cluster}: {bad} drive(s) reported unhealthy",
                                         f"{cluster}: drives healthy again")
            if result.get("stale") is not None:
                days = int(result.get("days_since_update") or 0)
                keys[f"{cluster}|stale"] = (not result["stale"], "stale",
                                            f"{cluster}: no updates detected for {days} days "
                                            f"(limit {result.get('stale_limit')})",
                                            f"{cluster}: updates detected again")
        now = time.time()
        for key, (ok, kind, down_msg, up_msg) in keys.items():
            if ok:
                self.fails[key] = 0
                if key in self.alerted:
                    self.alerted.discard(key)
                    await self._emit("recovered", cluster, up_msg)
            else:
                self.fails[key] = self.fails.get(key, 0) + 1
                if self.fails[key] >= self.cfg.fail_threshold and key not in self.alerted:
                    self.alerted.add(key)
                    self.last_alert[key] = now
                    await self._emit(kind, cluster, down_msg)
                elif (kind == "stale" and key in self.alerted and self.cfg.stale_reminder_days > 0
                      and now - self.last_alert.get(key, now) >= self.cfg.stale_reminder_days * 86400):
                    self.last_alert[key] = now
                    await self._emit(kind, cluster, down_msg + " (reminder)")

    async def _emit(self, kind: str, cluster: str, message: str):
        log.warning("%s: %s", kind, message)
        self.db.add_event(cluster, kind, message)
        try:
            await self.notify(kind, cluster, message)
        except Exception:  # noqa: BLE001 - alert delivery must not kill the poller
            log.exception("alert delivery failed")

    def webhook_payload(self, kind: str, message: str) -> dict:
        title = f"[{TITLES.get(kind, kind.upper())}] {message}"
        if self.cfg.webhook_format == "teams":
            # Teams "Workflows" webhooks expect a message wrapping an Adaptive Card.
            return {"type": "message", "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4",
                    "body": [
                        {"type": "TextBlock", "text": f"Fleet Dashboard - {TITLES.get(kind, kind.upper())}",
                         "weight": "Bolder", "size": "Medium", "color": TEAMS_COLOUR.get(kind, "Default"), "wrap": True},
                        {"type": "TextBlock", "text": message, "wrap": True},
                    ],
                },
            }]}
        return {"text": title}   # Slack / Mattermost style

    async def _send(self, kind: str, cluster: str, message: str):
        subject = f"[{TITLES.get(kind, kind.upper())}] {message}"
        s = self.cfg.smtp
        errors = []
        if s.host and s.to:
            try:
                await asyncio.to_thread(self._smtp, subject, message)
            except Exception as e:  # noqa: BLE001
                errors.append(f"email: {e}")
        if self.cfg.webhook_url:
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.post(self.cfg.webhook_url, json=self.webhook_payload(kind, message))
                    r.raise_for_status()
            except Exception as e:  # noqa: BLE001
                errors.append(f"webhook: {e}")
        if errors:   # one channel failing must not hide the other; report everything that went wrong
            raise RuntimeError("; ".join(errors))

    def _smtp(self, subject: str, body: str):
        s = self.cfg.smtp
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = subject, s.sender or s.username, ", ".join(s.to)
        m.set_content(body)
        with smtplib.SMTP(s.host, s.port, timeout=20) as srv:
            if s.starttls:
                srv.starttls()
            if s.username:
                srv.login(s.username, s.password)
            srv.send_message(m)
