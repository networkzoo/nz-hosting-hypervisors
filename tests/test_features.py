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


def test_recipients_accept_string_list_and_commas(tmp_path):
    f = tmp_path / "c.yaml"
    for to, want in [("a@b.c", ["a@b.c"]), ("a@b.c, d@e.f;g@h.i", ["a@b.c", "d@e.f", "g@h.i"]),
                     (["a@b.c", " d@e.f "], ["a@b.c", "d@e.f"]), (None, [])]:
        f.write_text("alerts:\n  smtp: {host: h, to: %s}\n" % (repr(to) if to is not None else "~"))
        assert load_config(f).smtp.to == want


def test_teams_mention_only_on_down_and_stale_and_only_when_configured():
    eng, _ = mk(Config(webhook_format="teams", teams_mention=["gord.hynes@networkzoo.ca"]))
    card = eng.webhook_payload("down", "x")["attachments"][0]["content"]
    ent = card["msteams"]["entities"][0]
    assert ent["type"] == "mention" and ent["mentioned"]["id"] == "gord.hynes@networkzoo.ca"
    assert ent["text"] == "<at>Gord</at>" and any("<at>Gord</at>" in b["text"] for b in card["body"])
    assert "msteams" in eng.webhook_payload("stale", "x")["attachments"][0]["content"]
    assert "msteams" not in eng.webhook_payload("recovered", "x")["attachments"][0]["content"]
    plain, _ = mk(Config(webhook_format="teams"))
    assert "msteams" not in plain.webhook_payload("down", "x")["attachments"][0]["content"]


