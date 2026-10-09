"""Edge-triggered alerting: notify when something goes down (after N consecutive
failed polls) and again when it recovers. No repeat spam while it stays down."""
import asyncio
import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import Config

log = logging.getLogger("alerts")


class AlertEngine:
    def __init__(self, cfg: Config, db, notify=None):
        self.cfg = cfg
        self.db = db
        self.notify = notify or self._send
        self.fails: dict[str, int] = {}   # key -> consecutive failures
        self.alerted: set[str] = set()    # keys currently in alert state

    async def evaluate(self, cluster: str, result: dict):
        keys = {f"{cluster}|cluster": (result["reachable"], f"{cluster} is unreachable: {result.get('error','')}")}
        if result["reachable"]:
            for n in result["nodes"]:
                keys[f"{cluster}|node|{n['name']}"] = (n["online"], f"{cluster}: node {n['name']} is offline")
        for key, (ok, msg) in keys.items():
            if ok:
                self.fails[key] = 0
                if key in self.alerted:
                    self.alerted.discard(key)
                    await self._emit("recovered", cluster, msg.replace("is unreachable", "is back").replace("is offline", "is back online"))
            else:
                self.fails[key] = self.fails.get(key, 0) + 1
                if self.fails[key] >= self.cfg.fail_threshold and key not in self.alerted:
                    self.alerted.add(key)
                    await self._emit("down", cluster, msg)

    async def _emit(self, kind: str, cluster: str, message: str):
        log.warning("%s: %s", kind, message)
        self.db.add_event(cluster, kind, message)
        try:
            await self.notify(kind, cluster, message)
        except Exception:  # noqa: BLE001 - alert delivery must not kill the poller
            log.exception("alert delivery failed")

    async def _send(self, kind: str, cluster: str, message: str):
        subject = f"[{'DOWN' if kind == 'down' else 'OK'}] {message}"
        s = self.cfg.smtp
        if s.host and s.to:
            await asyncio.to_thread(self._smtp, subject, message)
        if self.cfg.webhook_url:
            async with httpx.AsyncClient(timeout=10) as c:
                # "text" works for Slack/Mattermost/Teams-style incoming webhooks.
                await c.post(self.cfg.webhook_url, json={"text": subject})

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
