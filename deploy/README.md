# Deploying the dashboard VM (Proxmox)

## Prereqs on the Proxmox host
- Datacenter > Storage > (your `local`) > Content: tick **Snippets**.
- A root SSH public key (or point `SSH_KEY_FILE` at the key you'll use to log in).

## Create the VM
```
IP_CIDR=10.10.0.239/24 GATEWAY=10.10.0.254 ADMIN_CIDR=10.10.0.0/24,172.16.124.0/24 VLAN=1010 BRIDGE=vmbr1 \
  ./deploy/proxmox-create-vm.sh
```
Defaults: bridge vmbr1, VMID 9100, 2 vCPU, 2 GB RAM, 20 GB disk, auto-start on boot, UEFI. Override with env vars (see script header).
The VM boots, installs Docker/ufw/etc., reboots once, then is ready. Login: `ssh ops@<ip>` (key only).

Installed by hand from the ISO instead? Run `deploy/bootstrap.sh` as root in the VM.

## Deploy the app (on the VM)
```
ssh ops@<ip>
ssh-keygen -t ed25519 -f ~/.ssh/deploy -N ""      # then add ~/.ssh/deploy.pub as a read-only Deploy key
                                                  # (GitHub repo > Settings > Deploy keys)
GIT_SSH_COMMAND="ssh -i ~/.ssh/deploy" git clone -b <branch> git@github.com:networkzoo/nz-hosting-hypervisors.git /opt/dashboard
cd /opt/dashboard
cp config.example.yaml config.yaml && cp .env.example .env    # edit both
deploy/set-password.sh admin                                  # dashboard login (do this BEFORE docker compose up)
docker compose up -d --build
```
Set `DASH_HOSTS` in `.env` to every address you browse to (comma-separated), then `docker compose up -d`. Browse to `https://<ip>` (accept the self-signed cert warning). Only port 443 is exposed, behind the login.

## Adding Proxmox clusters
On **one node of each cluster**, as root: `deploy/pve-create-monitor.sh "<cluster name>"`.
It creates a read-only `monitor@pve` user and a `dashboard` API token, tests it, and prints the
`.env` and `config.yaml` lines to paste. Re-run with `--rotate` to replace a token.

## Adding clusters to the dashboard (no hand-editing YAML)
On the VM, from `/opt/dashboard`:
```
python3 deploy/cluster_config.py add proxmox "Aviary" https://172.16.124.30:8006 --apply
python3 deploy/cluster_config.py add scale   "CDAC"   https://10.4.33.10 --user monitor --apply
python3 deploy/cluster_config.py check      # validates config.yaml and that every secret is in .env
python3 deploy/cluster_config.py list
```
It prompts for the secret (hidden), writes it to `.env`, appends a correctly indented entry, keeps a
`config.yaml.bak`, and with `--apply` recreates the dashboard container. Defaults: Proxmox token id
`monitor@pve!dashboard`, TLS verification off (`--verify-tls` to turn on).

### Pending-updates counts (optional, Proxmox)
`deploy/pve-create-monitor.sh "<name>" --with-updates` also grants `Sys.Modify` on `/nodes` (a separate
`DashboardUpdates` role) so the dashboard can show per-node pending updates. That is more than read-only,
so it is opt-in per cluster. Re-running it on an existing cluster adds the grant without touching the token.
Revoke with `pveum acl delete /nodes --users monitor@pve --roles DashboardUpdates`.
