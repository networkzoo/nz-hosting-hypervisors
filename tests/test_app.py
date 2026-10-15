import httpx
import pytest

from app.alerts import AlertEngine
from app.collectors import poll
from app.config import ClusterConfig, Config
from app.store import EventDB


def pve_handler(offline=False):
    def h(req: httpx.Request):
        p = req.url.path
        assert req.headers["Authorization"].startswith("PVEAPIToken=")
        if p.endswith("/version"):
            return httpx.Response(200, json={"data": {"version": "8.2.4"}})
        if p.endswith("/cluster/resources"):
            return httpx.Response(200, json={"data": [
                {"type": "node", "node": "a", "status": "online", "cpu": 0.5, "maxcpu": 8, "mem": 4, "maxmem": 16},
                {"type": "node", "node": "b", "status": "offline" if offline else "online", "cpu": 0, "maxcpu": 8, "mem": 0, "maxmem": 16},
                {"type": "qemu", "status": "running", "name": "web1", "vmid": 100},
                {"type": "lxc", "status": "stopped", "name": "Backup", "vmid": 101},
                {"type": "qemu", "status": "running", "name": "api", "vmid": 102},
                {"type": "qemu", "status": "stopped", "template": 1, "name": "tmpl", "vmid": 9000},
            ]})
        if "/apt/update" in p:
            return httpx.Response(200, json={"data": [{"Package": "x"}] * 3})
        return httpx.Response(404)
    return h


PVE = ClusterConfig("pve", "proxmox", "https://pve", token_id="u@pve!t", token_secret="s")


async def test_proxmox_collect():
    async with httpx.AsyncClient(transport=httpx.MockTransport(pve_handler(offline=True))) as c:
        r = await poll(PVE, c)
    assert r["reachable"] and r["version"] == "8.2.4"
    assert [n["online"] for n in r["nodes"]] == [True, False]
    assert (r["vms_running"], r["vms_total"]) == (2, 3)
    # running first (by name), then not running; templates excluded; containers flagged
    assert [(v["name"], v["running"], v["kind"]) for v in r["vms"]] == [
        ("api", True, "vm"), ("web1", True, "vm"), ("Backup", False, "ct")]
    assert r["updates_pending"] == 3  # only the online node is queried
    assert r["mem_pct"] == 25.0 and r["cpu_pct"] == 50.0


async def test_scale_collect():
    def h(req):
        p = req.url.path
        if p.endswith("Cluster"): return httpx.Response(200, json=[{"icosVersion": "9.7.11"}])
        if p.endswith("Node"): return httpx.Response(200, json=[
            {"lanIP": "10.0.0.1", "networkStatus": "ONLINE", "cpuUsage": 10.0, "memUsagePercentage": 40.0,
             "drives": [{"isHealthy": True}, {"isHealthy": False}]},
            {"lanIP": "10.0.0.2", "networkStatus": "OFFLINE", "cpuUsage": 30.0, "memUsagePercentage": 60.0, "drives": []}])
        if p.endswith("VirDomain"): return httpx.Response(200, json=[{"state": "SHUTOFF", "name": "zeta"}, {"state": "RUNNING", "name": "Alpha"}])
    cfg = ClusterConfig("s", "scale", "https://s", username="u", password="p")
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        r = await poll(cfg, c)
    assert r["version"] == "9.7.11" and r["cpu_pct"] == 20.0 and r["mem_pct"] == 50.0
    assert [n["online"] for n in r["nodes"]] == [True, False]
    assert (r["vms_running"], r["vms_total"], r["drives_unhealthy"]) == (1, 2, 1)
    assert [(v["name"], v["running"], v["state"]) for v in r["vms"]] == [("Alpha", True, "running"), ("zeta", False, "shutoff")]


async def test_scale_vms_optional():
    def h(req):
        p = req.url.path
        if p.endswith("Cluster"): return httpx.Response(200, json=[{"icosVersion": "9"}])
        if p.endswith("Node"): return httpx.Response(200, json=[{"lanIP": "n", "networkStatus": "ONLINE"}])
        return httpx.Response(403)  # VirDomain denied
    cfg = ClusterConfig("s", "scale", "https://s", username="u", password="p")
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        r = await poll(cfg, c)
    assert r["reachable"] and r["vms_total"] is None and r["vms"] is None and len(r["nodes"]) == 1


async def test_drive_alert():
    sent = []
    async def notify(kind, cluster, msg): sent.append(msg)
    eng = AlertEngine(Config(fail_threshold=1), EventDB(":memory:"), notify)
    await eng.evaluate("c", {"reachable": True, "nodes": [], "drives_unhealthy": 2})
    assert sent == ["c: 2 drive(s) reported unhealthy"]


async def test_unreachable_does_not_raise():
    def h(req): raise httpx.ConnectError("boom")
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        r = await poll(PVE, c)
    assert r["reachable"] is False and "ConnectError" in r["error"]


async def test_alert_threshold_and_recovery():
    sent = []
    async def notify(kind, cluster, msg): sent.append((kind, msg))
    eng = AlertEngine(Config(fail_threshold=2), EventDB(":memory:"), notify)
    down = {"reachable": False, "error": "x", "nodes": []}
    up = {"reachable": True, "nodes": [{"name": "a", "online": True}]}
    await eng.evaluate("c", down); assert sent == []          # below threshold
    await eng.evaluate("c", down); assert len(sent) == 1       # fires once
    await eng.evaluate("c", down); assert len(sent) == 1       # no repeat spam
    await eng.evaluate("c", up);   assert sent[-1][0] == "recovered"


async def test_node_offline_alert():
    sent = []
    async def notify(kind, cluster, msg): sent.append(msg)
    eng = AlertEngine(Config(fail_threshold=1), EventDB(":memory:"), notify)
    await eng.evaluate("c", {"reachable": True, "nodes": [{"name": "n2", "online": False}]})
    assert sent == ["c: node n2 is offline"]
