#!/usr/bin/env bash
# Run on the Proxmox host as root. Builds an Ubuntu 24.04 cloud-init VM for the dashboard.
# Usage: edit the variables below (or export them), then ./proxmox-create-vm.sh
set -euo pipefail

VMID=${VMID:-9100}
NAME=${NAME:-nz-dashboard}
STORAGE=${STORAGE:-local-lvm}          # where the VM disk lives
SNIPPETS=${SNIPPETS:-local}            # storage with 'Snippets' content enabled
BRIDGE=${BRIDGE:-vmbr0}
VLAN=${VLAN:-}                         # e.g. 20, or empty for untagged
IP_CIDR=${IP_CIDR:-}                   # e.g. 10.0.20.50/24  (required)
GATEWAY=${GATEWAY:-}                   # e.g. 10.0.20.1      (required)
DNS=${DNS:-1.1.1.1}
ADMIN_CIDR=${ADMIN_CIDR:-}             # who may SSH in, e.g. 10.0.10.0/24 (required)
SSH_KEY_FILE=${SSH_KEY_FILE:-/root/.ssh/id_ed25519.pub}
DISK_GB=${DISK_GB:-20}
MEM_MB=${MEM_MB:-2048}
CORES=${CORES:-2}
IMG_URL=https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

for v in IP_CIDR GATEWAY ADMIN_CIDR; do [[ -n "${!v}" ]] || { echo "Set $v" >&2; exit 1; }; done
[[ -f $SSH_KEY_FILE ]] || { echo "No SSH public key at $SSH_KEY_FILE" >&2; exit 1; }
qm status "$VMID" &>/dev/null && { echo "VMID $VMID already exists" >&2; exit 1; }

SNIP_DIR=$(pvesm path "$SNIPPETS:snippets/x" | xargs dirname)
[[ -d $SNIP_DIR ]] || { echo "Storage '$SNIPPETS' has no snippets dir; enable 'Snippets' under Datacenter > Storage" >&2; exit 1; }

WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
echo ">> Downloading Ubuntu 24.04 cloud image"
curl -fL -o "$WORK/noble.img" "$IMG_URL"

echo ">> Rendering cloud-init user-data"
HERE=$(cd "$(dirname "$0")" && pwd)
sed -e "s|__HOSTNAME__|$NAME|g" \
    -e "s|__SSH_KEY__|$(cat "$SSH_KEY_FILE")|g" \
    -e "s|__ADMIN_CIDR__|$ADMIN_CIDR|g" \
    "$HERE/cloud-init-user-data.yaml" > "$SNIP_DIR/$NAME-user.yaml"

echo ">> Creating VM $VMID ($NAME)"
NET="virtio,bridge=$BRIDGE${VLAN:+,tag=$VLAN},firewall=1"
qm create "$VMID" --name "$NAME" --ostype l26 --machine q35 --bios ovmf --efidisk0 "$STORAGE:0,efitype=4m" \
  --cpu host --cores "$CORES" --memory "$MEM_MB" --net0 "$NET" \
  --scsihw virtio-scsi-single --agent enabled=1 --onboot 1 --serial0 socket --vga serial0
qm importdisk "$VMID" "$WORK/noble.img" "$STORAGE" >/dev/null
qm set "$VMID" --scsi0 "$STORAGE:vm-$VMID-disk-1,discard=on,ssd=1" --boot order=scsi0
qm resize "$VMID" scsi0 "${DISK_GB}G"
qm set "$VMID" --ide2 "$STORAGE:cloudinit" \
  --ipconfig0 "ip=$IP_CIDR,gw=$GATEWAY" --nameserver "$DNS" \
  --cicustom "user=$SNIPPETS:snippets/$NAME-user.yaml"

qm start "$VMID"
echo ">> Started. First boot installs packages and reboots once (~3-5 min)."
echo "   Then: ssh ops@${IP_CIDR%/*}   and check: cat /var/log/dashboard-provisioned"
