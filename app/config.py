"""Load config.yaml. Secrets are referenced by env-var name, never stored in the file."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ClusterConfig:
    name: str
    type: str  # "proxmox" | "scale"
    url: str
    verify_tls: bool = True
    link: str = ""  # SSO / console URL for the "Open" button (defaults to url)
    # proxmox
    token_id: str = ""
    token_secret: str = ""
    # scale
    username: str = ""
    password: str = ""
    ssh_user: str = "root"           # used for the "copy ssh command" buttons (Proxmox)
    stale_days: int | None = None    # per-cluster override of alerts.stale_days (0 = off)


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    starttls: bool = True
    sender: str = ""
    to: list[str] = field(default_factory=list)


@dataclass
class Config:
    poll_interval: int = 60
    fail_threshold: int = 2  # consecutive failed polls before alerting
    db_path: str = "data/events.db"
    smtp: SmtpConfig = field(default_factory=SmtpConfig)
    webhook_url: str = ""
    webhook_format: str = "text"     # "text" (Slack/Mattermost style) or "teams" (Workflows adaptive card)
    stale_days: int = 30             # alert when a Proxmox cluster shows no update for this many days (0 = off)
    stale_reminder_days: int = 7     # repeat the stale alert this often while it stays stale
    deadman_url: str = ""            # pinged after every successful poll cycle
    clusters: list[ClusterConfig] = field(default_factory=list)


def _secret(d: dict, key: str) -> str:
    """Read `<key>_env` (preferred) or a literal `<key>`."""
    env = d.get(f"{key}_env")
    if env:
        return os.environ.get(env, "")
    return d.get(key, "") or ""


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    alerts = raw.get("alerts", {}) or {}
    s = alerts.get("smtp", {}) or {}
    cfg = Config(
        poll_interval=int(raw.get("poll_interval", 60)),
        fail_threshold=int(alerts.get("fail_threshold", 2)),
        db_path=raw.get("db_path", "data/events.db"),
        smtp=SmtpConfig(
            host=s.get("host", ""),
            port=int(s.get("port", 587)),
            username=s.get("username", ""),
            password=_secret(s, "password"),
            starttls=bool(s.get("starttls", True)),
            sender=s.get("from", ""),
            to=list(s.get("to", []) or []),
        ),
        webhook_url=_secret(alerts, "webhook_url"),
        webhook_format=str(alerts.get("webhook_format", "text")).lower(),
        stale_days=int(alerts.get("stale_days", 30)),
        stale_reminder_days=int(alerts.get("stale_reminder_days", 7)),
        deadman_url=_secret(raw, "deadman_url"),
    )
    if cfg.webhook_format not in ("text", "teams"):
        raise ValueError("alerts.webhook_format must be 'text' or 'teams'")
    for c in raw.get("clusters", []) or []:
        if c["type"] not in ("proxmox", "scale"):
            raise ValueError(f"cluster {c.get('name')}: type must be proxmox or scale")
        cfg.clusters.append(
            ClusterConfig(
                name=c["name"],
                type=c["type"],
                url=c["url"].rstrip("/"),
                verify_tls=bool(c.get("verify_tls", True)),
                link=c.get("link", ""),
                token_id=c.get("token_id", ""),
                token_secret=_secret(c, "token_secret"),
                username=c.get("username", ""),
                password=_secret(c, "password"),
                ssh_user=c.get("ssh_user", "root"),
                stale_days=None if c.get("stale_days") is None else int(c["stale_days"]),
            )
        )
    return cfg
