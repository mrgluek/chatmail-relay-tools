# chatmail-relay-tools

Small, dependency-free admin scripts for [chatmail relays](https://github.com/chatmail/relay):
count accounts and live IMAP load, send the numbers to ntfy or Uptime Kuma,
and mirror the webxdc App Picker for users who cannot reach the upstream.

Everything runs on the relay itself as root and uses only what a chatmail relay
already has (python3, doveadm, coreutils, curl). Nothing here is touched by
`cmdeploy run`.

| Script | What it does |
|---|---|
| `chatmail-stats.py` | Accounts (total, active today / 7d / 30d / 90d, new 24h / 7d), IMAP sessions from Dovecot, imap processes, TCP on 993/143/443, load. Human, JSON or CSV output. |
| `chatmail-ntfy-daily.sh` | Once-a-day summary to an [ntfy](https://ntfy.sh) topic, with the change in account count since the previous run. |
| `chatmail-kuma-push.sh` | Sends numbers to [Uptime Kuma](https://github.com/louislam/uptime-kuma) Push monitors so Kuma graphs them. |
| `chatmail-apps-mirror.py` | Local copy of the webxdc App Picker (apps list, `.xdc` files, store page) at `https://<relay>/apps/`, refreshed by a systemd timer. |

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
install -m 755 chatmail-apps-mirror.py /usr/local/bin/chatmail-apps-mirror # optional
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

## webxdc App Picker mirror

Delta Chat picks mini apps from two upstream sites: the
[xdcget export](https://apps.testrun.org/) (`xdcget-lock.json`, `.xdc` files,
icons) used by Desktop, and the [store page](https://webxdc.org/apps/) opened
on Android. Where those are blocked, `chatmail-apps-mirror` keeps a copy of
both on the relay. Their file names do not overlap, so one URL serves every
client:

```
https://<relay>/apps/
```

Set it as the webxdc store URL in the client's experimental settings
(Desktop: Settings → Advanced → Experimental features), or just open it in a
browser. Desktop reads `xdcget-lock.json` from it; Android/iOS open it as the
store page.

What the copy changes:

- `main.js` loads the list and icons from `/apps` instead of testrun.org.
- In `xdcget-lock.json` every app's `url` points at our copy of the `.xdc`.
  Desktop with a custom store URL downloads from `url`, which upstream is the
  developer's GitHub/Codeberg release.
- Icons larger than 192 px are downscaled to 192 px in the same format (the
  store shows them at 64 CSS px, so this covers 3x screens): about 2.4 MB of
  icons become 1.2 MB. Needs `python3-pil`; without it icons are served as is.
  Names and the lock are unchanged.
- Desktop still loads **icons** from apps.testrun.org (hardcoded in the
  client); without access there the list shows no icons, but apps install.

How it is stored: each run builds a full release in
`/var/lib/chatmail-apps-mirror/<time>/`, then atomically switches the symlink
`/var/www/html/apps` to it. Unchanged files are hard-linked from the previous
release: `.xdc` files by name (the version is part of it, so they are not even
requested), icons and store files by content. apps.testrun.org re-dates all
its files on every export, so dates cannot tell what changed; comparing
content keeps our `Last-Modified`/`ETag` stable and lets browsers reuse cached
icons. A failed run leaves the previous copy in place;
the previous release is kept for rollback. About 120 MB.

The relay's nginx already serves `/var/www/html` and `cmdeploy run` copies its
web pages there without `--delete`, so no nginx change is needed and the
mirror survives redeploys.

```sh
apt install -y python3-pil   # optional, for smaller icons
install -m 755 chatmail-apps-mirror.py /usr/local/bin/chatmail-apps-mirror
install -m 644 systemd/chatmail-apps-mirror.* /etc/systemd/system/
systemctl daemon-reload
systemctl start chatmail-apps-mirror          # first sync, ~2 min
systemctl enable --now chatmail-apps-mirror.timer
journalctl -u chatmail-apps-mirror -n 5       # "167 apps, downloaded ..."
```

The public URL defaults to `https://<mail_domain>/apps/` from `chatmail.ini`;
use `--public-url` / `--dest` for another place.

## Privacy

Only counts are produced. Addresses appear only in the interactive `--top`
view on the server; they are never written to CSV, ntfy or Kuma.

## License

MIT, see [LICENSE](LICENSE).
