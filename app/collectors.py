"""Poll cluster APIs and normalise to a common shape.

Result dict:
  reachable, error, version, nodes: [{name, online}], vms_running, vms_total,
  cpu_pct, mem_pct, updates_pending (None = unknown)
"""
import httpx

from .config import ClusterConfig

TIMEOUT = 15.0


def _pct(used, total):
    return round(100 * used / total, 1) if total else None


async def collect_proxmox(c: ClusterConfig, client: httpx.AsyncClient) -> dict:
    headers = {"Authorization": f"PVEAPIToken={c.token_id}={c.token_secret}"}

    async def get(path):
        r = await client.get(f"{c.url}/api2/json{path}", headers=headers)
        r.raise_for_status()
        return r.json()["data"]

    version = (await get("/version"))["version"]
    resources = await get("/cluster/resources")
    nodes = [
        {"name": r["node"], "online": r.get("status") == "online"}
        for r in resources
        if r["type"] == "node"
    ]
    guests = [r for r in resources if r["type"] in ("qemu", "lxc") and not r.get("template")]
    node_res = [r for r in resources if r["type"] == "node" and r.get("status") == "online"]

    # Pending apt updates: needs Sys.Modify on the token, so best-effort only.
    pending = 0
    try:
        for n in nodes:
            if n["online"]:
                pending += len(await get(f"/nodes/{n['name']}/apt/update"))
    except (httpx.HTTPError, KeyError, ValueError):
        pending = None

    return {
        "version": version,
        "nodes": nodes,
        "vms_running": sum(1 for g in guests if g.get("status") == "running"),
        "vms_total": len(guests),
        "cpu_pct": _pct(sum(r.get("cpu", 0) * r.get("maxcpu", 0) for r in node_res),
                        sum(r.get("maxcpu", 0) for r in node_res)),
        "mem_pct": _pct(sum(r.get("mem", 0) for r in node_res),
                        sum(r.get("maxmem", 0) for r in node_res)),
        "updates_pending": pending,
    }


async def collect_scale(c: ClusterConfig, client: httpx.AsyncClient) -> dict:
    auth = (c.username, c.password)

    async def get(path):
        r = await client.get(f"{c.url}/rest/v1/{path}", auth=auth)
        r.raise_for_status()
        return r.json()

    cluster = (await get("Cluster") or [{}])[0]
    nodes_raw = await get("Node")
    nodes = [
        {"name": n.get("lanIP") or n.get("uuid", "?"),
         "online": str(n.get("networkStatus", "")).upper() == "ONLINE"}
        for n in nodes_raw
    ]

    def avg(key):
        vals = [n[key] for n in nodes_raw if isinstance(n.get(key), (int, float))]
        return round(sum(vals) / len(vals), 1) if vals else None

    # VM counts are best-effort: a role without VirDomain access shouldn't blank the card.
    try:
        vms = await get("VirDomain")
        vms_running = sum(1 for v in vms if v.get("state") == "RUNNING")
        vms_total = len(vms)
    except (httpx.HTTPError, ValueError):
        vms_running = vms_total = None

    drives = [d for n in nodes_raw for d in n.get("drives", [])]
    return {
        "version": cluster.get("icosVersion", ""),
        "nodes": nodes,
        "vms_running": vms_running,
        "vms_total": vms_total,
        "cpu_pct": avg("cpuUsage"),
        "mem_pct": avg("memUsagePercentage"),
        "drives_unhealthy": sum(1 for d in drives if d.get("isHealthy") is False),
        "updates_pending": None,
    }


COLLECTORS = {"proxmox": collect_proxmox, "scale": collect_scale}


async def poll(c: ClusterConfig, client: httpx.AsyncClient | None = None) -> dict:
    """Never raises: failures come back as reachable=False with an error string."""
    own = client is None
    client = client or httpx.AsyncClient(verify=c.verify_tls, timeout=TIMEOUT)
    try:
        data = await COLLECTORS[c.type](c, client)
        return {"reachable": True, "error": "", **data}
    except Exception as e:  # noqa: BLE001 - any failure means "can't see cluster"
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"[:300], "nodes": []}
    finally:
        if own:
            await client.aclose()