def test_teams_mention_config_accepts_string_or_list(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("alerts:\n  webhook_format: teams\n  teams_mention: a@b.c, d@e.f\n")
    assert load_config(f).teams_mention == ["a@b.c", "d@e.f"]


# ---------- hourly reminders + silencing ----------
HOUR = 3600


def down(name="n1"):
    return {"reachable": True, "nodes": [{"name": name, "online": False}]}


def up(name="n1"):
    return {"reachable": True, "nodes": [{"name": name, "online": True}]}


def clock(monkeypatch, start=1_000_000.0):
    t = [start]
    monkeypatch.setattr(alerts_mod.time, "time", lambda: t[0])
    return t


def mk2(**cfg):
    sent = []
    async def notify(kind, cluster, msg): sent.append((kind, msg))
    db = EventDB(":memory:")
    return AlertEngine(Config(fail_threshold=1, **cfg), db, notify), sent, db


async def test_down_repeats_hourly_with_how_long_and_recovers(monkeypatch):
    t = clock(monkeypatch); eng, sent, db = mk2()
    await eng.evaluate("c", down()); assert len(sent) == 1
    t[0] += 30 * 60; await eng.evaluate("c", down()); assert len(sent) == 1           # not yet
    t[0] += 31 * 60; await eng.evaluate("c", down())                                  # 61 min
    assert len(sent) == 2 and "still down after 1h 1m" in sent[-1][1] and sent[-1][0] == "down"
    t[0] += HOUR; await eng.evaluate("c", down()); assert len(sent) == 3 and "2h 1m" in sent[-1][1]
    await eng.evaluate("c", up()); assert sent[-1][0] == "recovered"
    assert [e["kind"] for e in db.recent()] == ["recovered", "down"]                    # reminders aren't history spam


async def test_repeat_hours_zero_disables_reminders(monkeypatch):
    t = clock(monkeypatch); eng, sent, _ = mk2(repeat_hours=0)
    await eng.evaluate("c", down()); t[0] += 5 * HOUR; await eng.evaluate("c", down())
    assert len(sent) == 1


async def test_silencing_before_the_first_alert_then_it_fires_when_the_silence_ends(monkeypatch):
    t = clock(monkeypatch); eng, sent, db = mk2()
    db.silence("c|node|n1", t[0] + 2 * HOUR)
    await eng.evaluate("c", down()); t[0] += HOUR; await eng.evaluate("c", down())
    assert sent == []                                                                  # silent while silenced
    t[0] += 2 * HOUR; await eng.evaluate("c", down())
    assert [k for k, _ in sent] == ["down"]                                            # silence over, still down: alert


async def test_silencing_an_active_alert_stops_reminders_until_it_expires(monkeypatch):
    t = clock(monkeypatch); eng, sent, db = mk2()
    await eng.evaluate("c", down()); assert len(sent) == 1
    db.silence("c|node|n1", t[0] + 3 * HOUR)
    t[0] += 2 * HOUR; await eng.evaluate("c", down()); assert len(sent) == 1           # reminder due but silenced
    t[0] += 2 * HOUR; await eng.evaluate("c", down()); assert len(sent) == 2           # silence expired: reminder resumes


async def test_silence_until_fixed_clears_on_recovery_and_recovery_still_reported(monkeypatch):
    t = clock(monkeypatch); eng, sent, db = mk2()
    await eng.evaluate("c", down())
    db.silence("c|node|n1", None)                                                      # until it recovers
    t[0] += 10 * HOUR; await eng.evaluate("c", down()); assert len(sent) == 1
    await eng.evaluate("c", up())
    assert sent[-1][0] == "recovered" and not db.is_silenced("c|node|n1")
    t[0] += HOUR; await eng.evaluate("c", down())                                      # goes down again later: alerts again
    assert len(sent) == 3


async def test_no_recovery_message_for_something_that_never_alerted(monkeypatch):
    clock(monkeypatch); eng, sent, db = mk2()
    db.silence("c|node|n1", None)
    await eng.evaluate("c", down()); await eng.evaluate("c", up())
    assert sent == []


def test_problems_list_for_the_page():
    eng, _, _ = mk2()
    p = eng.problems("c", {"reachable": True, "nodes": [{"name": "n1", "online": False}], "drives_unhealthy": 2,
                           "stale": True, "days_since_update": 40, "stale_limit": 30})
    assert [(x["key"], x["warn"]) for x in p] == [("c|node|n1", False), ("c|drives", False), ("c|stale", True)]
    assert eng.problems("c", up()) == []
    assert eng.problems("c", {"reachable": False, "error": "boom", "nodes": []})[0]["key"] == "c|cluster"


def test_silence_store_expiry_and_forever():
    db = EventDB(":memory:")
    db.silence("a", 100.0, now=0); db.silence("b", None, now=0)
    assert db.silences(now=50) == {"a": 100.0, "b": EventDB.FOREVER}
    assert db.silences(now=101) == {"b": EventDB.FOREVER}                              # a expired and dropped
    db.unsilence("b"); assert db.silences(now=101) == {}


# ---------- silence API ----------
def api_client(monkeypatch):
    from fastapi.testclient import TestClient
    main_mod.app.state.db = EventDB(":memory:")
    main_mod.state.clear()
    main_mod.state["c"] = {"name": "c", "problems": [{"key": "c|node|n1", "text": "c: node n1 is offline", "warn": False}]}
    return TestClient(main_mod.app)


def test_silence_api_requires_the_custom_header_and_validates(monkeypatch):
    c = api_client(monkeypatch)
    assert c.post("/api/silence", json={"key": "c|node|n1", "hours": 1}).status_code == 403     # no header (cross-site)
    h = {"X-Requested-With": "dashboard"}
    assert c.post("/api/silence", json={"key": "c|node|n1", "hours": 0}, headers=h).status_code == 400
    assert c.post("/api/silence", json={"key": "c|node|n1", "hours": 99999}, headers=h).status_code == 400
    assert c.post("/api/silence", json={"key": "", "hours": 1}, headers=h).status_code == 400


def test_silence_api_roundtrip_shows_in_clusters(monkeypatch):
    c = api_client(monkeypatch); h = {"X-Requested-With": "dashboard"}
    assert c.get("/api/clusters").json()[0]["problems"][0]["silenced_until"] is None
    r = c.post("/api/silence", json={"key": "c|node|n1", "hours": 4}, headers=h).json()
    assert r["ok"] and 3.9 * HOUR < r["until"] - __import__("time").time() <= 4 * HOUR
    assert c.get("/api/clusters").json()[0]["problems"][0]["silenced_until"] == r["until"]
    c.post("/api/silence", json={"key": "c|node|n1"}, headers=h)                       # until fixed
    assert c.get("/api/clusters").json()[0]["problems"][0]["silenced_until"] == EventDB.FOREVER
    assert c.post("/api/unsilence", json={"key": "c|node|n1"}, headers=h).json()["ok"]
    assert c.get("/api/clusters").json()[0]["problems"][0]["silenced_until"] is None
