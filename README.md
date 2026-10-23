# nz-hosting-hypervisors — fleet dashboard

One page showing every Proxmox and Scale Computing cluster: node/VM health, memory/CPU,
version, pending updates (Proxmox), plus **Open** (SSO/console link) and **Embed** (iframe) buttons.
Alerts by email and/or webhook when a cluster becomes unreachable or a node goes offline, and again on recovery.

## Run

```
cp config.example.yaml config.yaml     # edit clusters + alert settings
cp .env.example .env                   # secrets referenced by *_env keys
docker compose up -d --build           # http://127.0.0.1:8080
```
Or locally: `pip install -r requirements.txt && uvicorn app.main:app --port 8080`. Tests: `pytest`.

**Adding or removing clusters:** see [docs/clusters.md](docs/clusters.md).

## Credentials (read-only everywhere)
- **Proxmox**: API token with the `PVEAuditor` role. "Updates pending" additionally needs `Sys.Modify` on `/nodes`; without it that field shows as unknown.
- **Scale HyperCore**: a read-only user, HTTP basic auth against `/rest/v1/`.

## Alerting
Edge-triggered: fires after `fail_threshold` consecutive failed polls, once, then a recovery notice. Events are stored in SQLite and shown on the page. The webhook posts `{"text": ...}` (Slack/Teams/Mattermost compatible).

## Security notes
- The dashboard itself has **no login**. Bind to localhost (compose does) and put it behind your SSO reverse proxy (oauth2-proxy, Authentik, Cloudflare Access...).
- Embedding: Proxmox/Scale UIs may refuse iframes (`X-Frame-Options`) or be mixed-content blocked; the Embed view then offers "Open in new tab". Prefer Open with SSO links.

## Caveats
Scale field names (`networkStatus`, `icosVersion`, `memSize`, `state`) are from the HyperCore REST API as I know it and were only tested against mocks — verify against a real cluster first. Scale CPU and "updates pending" aren't collected yet.
