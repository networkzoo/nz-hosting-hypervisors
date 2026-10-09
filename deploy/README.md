# Deploying the dashboard VM (Proxmox)

## Prereqs on the Proxmox host
- Datacenter > Storage > (your `local`) > Content: tick **Snippets**.
- A root SSH public key (or point `SSH_KEY_FILE` at the key you'll use to log in).

## Create the VM
```
IP_CIDR=10.10.0.239/24 GATEWAY=10.10.0.254 ADMIN_CIDR=10.10.0.0/24 VLAN=1010 BRIDGE=vmbr1 \
  ./deploy/proxmox-create-vm.sh
```
Defaults: bridge vmbr1, VMID 9100, 2 vCPU, 2 GB RAM, 20 GB disk, auto-start on boot, UEFI. Override with env vars (see script header).
The VM boots, installs Docker/ufw/etc., reboots once, then is ready. Login: `ssh ops@<ip>` (key only).

Installed by hand from the ISO instead? Run `deploy/bootstrap.sh` as root in the VM.

## Deploy the app
```
sudo -u ops git clone <repo-url> /opt/dashboard   # private repo: use a read-only deploy key
cd /opt/dashboard && cp config.example.yaml config.yaml && cp .env.example .env
# edit both, then:
docker compose up -d --build
```
The app listens on 127.0.0.1:8080 only; the firewall opens 443 for the reverse proxy (next step).
