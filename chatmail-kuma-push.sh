#!/bin/sh
# chatmail-kuma-push — send chatmail-stats numbers to Uptime Kuma Push monitors.
# Each metric goes to its own Push monitor; the number is sent as "ping",
# so Kuma draws it on the response-time chart (the "ms" label is cosmetic).
#
# Put tokens (the last part of each Push URL) into /etc/chatmail-kuma.env:
#   KUMA_URL=https://kuma.example.com
#   TOKEN_ACCOUNTS=xxxxxxxx
#   TOKEN_IMAP=yyyyyyyy
#   TOKEN_NEW24H=zzzzzzzz
#   TOKEN_DISK=wwwwwwww
#   DISK_WARN=85            # disk % at which the disk monitor goes DOWN
# Any TOKEN_* left empty is simply skipped.
#
# cron: */5 * * * * root /usr/local/bin/chatmail-kuma-push

set -eu
. /etc/chatmail-kuma.env
DISK_WARN=${DISK_WARN:-85}

read ACCOUNTS ACTIVE IMAP IMAPUSERS NEW24 <<EOF
$(/usr/local/bin/chatmail-stats --json | python3 -c '
import json, sys
d = json.load(sys.stdin); a = d["accounts"]; i = d["imap"]
print(a["total"], a["active"]["today"], i.get("connections", 0), i.get("users", 0),
      (a.get("new") or {}).get("24h", 0))')
EOF
DISK=$(df --output=pcent /home/vmail | tail -1 | tr -dc '0-9')

push() {  # push TOKEN VALUE MESSAGE [STATUS]
    [ -n "$1" ] || return 0
    msg=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$3")
    curl -fsS -m 10 -o /dev/null \
        "$KUMA_URL/api/push/$1?status=${4:-up}&msg=$msg&ping=$2" || true
}

push "${TOKEN_ACCOUNTS:-}" "$ACCOUNTS" "accounts $ACCOUNTS, active today $ACTIVE"
push "${TOKEN_IMAP:-}"     "$IMAP"     "imap $IMAP conns / $IMAPUSERS accounts"
push "${TOKEN_NEW24H:-}"   "$NEW24"    "new accounts 24h: $NEW24"
if [ "$DISK" -ge "$DISK_WARN" ]; then st=down; else st=up; fi
push "${TOKEN_DISK:-}"     "$DISK"     "disk ${DISK}% used" "$st"
