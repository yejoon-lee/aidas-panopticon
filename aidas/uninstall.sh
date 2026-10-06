#!/bin/bash
# Remove everything install.sh added. Run on aidas, in a clone of the repo:  sudo bash aidas/uninstall.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
rm -f /etc/cron.d/aidas-storage /usr/local/sbin/aidas-storage-scan /var/log/aidas-storage-scan.log
if mountpoint -q /mnt/TrueNAS; then
  rm -rf /mnt/TrueNAS/yejoon/aidas-storage || echo "Could not remove /mnt/TrueNAS/yejoon/aidas-storage; delete it as yejoon."
fi
echo "Removed the AIDAS storage scan."
