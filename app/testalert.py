"""Send a test alert through every configured channel:  docker compose exec dashboard python -m app.testalert"""
import argparse
import asyncio
import os
import sys

from .alerts import AlertEngine
from .config import load_config
from .store import EventDB


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=int, default=0, help="wait this many seconds first (lock your phone and watch)")
    ap.add_argument("--kind", choices=["down", "stale", "both"], default="both", help="which kind of test to send")
    args = ap.parse_args()
    cfg = load_config(os.environ.get("CONFIG", "config.yaml"))
    channels = []
    if cfg.smtp.host and cfg.smtp.to:
        channels.append(f"email -> {', '.join(cfg.smtp.to)}")
    if cfg.webhook_url:
        channels.append(f"webhook ({cfg.webhook_format})")
    if not channels:
        print("No alert channel configured (set alerts.webhook_url_env and/or alerts.smtp in config.yaml).")
        return 1
    if cfg.webhook_format == "teams" and cfg.teams_mention:
        channels.append(f"@mention {', '.join(cfg.teams_mention)}")
    print("Sending test alert to:", "; ".join(channels))
    if args.delay:
        print(f"Sending in {args.delay}s - lock your phone now.")
        await asyncio.sleep(args.delay)
    engine = AlertEngine(cfg, EventDB(":memory:"))
    try:
        if args.kind in ("down", "both"):
            await engine._send("down", "TEST", "TEST alert from the Fleet Dashboard - if you can read this, alerts work")
        if args.kind == "stale":
            await engine._send("stale", "TEST", "TEST overdue-update alert from the Fleet Dashboard")
        if args.kind == "both":
            await engine._send("recovered", "TEST", "TEST recovery message from the Fleet Dashboard")
    except Exception as e:  # noqa: BLE001
        print("FAILED:", e)
        return 1
    print("Sent. Check the channel (a Teams Workflow can take a few seconds).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
