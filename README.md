# Fleet Dashboard (nz-hosting-hypervisors)

One page that shows the health of every **Proxmox VE** and **Scale Computing HyperCore** cluster, and
tells you in **Teams** and/or **email** when something goes wrong.

**What you see**
- A card per cluster: nodes online, VMs running (expand to list them, running first), memory, CPU, version,
  when it was last updated, pending-update counts (Proxmox), unhealthy drives (Scale).
- An **Active problems** list built from live state (cluster unreachable, node offline, unhealthy drive, update
  overdue). Any problem can be **silenced** for 1h/4h/24h/7d or until it is fixed.
- **Recent events** history (new ones bold until your next visit; **Clear** is per person).
- Per-card buttons: **Open** (console / SSO link), **Embed** (framed console), per-node **Updates** (jumps to that
  node's Updates page), per-node **SSH (copy)** (copies `ssh root@<ip>` for Termius).
- Works on a phone, in light and dark mode.

**What it does for you**
- Polls every cluster read-only (default every 60 s).
- Alerts when a **node goes offline** or a **cluster is unreachable** (after 2 failed checks, so blips don't page you),
  **re-alerts every hour** while it stays down, and says when it recovers. Teams cards can **@mention** you so the phone pushes.
- Alerts if a Proxmox cluster shows **no update for 30 days** (reminder weekly). Scale is tracked but never alerts.
- An optional **dead-man's switch**: it pings an external Healthchecks after every poll cycle, so you hear about it if the
  dashboard itself (or its VM) dies.

```
 browser ──https──> Caddy (login + TLS) ──> dashboard app (FastAPI, polls every cluster)
                                              ├─ Proxmox API  (read-only token)
                                              ├─ Scale REST API (read-only user)
                                              ├─ SQLite  data/events.db  (events, silences, version history)
                                              ├─> Teams webhook / SMTP email   (alerts)
                                              └─> Healthchecks ping            (dead-man's switch)
```

## Quick start
1. Build the VM (Ubuntu cloud image on Proxmox): [`deploy/README.md`](deploy/README.md) (`deploy/proxmox-create-vm.sh`).
2. On the VM: `git clone` this repo to `/opt/dashboard`, then
   ```
   cp config.example.yaml config.yaml && cp .env.example .env
   deploy/set-password.sh admin            # the web login (Caddy basic auth)
   docker compose up -d --build            # https://<vm-address>  (self-signed certificate warning is expected)
   ```
3. Add clusters and alert secrets with the helper (never hand-edit secrets): [`docs/clusters.md`](docs/clusters.md).
4. Set up Teams / email alerts and test them: see **Alerts** below.

## Day-to-day
| Task | Command (on the VM, in `/opt/dashboard`) |
|---|---|
| Update the dashboard | `git pull && docker compose up -d --build` (code is inside the image, so `--build`) |
| Add / remove / rename a cluster, fix a secret | `python3 deploy/cluster_config.py add\|remove\|rename\|set-secret ...` ([guide](docs/clusters.md)) |
| Check config and secrets | `python3 deploy/cluster_config.py check` |
| Store any secret (SMTP password, Teams URL) | `python3 deploy/cluster_config.py set-env NAME` |
| Send a test alert | `docker compose exec dashboard python -m app.testalert` (`--delay 30`, `--kind down\|stale`) |
| Reset the web password | `deploy/set-password.sh admin && docker compose restart caddy` |
| See logs | `docker compose logs -f dashboard` |

## Configuration (`config.yaml`)
Secrets are never in this file. A line ending `_env` holds the **name** of a variable in `.env`.

| Key | Meaning | Default |
|---|---|---|
| `poll_interval` | seconds between polls | 60 |
| `alerts.fail_threshold` | consecutive failed polls before alerting | 2 |
| `alerts.repeat_hours` | re-alert this often while still down (0 = off) | 1 |
| `alerts.stale_days` | Proxmox: alert when no update seen for N days (0 = off); per cluster `stale_days:` overrides | 30 |
| `alerts.stale_reminder_days` | repeat the overdue alert this often | 7 |
| `alerts.webhook_url_env`, `alerts.webhook_format` | Teams (or Slack-style) webhook; `teams` or `text` | `text` |
| `alerts.teams_mention` | account(s) to @mention on DOWN/overdue Teams cards | none |
| `alerts.smtp.*` | `host, port, starttls, username, password_env, from, to` (`to` may be one address or several, comma-separated) | none |
| `deadman_url_env` | Healthchecks ping URL variable (top level) | none |
| cluster `type` | `proxmox` or `scale` | |
| cluster `url`, `link` | API address; optional console/SSO link for **Open** | link = url |
| cluster `token_id` + `token_secret_env` | Proxmox API token | |
| cluster `username` + `password_env` | Scale read-only user | |
| cluster `verify_tls` | verify the cluster's HTTPS certificate | **true** (set `false` for self-signed) |
| cluster `ssh_user` | login used by the SSH (copy) buttons | `root` |

## Alerts
- **What triggers one:** node offline, cluster unreachable, Scale drive unhealthy (each after `fail_threshold` checks); a
  Proxmox cluster with no update seen for `stale_days`.
- **Reminders:** a problem that stays down re-alerts every `repeat_hours` ("still down after 3h"). Reminders are not added
  to the history list. Overdue-update reminders repeat every `stale_reminder_days`.
- **Silence:** on the **Active problems** list choose *Silence…* (1h / 4h / 24h / 7d / until fixed). A silenced problem
  stays visible, sends nothing, resumes reminders when the silence ends, and "until fixed" ends by itself on recovery. If it
  recovered after having alerted, you still get the recovery message. Silences are shared by everyone who can log in.
- **Teams:** create a Workflow in the channel ("Send webhook alerts to a channel" / "Post to a channel when a webhook request
  is received"), save its URL with `set-env TEAMS_WEBHOOK`, set `webhook_url_env`, `webhook_format: teams`, and
  `teams_mention: you@yourdomain` (a mention is what makes Teams push to your phone). Details and fixes: [`deploy/README.md`](deploy/README.md).
- **Email:** the `alerts.smtp` block (the sender address must be verified at your SMTP provider).
- **Restarting the dashboard** re-alerts anything still down (the alert state is in memory; silences and history are saved).
- **Dead-man's switch:** [`deadman/README.md`](deadman/README.md) (self-hosted Healthchecks on a different machine/site).

## Accounts and permissions (read-only everywhere)
- **Proxmox:** `deploy/pve-create-monitor.sh "<name>"` on one node per cluster makes `monitor@pve` with the `PVEAuditor` role
  and a `dashboard` token. `--with-updates` additionally grants `Sys.Modify` on `/nodes` (a little more than read-only) so
  pending-update counts can be shown. `--rotate` replaces a token.
- **Scale:** create a read-only `monitor` user in the Scale UI.

## Security notes
- The app has no login of its own: it listens only inside Docker, and **Caddy** provides HTTPS and a basic-auth login.
  Keep the VM's firewall tight (SSH from your admin networks, 443 only).
- Secrets live in `/opt/dashboard/.env` on the VM (plain text, owner-only). Use read-only accounts so a leak is low impact.
- Silence requests need a custom header, so another website can't trigger them through your logged-in browser.
- The dashboard can't change anything on your clusters. "Updates" and "Embed" just open the cluster's own console.

## Repository layout
```
app/            FastAPI app: collectors (Proxmox, Scale), alert engine, storage, dead-man ping, static page
deploy/         VM build (cloud-init), Caddy login helper, cluster_config.py, pve-create-monitor.sh, runbook
deadman/        Self-hosted Healthchecks (compose + setup)
docs/           clusters.md (add/remove clusters)
scripts/windows dev-setup.ps1 (move/clone repos out of OneDrive to C:\dev)
tests/          pytest suite (pip install -r requirements.txt && pytest)
Caddyfile, docker-compose.yml, Dockerfile, config.example.yaml, .env.example
```

## Limits worth knowing
- Scale: CPU/memory come from per-node stats; update tracking is display-only and Scale never alerts on staleness.
- "Last updated" starts as "watching since <date>" because the real date of the last update isn't knowable the first time
  a cluster is seen; the 30 days count from then.
- Embedding a console can be blocked by the cluster (frame headers); use **Open in new tab** then.
- Alerts depend on the dashboard VM being up. Use the dead-man's switch for that.
