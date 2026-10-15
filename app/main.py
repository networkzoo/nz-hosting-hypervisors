import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .alerts import AlertEngine
from .collectors import poll
from .config import load_config
from .store import EventDB

STATIC = Path(__file__).parent / "static"
state: dict[str, dict] = {}


async def poll_loop(cfg, engine):
    while True:
        results = await asyncio.gather(*(poll(c) for c in cfg.clusters))
        for c, r in zip(cfg.clusters, results):
            prev = state.get(c.name, {})
            r["name"], r["type"], r["link"], r["url"] = c.name, c.type, c.link or c.url, c.url
            r["checked_at"] = time.time()
            r["last_ok"] = r["checked_at"] if r["reachable"] else prev.get("last_ok")
            state[c.name] = r
            await engine.evaluate(c.name, r)
        await asyncio.sleep(cfg.poll_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config(os.environ.get("CONFIG", "config.yaml"))
    db = EventDB(cfg.db_path)
    app.state.db = db
    task = asyncio.create_task(poll_loop(cfg, AlertEngine(cfg, db)))
    yield
    task.cancel()


app = FastAPI(title="Hypervisor Fleet Dashboard", lifespan=lifespan)


@app.get("/api/clusters")
def clusters():
    return sorted(state.values(), key=lambda c: c["name"])


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
