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
