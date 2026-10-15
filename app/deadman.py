"""Dead-man's-switch ping: tell an external watcher (e.g. a self-hosted Healthchecks) that the
poller is alive. If the pings stop, the watcher alerts - covering the case where this whole
dashboard (or its VM) is down and therefore can't alert about itself."""
import logging

import httpx

log = logging.getLogger("deadman")


async def ping(url: str) -> bool:
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as c:   # internal host, often self-signed
            r = await c.get(url)
            r.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001 - a failed ping must never stop the poller
        log.warning("dead-man ping failed: %s", e)
        return False
