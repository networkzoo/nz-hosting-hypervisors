# Dead-man's switch (self-hosted Healthchecks)

The dashboard pings this after every complete poll cycle. If the pings stop for longer than the
check's period + grace, Healthchecks tells you in Teams - which covers the dashboard VM, its
Docker stack, or its network being down (things the dashboard can't alert about itself).

**Where to run it:** a machine that is *not* the dashboard VM, not on the same Proxmox host, and not
on the same storage (a NAS that hosts the dashboard's disk would take both down). Any small Docker
host at another site or on separate hardware works. It needs: outbound HTTPS to Teams, and the
dashboard VM must be able to reach it on port 8000.

## Set up
```
cp .env.example .env     # set SECRET_KEY (openssl rand -hex 32), SITE_ROOT, ALLOWED_HOSTS
docker compose up -d
docker compose exec healthchecks /opt/healthchecks/manage.py createsuperuser
```
(If that path differs in the image version you pulled, `docker compose exec healthchecks ls /opt`
shows where `manage.py` lives.) Then open `SITE_ROOT` and log in.

1. **Create a check** named "Fleet Dashboard poller". Schedule: period **5 minutes**, grace **5 minutes**
   (the dashboard polls every 60 s). Copy its **ping URL** (`SITE_ROOT/ping/<uuid>`).
2. **Add the alert channel** (Integrations > Webhook), pointing at your Teams Workflow URL (see
   `deploy/README.md`, "Teams alerts"). Healthchecks lets you set the request body for "down" and "up":

   Down:
   ```
   {"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","contentUrl":null,"content":{"type":"AdaptiveCard","version":"1.4","body":[{"type":"TextBlock","text":"WATCHDOG: $NAME is DOWN","weight":"Bolder","color":"Attention","wrap":true},{"type":"TextBlock","text":"No heartbeat from the Fleet Dashboard. The dashboard VM, its Docker stack or its network may be down.","wrap":true}]}}]}
   ```
   Up:
   ```
   {"type":"message","attachments":[{"contentType":"application/vnd.microsoft.card.adaptive","contentUrl":null,"content":{"type":"AdaptiveCard","version":"1.4","body":[{"type":"TextBlock","text":"WATCHDOG: $NAME is back UP","weight":"Bolder","color":"Good","wrap":true}]}}]}
   ```
   Set the method to POST and the content type to `application/json`. Menu names vary a little by version.
3. **Tell the dashboard** (on the dashboard VM): in `/opt/dashboard/.env` add
   `DEADMAN_URL=http://<this-host>:8000/ping/<uuid>`, and in `config.yaml` (top level, not under `alerts`)
   add `deadman_url_env: DEADMAN_URL`. Then `docker compose up -d --force-recreate dashboard`.
4. **Test it:** the check should turn green within a minute. Then `docker compose stop dashboard` on the
   VM; after about 10 minutes (period + grace) Teams should get the DOWN message. Start it again and
   expect the UP message.

The dashboard only pings after a *complete* poll cycle, so a stuck or crashing poller also stops the pings.
