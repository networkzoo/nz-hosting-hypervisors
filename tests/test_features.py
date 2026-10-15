import asyncio
import json

import httpx
import pytest

import app.alerts as alerts_mod
import app.deadman as deadman_mod
import app.main as main_mod
from app.alerts import AlertEngine
from app.collectors import poll
from app.config import ClusterConfig, Config, load_config
from app.store import EventDB

DAY = 86400
N1 = {"a": {"pve": "9.1.8", "kernel": "6.14.11-4-pve"}}
N2 = {"a": {"pve": "9.1.9", "kernel": "6.14.11-5-pve"}}


# ---------- version tracking ----------
def test_first_observation_is_not_a_change():
    db = EventDB(":memory:")
    r = db.observe_version("c", N1, now=1000)
    assert r["changed_at"] is None and r["first_seen"] == 1000
    assert db.observe_version("c", N1, now=2000)["changed_at"] is None
    assert db.recent() == []


def test_change_is_recorded_with_detail_and_event():
    db = EventDB(":memory:")
    db.observe_version("c", N1, now=1000)
    r = db.observe_version("c", N2, now=5000)
    assert r["changed_at"] == 5000 and r["first_seen"] == 1000
    assert "pve 9.1.8 -> 9.1.9" in r["last_change"] and "kernel" in r["last_change"]
    assert db.recent()[0]["kind"] == "updated"


def test_node_offline_or_joining_is_not_an_update():
    db = EventDB(":memory:")
    two = {"a": N1["a"], "b": N1["a"]}
    db.observe_version("c", two, now=1000)
    assert db.observe_version("c", {"a": N1["a"]}, now=2000)["changed_at"] is None      # b offline
    assert db.observe_version("c", two, now=3000)["changed_at"] is None                 # b back, same version
    assert db.observe_version("c", {**two, "z": N1["a"]}, now=4000)["changed_at"] is None  # new node joins
    # ...but when b comes back already updated, that IS a change
    assert db.observe_version("c", {"b": N2["a"]}, now=5000)["changed_at"] == 5000


# ---------- stale logic (30-day rule) ----------
def cfgs(**kw):
    return Config(stale_days=30), ClusterConfig("c", "proxmox", "https://x", **kw)


def test_stale_only_after_30_days_without_change():
    cfg, c = cfgs(); db = EventDB(":memory:")
    r = {"versions": N1}; main_mod.annotate_updates(cfg, c, r, db, 0)
    assert r["stale"] is False and r["updated_at"] is None
    r = {"versions": N1}; main_mod.annotate_updates(cfg, c, r, db, 29 * DAY)
    assert r["stale"] is False
    r = {"versions": N1}; main_mod.annotate_updates(cfg, c, r, db, 31 * DAY)
    assert r["stale"] is True and int(r["days_since_update"]) == 31
    r = {"versions": N2}; main_mod.annotate_updates(cfg, c, r, db, 32 * DAY)   # updated -> clock resets
    assert r["stale"] is False and r["updated_at"] == 32 * DAY


def test_scale_is_tracked_but_never_stale_and_override_works():
    cfg = Config(stale_days=30); db = EventDB(":memory:")
    scale = ClusterConfig("s", "scale", "https://s")
    r = {"versions": {"cluster": {"version": "9.7"}}}; main_mod.annotate_updates(cfg, scale, r, db, 0)
    r = {"versions": {"cluster": {"version": "9.7"}}}; main_mod.annotate_updates(cfg, scale, r, db, 90 * DAY)
    assert "stale" not in r and r["tracking_since"] == 0
    c = ClusterConfig("p", "proxmox", "https://p", stale_days=0)       # per-cluster: off
    r = {"versions": N1}; main_mod.annotate_updates(cfg, c, r, db, 0)
    r = {"versions": N1}; main_mod.annotate_updates(cfg, c, r, db, 90 * DAY)
    assert "stale" not in r


# ---------- alerting ----------
def mk(cfg=None):
    sent = []
    async def notify(kind, cluster, msg): sent.append((kind, msg))
    return AlertEngine(cfg or Config(fail_threshold=1, stale_reminder_days=7), EventDB(":memory:"), notify), sent


def res(stale, days=35):
    return {"reachable": True, "nodes": [], "stale": stale, "days_since_update": days, "stale_limit": 30}


