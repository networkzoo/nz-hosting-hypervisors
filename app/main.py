import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .alerts import AlertEngine
from .collectors import poll
from .deadman import ping as deadman_ping
from .config import load_config
from .store import EventDB

STATIC = Path(__file__).parent / "static"
state: dict[str, dict] = {}


log = logging.getLogger("poller")


def annotate_updates(cfg, c, r, db, now):
    """Work out 'last updated' and whether this cluster is overdue. Scale is tracked for display only."""
    info = db.observe_version(c.name, r["versions"], now)
    r["updated_at"], r["tracking_since"], r["last_change"] = info["changed_at"], info["first_seen"], info["last_change"]
    limit = cfg.stale_days if c.stale_days is None else c.stale_days
    if c.type == "proxmox" and limit > 0:
        days = (now - (info["changed_at"] or info["first_seen"])) / 86400
        r["days_since_update"], r["stale_limit"], r["stale"] = days, limit, days >= limit


async def poll_cycle(cfg, engine, db):
    results = await asyncio.gather(*(poll(c) for c in cfg.clusters))
    for c, r in zip(cfg.clusters, results):
        prev = state.get(c.name, {})
        now = time.time()
        r["name"], r["type"], r["link"], r["url"] = c.name, c.type, c.link or c.url, c.url
        r["ssh_user"] = c.ssh_user
        r["checked_at"] = now
        r["last_ok"] = now if r["reachable"] else prev.get("last_ok")
        if r["reachable"] and r.get("versions"):
            annotate_updates(cfg, c, r, db, now)
        elif prev.get("updated_at") is not None or prev.get("tracking_since"):
            # unreachable right now: keep showing the last known update info, but don't judge staleness
            for k in ("updated_at", "tracking_since", "last_change"):
                r[k] = prev.get(k)
        r["problems"] = engine.problems(c.name, r)
        state[c.name] = r
        await engine.evaluate(c.name, r)


async def poll_loop(cfg, engine, db):
    while True:
        try:
            await poll_cycle(cfg, engine, db)
            await deadman_ping(cfg.deadman_url)   # only reached when a whole cycle completed
        except Exception:  # noqa: BLE001 - keep polling; a missing ping is what tells you something's wrong
            log.exception("poll cycle failed")
        await asyncio.sleep(cfg.poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.environ.get("CONFIG", "config.yaml"))
    db = EventDB(cfg.db_path)
    app.state.db = db
    task = asyncio.create_task(poll_loop(cfg, AlertEngine(cfg, db), db))
    yield
    task.cancel()


app = FastAPI(title="Hypervisor Fleet Dashboard", lifespan=lifespan)


def with_silences(c: dict, silenced: dict) -> dict:
    out = dict(c)
    out["problems"] = [dict(p, silenced_until=silenced.get(p["key"])) for p in c.get("problems", [])]
    return out


@app.get("/api/clusters")
def clusters():
    silenced = app.state.db.silences(time.time())
    return [with_silences(c, silenced) for c in sorted(state.values(), key=lambda c: c["name"])]


class SilenceIn(BaseModel):
    key: str
    hours: float | None = None   # None = until the problem recovers


def _require_xhr(x_requested_with: str | None):
    # A custom header can't be sent by a cross-site form, so this blocks drive-by requests that
    # would otherwise ride on the browser's saved login.
    if x_requested_with != "dashboard":
        raise HTTPException(403, "missing X-Requested-With: dashboard")


@app.post("/api/silence")
def silence(body: SilenceIn, x_requested_with: str | None = Header(None)):
    _require_xhr(x_requested_with)
    if not body.key or len(body.key) > 300:
        raise HTTPException(400, "bad key")
    if body.hours is not None and not (0 < body.hours <= 24 * 30):
        raise HTTPException(400, "hours must be between 0 and 720")
    until = None if body.hours is None else time.time() + body.hours * 3600
    app.state.db.silence(body.key, until)
    return {"ok": True, "until": until}


@app.post("/api/unsilence")
def unsilence(body: SilenceIn, x_requested_with: str | None = Header(None)):
    _require_xhr(x_requested_with)
    app.state.db.unsilence(body.key)
    return {"ok": True}


@app.get("/api/events")
def events():
    return app.state.db.recent()


@app.get("/api/clusters/{name}")
def cluster(name: str):
    if name not in state:
        raise HTTPException(404)
    return state[name]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
