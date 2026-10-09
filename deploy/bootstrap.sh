#!/usr/bin/env bash
# For a VM installed by hand from the Ubuntu ISO: applies the same setup as the cloud-init file.
# Run as root:  ADMIN_CIDR=10.0.10.0/24 ./bootstrap.sh
set -euo pipefail
: "${ADMIN_CIDR:?set ADMIN_CIDR, e.g. 10.0.10.0/24}"
export DEBIAN_FRONTEND=noninteractive
apt-get update && apt-get -y upgrade
apt-get -y install qemu-guest-agent docker.io docker-compose-v2 git ufw unattended-upgrades chrony
cat > /etc/apt/apt.conf.d/52dashboard-unattended <<'CONF'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "04:00";
CONF
systemctl enable --now qemu-guest-agent docker chrony
ufw default deny incoming; ufw default allow outgoing
IFS=, read -ra CIDRS <<< "$ADMIN_CIDR"
for c in "${CIDRS[@]}"; do ufw allow from "${c// /}" to any port 22 proto tcp; done
ufw allow 443/tcp
ufw --force enable
install -d /opt/dashboard
echo "Done. Deploy the app into /opt/dashboard (see deploy/README.md)."
