#!/bin/bash
# Install the AIDAS storage scan. Run on aidas, in a clone of the repo:  sudo bash aidas/install.sh
#   /usr/local/sbin/aidas-storage-scan              read-only scanner (root:root 755)
#   /etc/cron.d/aidas-storage                       runs it hourly at :05
#   /mnt/TrueNAS/yejoon/aidas-storage/latest.json   its output (totals only)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo bash $0" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
mountpoint -q /mnt/TrueNAS || { echo "/mnt/TrueNAS is not mounted; mount it first." >&2; exit 1; }
[ -d /mnt/TrueNAS/yejoon ] || { echo "/mnt/TrueNAS/yejoon does not exist." >&2; exit 1; }

install -o root -g root -m 0755 "$HERE/aidas-storage-scan" /usr/local/sbin/aidas-storage-scan
install -o root -g root -m 0644 "$HERE/aidas-storage.cron" /etc/cron.d/aidas-storage
echo "Installed the scanner and its cron entry."

# First scan now (in the background, survives logout); later scans run hourly at :05.
systemd-run --quiet --unit "aidas-storage-firstscan-$(date +%s)" --nice=19 \
  /bin/sh -c 'flock -n /run/aidas-storage-scan.lock timeout -k 2m 50m /usr/local/sbin/aidas-storage-scan >>/var/log/aidas-storage-scan.log 2>&1'
echo "First scan started in the background (about a minute). Check: tail /var/log/aidas-storage-scan.log"
