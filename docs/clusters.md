# Adding and removing clusters

Everything below is run on the **dashboard VM** in `/opt/dashboard` unless it says "on the cluster".
You never need to edit `config.yaml` or `.env` by hand; the helper does it and keeps a `config.yaml.bak`.

```
cd /opt/dashboard && git pull        # do this first so you have the latest scripts
```

## Add a Proxmox cluster

**1. On the cluster (one node only, as root):** create the read-only login and get a token.

From your laptop (Command Prompt), nothing is saved on the node:
```
type C:\dev\hypervisor-web\deploy\pve-create-monitor.sh | ssh root@<node-address> "bash -s -- 'Cluster Name' --with-updates"
```
Or paste the script onto the node and run `./pve-create-monitor.sh "Cluster Name" --with-updates`.

- It prints a **token secret once**. Copy it straight away.
- `--with-updates` also lets the dashboard show pending-update counts (needs `Sys.Modify`, a little more than
  read-only). Leave it off if you don't want that.
- If it says the token **already exists**, no secret is shown. Run it again with `--rotate` to make a new one.
- If it says "unknown option", the copy of the script is out of date. Re-copy the latest one.

**2. On the dashboard VM:** add it.
```
python3 deploy/cluster_config.py add proxmox "Cluster Name" https://<node-address>:8006 --apply
```
Paste the token secret when asked (hidden). `--apply` restarts the dashboard; the card appears within a minute.
Optional extras added to the command: `--user`/`--token-id` only if you changed the defaults; for a different
SSH login or overdue limit, add `ssh_user: ops` / `stale_days: 45` to that cluster's block in `config.yaml`.

## Add a Scale cluster

1. In the Scale web UI, create a **read-only** user (for example `monitor`). Not an admin.
2. On the VM:
```
python3 deploy/cluster_config.py add scale "Cluster Name" https://<scale-address> --user monitor --apply
```
Enter that user's password when asked.

## Remove a cluster
```
python3 deploy/cluster_config.py remove "Cluster Name" --drop-secret --apply
```
`--drop-secret` also deletes its secret from `.env` (it's kept if another cluster still uses it).
Optional tidy-up on the cluster itself, so the old token can't be used any more:
`pveum user token remove monitor@pve dashboard` (add `pveum user delete monitor@pve` if no other
dashboard uses that user). Scale: delete the `monitor` user in its UI.

## Other changes
| I want to... | Command |
|---|---|
| Rename a cluster | `python3 deploy/cluster_config.py rename "Old" "New" --apply` |
| Rename one whose name starts with `-` | `python3 deploy/cluster_config.py rename --apply -- "--odd" "New"` |
| Fix a wrong or rotated secret | `python3 deploy/cluster_config.py set-secret "Name" --apply` |
| See what's configured and whether every secret is present | `python3 deploy/cluster_config.py check` (or `list`) |
| Undo the last change | `cp config.yaml.bak config.yaml && docker compose up -d --force-recreate dashboard` |

## If a card shows UNREACHABLE
The red text on the card says why:

| It says | It means | Fix |
|---|---|---|
| `401 Authentication failed` | The cluster was reached, but the token or password is wrong | `set-secret`; if the token id was different, edit `token_id`; if you lost the secret, rotate it (`--rotate` on the node) then `set-secret` |
| `403` | Logged in, but not allowed to read it | Re-run the node script (it re-applies the read-only role) |
| timeout / `ConnectError` | The VM can't reach that address | A firewall or routing problem (NetBird route, site firewall). Test from the VM: `curl -sk -o /dev/null -w "%{http_code}\n" https://<address>:8006` |
| certificate error | TLS verification is on for a self-signed cluster | Leave `verify_tls: false` (the default) |

## Good to know
- The dashboard reads its config when it **starts**. `--apply` handles that. If you ever edit by hand, run
  `docker compose up -d --force-recreate dashboard` afterwards (a plain restart doesn't re-read `.env`).
- Cluster names must be unique and can't start with `-`.
- A bad `config.yaml` stops the dashboard from starting. `check` finds the line; `cp config.yaml.bak config.yaml`
  puts back the last good copy.
- Never paste secrets into chat, tickets or shell history. The helper asks for them with hidden input.