async def test_stale_alert_fires_once_reminds_weekly_and_recovers(monkeypatch):
    eng, sent = mk()
    t = [1_000_000.0]
    monkeypatch.setattr(alerts_mod.time, "time", lambda: t[0])
    await eng.evaluate("c", res(True)); assert [k for k, _ in sent] == ["stale"]
    t[0] += 3 * DAY; await eng.evaluate("c", res(True)); assert len(sent) == 1          # no spam
    t[0] += 5 * DAY; await eng.evaluate("c", res(True))                                  # 8 days: reminder
    assert len(sent) == 2 and sent[-1][1].endswith("(reminder)")
    await eng.evaluate("c", res(False, 0)); assert sent[-1][0] == "recovered"
    assert "updates detected again" in sent[-1][1]


def test_teams_payload_is_an_adaptive_card_and_text_is_plain():
    eng, _ = mk(Config(webhook_format="teams"))
    p = eng.webhook_payload("down", "Aviary is unreachable")
    card = p["attachments"][0]
    assert p["type"] == "message" and card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"
    assert card["content"]["body"][0]["color"] == "Attention" and card["content"]["body"][1]["text"] == "Aviary is unreachable"
    assert eng.webhook_payload("stale", "x")["attachments"][0]["content"]["body"][0]["color"] == "Warning"
    assert eng.webhook_payload("recovered", "x")["attachments"][0]["content"]["body"][0]["color"] == "Good"
    plain, _ = mk(Config(webhook_format="text"))
    assert plain.webhook_payload("down", "boom") == {"text": "[DOWN] boom"}


def patch_http(monkeypatch, module, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


async def test_webhook_failure_is_reported_not_swallowed(monkeypatch):
    patch_http(monkeypatch, alerts_mod, lambda req: httpx.Response(400, text="bad"))
    eng = AlertEngine(Config(webhook_url="https://hook", webhook_format="teams"), EventDB(":memory:"))
    with pytest.raises(RuntimeError, match="webhook"):
        await eng._send("down", "c", "msg")


async def test_webhook_success_posts_json_and_email_failure_does_not_block_it(monkeypatch):
    seen = []
    def h(req): seen.append(json.loads(req.content)); return httpx.Response(202)
    patch_http(monkeypatch, alerts_mod, h)
    cfg = Config(webhook_url="https://hook", webhook_format="teams")
    cfg.smtp.host, cfg.smtp.to = "smtp.invalid", ["a@b.c"]
    eng = AlertEngine(cfg, EventDB(":memory:"))
    monkeypatch.setattr(eng, "_smtp", lambda *a: (_ for _ in ()).throw(OSError("smtp down")))
    with pytest.raises(RuntimeError, match="email: smtp down"):
        await eng._send("down", "c", "msg")
    assert len(seen) == 1 and seen[0]["type"] == "message"        # webhook still went out


# ---------- dead-man switch ----------
async def test_deadman_ping_ok_and_failure_never_raises(monkeypatch):
    hits = []
    patch_http(monkeypatch, deadman_mod, lambda req: (hits.append(str(req.url)), httpx.Response(200))[1])
    assert await deadman_mod.ping("http://hc/ping/abc") is True and hits == ["http://hc/ping/abc"]
    assert await deadman_mod.ping("") is False
    patch_http(monkeypatch, deadman_mod, lambda req: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert await deadman_mod.ping("http://hc/ping/abc") is False


async def test_loop_pings_after_good_cycle_but_not_after_a_failed_one(monkeypatch):
    pings = []
    async def fake_ping(url): pings.append(url)
    monkeypatch.setattr(main_mod, "deadman_ping", fake_ping)
    calls = {"n": 0}
    async def cycle(cfg, engine, db):
        calls["n"] += 1
        if calls["n"] == 1: raise RuntimeError("boom")
    monkeypatch.setattr(main_mod, "poll_cycle", cycle)
    async def stop(_):
        if calls["n"] >= 2: raise asyncio.CancelledError
    monkeypatch.setattr(main_mod.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await main_mod.poll_loop(Config(deadman_url="http://hc/p", poll_interval=1), None, None)
    assert pings == ["http://hc/p"]      # cycle 1 failed -> no ping; cycle 2 ok -> one ping


# ---------- config ----------
def test_config_new_options(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("deadman_url: http://hc/p\nalerts:\n  webhook_format: Teams\n  stale_days: 45\nclusters:\n"
                 "  - {name: a, type: proxmox, url: 'https://a', ssh_user: ops, stale_days: 0}\n")
    c = load_config(f)
    assert (c.webhook_format, c.stale_days, c.deadman_url) == ("teams", 45, "http://hc/p")
    assert (c.clusters[0].ssh_user, c.clusters[0].stale_days) == ("ops", 0)
    f.write_text("alerts: {webhook_format: slackish}\n")
    with pytest.raises(ValueError): load_config(f)
