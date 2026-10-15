import json
import sqlite3
import time
from pathlib import Path


class EventDB:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS events(ts REAL, cluster TEXT, kind TEXT, message TEXT)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS versions(cluster TEXT PRIMARY KEY, nodes TEXT, "
            "changed_at REAL, first_seen REAL, last_change TEXT)"
        )

    def add_event(self, cluster, kind, message):
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (time.time(), cluster, kind, message))
        self.conn.commit()

    def recent(self, limit=50):
        rows = self.conn.execute(
            "SELECT ts,cluster,kind,message FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(ts=r[0], cluster=r[1], kind=r[2], message=r[3]) for r in rows]

    def observe_version(self, cluster: str, nodes: dict, now: float | None = None) -> dict:
        """Record what each node runs; note when it changes.

        `nodes` is {node: {"pve": "9.1.9", "kernel": "6.14.11-5-pve"}}. Only nodes seen before AND now
        are compared, so a node going offline (or joining) is not mistaken for an update. The first
        observation can't tell us when the cluster was last updated, so changed_at stays None until
        a change is actually seen.
        """
        now = time.time() if now is None else now
        row = self.conn.execute(
            "SELECT nodes, changed_at, first_seen, last_change FROM versions WHERE cluster=?", (cluster,)
        ).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO versions VALUES(?,?,?,?,?)",
                              (cluster, json.dumps(nodes), None, now, ""))
            self.conn.commit()
            return {"changed_at": None, "first_seen": now, "last_change": ""}
        old, changed_at, first_seen, last_change = json.loads(row[0]), row[1], row[2], row[3]
        diffs = []
        for node, new in nodes.items():
            prev = old.get(node)
            if prev is not None and prev != new:
                parts = [f"{k} {prev.get(k, '?')} -> {new.get(k, '?')}" for k in new if prev.get(k) != new.get(k)]
                diffs.append(f"{node}: " + ", ".join(parts))
        if diffs:
            changed_at, last_change = now, "; ".join(diffs)
            self.add_event(cluster, "updated", f"{cluster}: " + last_change)
        merged = {**old, **nodes}
        if diffs or merged != old:
            self.conn.execute("UPDATE versions SET nodes=?, changed_at=?, last_change=? WHERE cluster=?",
                              (json.dumps(merged), changed_at, last_change, cluster))
            self.conn.commit()
        return {"changed_at": changed_at, "first_seen": first_seen, "last_change": last_change}
