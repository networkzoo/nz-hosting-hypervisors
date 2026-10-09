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

    def add_event(self, cluster, kind, message):
        self.conn.execute("INSERT INTO events VALUES(?,?,?,?)", (time.time(), cluster, kind, message))
        self.conn.commit()

    def recent(self, limit=50):
        rows = self.conn.execute(
            "SELECT ts,cluster,kind,message FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(ts=r[0], cluster=r[1], kind=r[2], message=r[3]) for r in rows]
