# chatmail-relay-tools

Small, dependency-free admin scripts for [chatmail relays](https://github.com/chatmail/relay):
count accounts and live IMAP load, and send the numbers to ntfy or Uptime Kuma.

Everything runs on the relay itself as root and uses only what a chatmail relay
already has (python3, doveadm, coreutils, curl). Nothing here is touched by
`cmdeploy run`.

| Script | What it does |
|---|---|
| `chatmail-stats.py` | Accounts (total, active today / 7d / 30d / 90d, new 24h / 7d), IMAP sessions from Dovecot, imap processes, TCP on 993/143/443, load. Human, JSON or CSV output. |
| `chatmail-ntfy-daily.sh` | Once-a-day summary to an [ntfy](https://ntfy.sh) topic, with the change in account count since the previous run. |
| `chatmail-kuma-push.sh` | Sends numbers to [Uptime Kuma](https://github.com/louislam/uptime-kuma) Push monitors so Kuma graphs them. |

## How the numbers are counted

- **Accounts**: every `<mailboxes_dir>/<addr>/password` is one account.
  `mailboxes_dir` is read from `/usr/local/lib/chatmaild/chatmail.ini`
  (default `/home/vmail/mail/<mail_domain>`).
- **Active**: chatmaild sets the mtime of the `password` file to the UTC day of
  the last login, so activity has one-day granularity. Nothing else is tracked.
- **New accounts**: creation (birth) time of the account directory. Shows `n/a`
  if the filesystem/kernel does not expose it.
- **IMAP**: `doveadm who`, so clients connecting through 443 (ALPN) are counted
  too, not only port 993. Idle connections hibernated by Dovecot
  (`imap_hibernate_timeout`) are counted as sessions but do not have an
  `imap` process.

## Install

```sh
git clone https://github.com/mrgluek/chatmail-relay-tools
cd chatmail-relay-tools
install -m 755 chatmail-stats.py      /usr/local/bin/chatmail-stats
install -m 755 chatmail-ntfy-daily.sh /usr/local/bin/chatmail-ntfy-daily   # optional
install -m 755 chatmail-kuma-push.sh  /usr/local/bin/chatmail-kuma-push    # optional
```

The ntfy and Kuma scripts call `/usr/local/bin/chatmail-stats`.

## chatmail-stats

```sh
chatmail-stats                 # human-readable report
chatmail-stats --top 10        # + accounts with most IMAP sessions
chatmail-stats --json          # for bots / monitoring
chatmail-stats --csv FILE      # append one line (no addresses) — for cron
```

Example:

```
Accounts
  total                1164
  logged in today(UTC) 682
  active 7d / 30d / 90d 1081 / 1164 / 1164
  no login recorded    6
  new 24h / 7d         261 / 888

IMAP (dovecot anvil)
  connections          240
  distinct accounts    187
  distinct IPs         127
  conns per account    1.28
  imap processes       70  (570.8 MiB RSS)
```

RSS is summed per process and includes shared pages, so it overstates real
memory use; check `free -m` for the true picture.

Keep a history for trends:

```sh
echo '*/5 * * * * root /usr/local/bin/chatmail-stats --csv /var/log/chatmail-stats.csv' \
  > /etc/cron.d/chatmail-stats
```

## Daily summary to ntfy

```sh
cat > /etc/chatmail-ntfy.env <<'EOF'
NTFY_TOPIC=https://ntfy.example.org/chatmail-status
# NTFY_TOKEN=tk_...     # if the topic is protected
# NTFY_TITLE=my.relay   # default: mail_domain from chatmail.ini
# DISK_WARN=85          # disk % that raises the priority
EOF
chmod 600 /etc/chatmail-ntfy.env
chatmail-ntfy-daily
echo '55 23 * * * root /usr/local/bin/chatmail-ntfy-daily' > /etc/cron.d/chatmail-ntfy
```

Message:

```
Accounts: 1164 (+64)
Active today: 682
New 24h: 261
IMAP now: 240
Disk: 49%
```

The number in brackets is the net change since the previous run: new
registrations minus accounts removed by `delete_inactive_users_after`.
The first run has nothing to compare with and shows no change.

## Uptime Kuma push monitors

Create one monitor of type **Push** per metric in Kuma and copy the token
(the part after `/api/push/`). Set the heartbeat interval a bit above the cron
interval (e.g. 600 s for a 5-minute cron).

```sh
cat > /etc/chatmail-kuma.env <<'EOF'
KUMA_URL=https://kuma.example.org
TOKEN_ACCOUNTS=...
TOKEN_IMAP=...
TOKEN_NEW24H=...
TOKEN_DISK=...
DISK_WARN=85
EOF
chmod 600 /etc/chatmail-kuma.env
echo '*/5 * * * * root /usr/local/bin/chatmail-kuma-push' > /etc/cron.d/chatmail-kuma
```

Each value is sent as `ping`, so Kuma draws it on the response-time chart
(ignore the "ms" label). Empty tokens are skipped. The disk monitor goes DOWN
when usage reaches `DISK_WARN`.

## Privacy

Only counts are produced. Addresses appear only in the interactive `--top`
view on the server; they are never written to CSV, ntfy or Kuma.

## License

MIT, see [LICENSE](LICENSE).
