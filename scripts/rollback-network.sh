#!/usr/bin/env bash
set -euo pipefail
backup=${1:?Supply the backup directory printed by core-network.sh --install}
/usr/local/lib/core-http-bridge/core-network.sh --remove
if [ -f "$backup/core-bridge-netfilter.service" ]; then
  cp -p "$backup/core-bridge-netfilter.service" /etc/systemd/system/core-bridge-netfilter.service
else
  systemctl disable core-bridge-netfilter.service
  rm /etc/systemd/system/core-bridge-netfilter.service
fi
systemctl daemon-reload
# Do not restore the entire iptables snapshot: unrelated rules may have changed.
