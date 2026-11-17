#!/usr/bin/env bash
set -euo pipefail
mode=${1:---check}
comment=core-http-bridge-managed
rules() {
  local binary=$1 action=$2
  for incoming in 'p.+' 'b.+'; do
    for outgoing in 'p.+' 'b.+'; do
      case "$action" in
        ensure) "$binary" -w -C FORWARD -i "$incoming" -o "$outgoing" -m comment --comment "$comment" -j ACCEPT 2>/dev/null || "$binary" -w -I FORWARD 1 -i "$incoming" -o "$outgoing" -m comment --comment "$comment" -j ACCEPT ;;
        remove) while "$binary" -w -C FORWARD -i "$incoming" -o "$outgoing" -m comment --comment "$comment" -j ACCEPT 2>/dev/null; do "$binary" -w -D FORWARD -i "$incoming" -o "$outgoing" -m comment --comment "$comment" -j ACCEPT; done ;;
        check) "$binary" -w -C FORWARD -i "$incoming" -o "$outgoing" -m comment --comment "$comment" -j ACCEPT ;;
      esac
    done
  done
}
case "$mode" in
  --ensure) rules iptables ensure; if command -v ip6tables >/dev/null; then rules ip6tables ensure; fi ;;
  --remove) rules iptables remove; if command -v ip6tables >/dev/null; then rules ip6tables remove; fi ;;
  --check) rules iptables check; if command -v ip6tables >/dev/null; then rules ip6tables check; fi ;;
  --install)
    backup=/var/backups/core-http-bridge/network-$(date +%Y%m%d-%H%M%S)
    mkdir -p "$backup" /usr/local/lib/core-http-bridge
    iptables-save > "$backup/iptables.rules"
    if command -v ip6tables-save >/dev/null; then ip6tables-save > "$backup/ip6tables.rules"; fi
    if [ -f /etc/systemd/system/core-bridge-netfilter.service ]; then cp -p /etc/systemd/system/core-bridge-netfilter.service "$backup/core-bridge-netfilter.service"; fi
    install -m 755 "$0" /usr/local/lib/core-http-bridge/core-network.sh
    cat > /etc/systemd/system/core-bridge-netfilter.service <<'UNIT'
[Unit]
Description=CORE bridge forwarding for point-to-point and switch networks
After=network-online.target docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/lib/core-http-bridge/core-network.sh --ensure
ExecStop=/usr/local/lib/core-http-bridge/core-network.sh --remove
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable core-bridge-netfilter.service
    /usr/local/lib/core-http-bridge/core-network.sh --ensure
    printf '%s\n' "$backup" ;;
  *) echo 'usage: core-network.sh --check|--ensure|--remove|--install' >&2; exit 2 ;;
esac
