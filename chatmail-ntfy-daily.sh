#!/bin/sh
# chatmail-ntfy-daily — daily chatmail relay summary to an ntfy topic.
#
# Config: /etc/chatmail-ntfy.env
#   NTFY_TOPIC=https://ntfy.example.org/chatmail-status   (required)
#   NTFY_TOKEN=tk_...          # optional, for protected topics
#   NTFY_TITLE=my.relay        # optional, default: mail_domain from chatmail.ini
#   DISK_WARN=85               # optional, disk % that raises priority
#
# cron: 55 23 * * * root /usr/local/bin/chatmail-ntfy-daily
set -eu

ENV_FILE=${CHATMAIL_NTFY_ENV:-/etc/chatmail-ntfy.env}
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
: "${NTFY_TOPIC:?set NTFY_TOPIC in $ENV_FILE}"
DISK_WARN=${DISK_WARN:-85}
STATE=${STATE:-/var/lib/chatmail-stats/last_total}
INI=/usr/local/lib/chatmaild/chatmail.ini
[ -n "${NTFY_TITLE:-}" ] || NTFY_TITLE=$(sed -n 's/^mail_domain *= *//p' "$INI" 2>/dev/null | head -1)
[ -n "$NTFY_TITLE" ] || NTFY_TITLE=$(hostname -f)

read TOTAL ACTIVE NEW24 IMAP <<EOF
$(/usr/local/bin/chatmail-stats --json | python3 -c '
import json, sys
d = json.load(sys.stdin); a = d["accounts"]
print(a["total"], a["active"]["today"], (a.get("new") or {}).get("24h", "n/a"),
      d["imap"].get("connections", "n/a"))')
EOF
DISK=$(df --output=pcent /home/vmail | tail -1 | tr -dc '0-9')

PREV=$(cat "$STATE" 2>/dev/null || true)
DELTA=""
[ -n "$PREV" ] && DELTA=" ($(printf '%+d' $((TOTAL - PREV))))"

PRIO=default; TAGS=bar_chart
[ "$DISK" -ge "$DISK_WARN" ] && { PRIO=high; TAGS=warning; }

set -- -H "Title: $NTFY_TITLE" -H "Tags: $TAGS" -H "Priority: $PRIO"
[ -n "${NTFY_TOKEN:-}" ] && set -- "$@" -H "Authorization: Bearer $NTFY_TOKEN"

curl -fsS -m 15 -o /dev/null "$@" \
    -d "Accounts: $TOTAL$DELTA
Active today: $ACTIVE
New 24h: $NEW24
IMAP now: $IMAP
Disk: $DISK%" "$NTFY_TOPIC"

mkdir -p "$(dirname "$STATE")"
echo "$TOTAL" > "$STATE"
