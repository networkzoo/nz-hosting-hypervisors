#!/usr/bin/env bash
# Run on the Proxmox host as root. Builds an Ubuntu 24.04 cloud-init VM for the dashboard.
# Usage: edit the variables below (or export them), then ./proxmox-create-vm.sh
set -euo pipefail

VMID=${VMID:-9100}
NAME=${NAME:-nz-dashboard}
STORAGE=${STORAGE:-local-lvm}          # where the VM disk lives
SNIPPETS=${SNIPPETS:-local}            # storage with 'Snippets' content enabled
BRIDGE=${BRIDGE:-vmbr1}
VLAN=${VLAN:-}                         # e.g. 20, or empty for untagged
IP_CIDR=${IP_CIDR:-}                   # e.g. 10.0.20.50/24  (required)
GATEWAY=${GATEWAY:-}                   # e.g. 10.0.20.1      (required)
DNS=${DNS:-1.1.1.1}
ADMIN_CIDR=${ADMIN_CIDR:-}             # who may SSH in; comma-separated, e.g. 10.10.0.0/24,172.16.124.0/24 (required)
SSH_KEY_FILE=${SSH_KEY_FILE:-/root/.ssh/id_ed25519.pub}
DISK_GB=${DISK_GB:-20}
MEM_MB=${MEM_MB:-2048}
CORES=${CORES:-2}
IMG_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

for v in IP_CIDR GATEWAY ADMIN_CIDR; do [[ -n "${!v}" ]] || { echo "Set $v" >&2; exit 1; }; done
[[ -f $SSH_KEY_FILE ]] || { echo "No SSH public key at $SSH_KEY_FILE" >&2; exit 1; }
qm status "$VMID" &>/dev/null && { echo "VMID $VMID already exists" >&2; exit 1; }

# Bridge must exist, and be VLAN-aware if we're tagging.
ip link show "$BRIDGE" &>/dev/null || { echo "Bridge '$BRIDGE' not found on this host" >&2; exit 1; }
if [[ -n $VLAN ]]; then
  [[ $(cat "/sys/class/net/$BRIDGE/bridge/vlan_filtering" 2>/dev/null) == 1 ]] \
    || { echo "Bridge '$BRIDGE' is not VLAN-aware; tag $VLAN would not work (tick 'VLAN aware' on it)" >&2; exit 1; }
fi
echo ">> Network: bridge=$BRIDGE vlan=${VLAN:-none} ip=$IP_CIDR gw=$GATEWAY"

SNIP_DIR=$(pvesm path "$SNIPPETS:snippets/x" | xargs dirname)
[[ -d $SNIP_DIR ]] || { echo "Storage '$SNIPPETS' has no snippets dir; enable 'Snippets' under Datacenter > Storage" >&2; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
echo ">> Downloading Ubuntu 24.04 cloud image"
curl -fL -o "$WORK/noble.img" "$IMG_URL"

echo ">> Rendering cloud-init user-data"
HERE=$(cd "$(dirname "$0")" && pwd)
# One ufw rule per allowed subnet (comma-separated ADMIN_CIDR).
RULES=""
IFS=',' read -ra CIDRS <<< "$ADMIN_CIDR"
for c in "${CIDRS[@]}"; do
  c=${c// /}
  [[ $c =~ ^[0-9.]+/[0-9]+$ ]] || { echo "Bad ADMIN_CIDR entry '$c' (want e.g. 10.10.0.0/24)" >&2; exit 1; }
  RULES+="  - ufw allow from $c to any port 22 proto tcp"$'\n'
done
RULES=${RULES%$'\n'}
sed -e "s|__HOSTNAME__|$NAME|g" -e "s|__SSH_KEY__|$(cat "$SSH_KEY_FILE")|g" \
    "$HERE/cloud-init-user-data.yaml" \
  | awk -v rules="$RULES" '$0=="__UFW_SSH_RULES__"{print rules; next} {print}' \
  > "$SNIP_DIR/$NAME-user.yaml"

echo ">> Creating VM $VMID ($NAME)"
NET="virtio,bridge=$BRIDGE${VLAN:+,tag=$VLAN},firewall=1"
qm create "$VMID" --name "$NAME" --ostype l26 --machine q35 --bios ovmf --efidisk0 "$STORAGE:0,efitype=4m" \
  --cpu host --cores "$CORES" --memory "$MEM_MB" --net0 "$NET" \
  --scsihw virtio-scsi-single --agent enabled=1 --onboot 1 --serial0 socket --vga serial0
qm importdisk "$VMID" "$WORK/noble.img" "$STORAGE" >/dev/null
DISK=$(qm config "$VMID" | awk '/^unused0:/{print $2}')   # works on lvm-thin, zfs, dir, ...
[[ -n $DISK ]] || { echo "Disk import failed (no unused0)" >&2; exit 1; }
qm set "$VMID" --scsi0 "$DISK,discard=on,ssd=1" --boot order=scsi0
qm resize "$VMID" scsi0 "${DISK_GB}G"
qm set "$VMID" --ide2 "$STORAGE:cloudinit" \
  --ipconfig0 "ip=$IP_CIDR,gw=$GATEWAY" --nameserver "$DNS" \
  --cicustom "user=$SNIPPETS:snippets/$NAME-user.yaml"

qm start "$VMID"
echo ">> Started. First boot installs packages and reboots once (~3-5 min)."
echo "   Then: ssh ops@${IP_CIDR%/*}   and check: cat /var/log/dashboard-provisioned"
