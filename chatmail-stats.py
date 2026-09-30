#!/usr/bin/env python3
"""chatmail-stats — accounts and live IMAP load on a chatmail relay.

Accounts:  every <mailboxes_dir>/<addr>/password file is one account.
           chatmaild sets the password file's mtime to the UTC day of the
           last login, so "active" below has one-day granularity.
IMAP:      taken from `doveadm who` (Dovecot anvil), so it also counts
           clients that reach IMAP via 443/ALPN, not only port 993.

Usage:
  chatmail-stats                  human-readable report
  chatmail-stats --top 10         also show the 10 accounts with most IMAP sessions
  chatmail-stats --json           JSON (for bots / monitoring)
  chatmail-stats --csv FILE       append one line to FILE (for cron, no addresses logged)

Must run as root (reads /home/vmail and talks to doveadm).
"""

import argparse
import configparser
import csv
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

VERSION = "1.0"
DEFAULT_INI = "/usr/local/lib/chatmaild/chatmail.ini"
DAY = 86400


# ---------------------------------------------------------------- config

def read_ini(path):
    cp = configparser.ConfigParser(interpolation=None)
    if not cp.read(path):
        return {}
    sect = "params" if cp.has_section("params") else (cp.sections() or [None])[0]
    return dict(cp[sect]) if sect else {}


def resolve_mailboxes_dir(args):
    if args.mailboxes_dir:
        return Path(args.mailboxes_dir)
    params = read_ini(args.ini)
    if params.get("mailboxes_dir"):
        return Path(params["mailboxes_dir"].strip())
    domain = (params.get("mail_domain") or "").strip()
    if domain:
        return Path("/home/vmail/mail") / domain
    # last resort: a single domain dir under /home/vmail/mail
    base = Path("/home/vmail/mail")
    dirs = [d for d in base.iterdir() if d.is_dir()] if base.is_dir() else []
    if len(dirs) == 1:
        return dirs[0]
    sys.exit(f"cannot find mailboxes_dir (tried {args.ini}); pass --mailboxes-dir")


# ---------------------------------------------------------------- accounts

def birth_times(paths, chunk=2000):
    """Directory creation times via coreutils `stat -c %W` (statx), batched.
    Returns list of ints; 0 where the FS/kernel doesn't expose birth time."""
    out = []
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        try:
            p = subprocess.run(["stat", "-c", "%W", "--", *part],
                               capture_output=True, text=True, timeout=60)
            vals = p.stdout.split()
        except (OSError, subprocess.SubprocessError):
            vals = []
        for j in range(len(part)):
            try:
                out.append(max(int(vals[j]), 0))
            except (IndexError, ValueError):
                out.append(0)
    return out


def account_stats(mbdir, now, with_birth):
    today = now // DAY * DAY
    total = 0
    active = {"today": 0, "7d": 0, "30d": 0, "90d": 0}
    never_tracked = 0
    dirs = []

    with os.scandir(mbdir) as it:
        for e in it:
            if "@" not in e.name or not e.is_dir(follow_symlinks=False):
                continue
            try:
                mtime = int(os.stat(os.path.join(e.path, "password")).st_mtime)
            except FileNotFoundError:
                continue
            total += 1
            if mtime >= today:
                active["today"] += 1
            if mtime >= today - 6 * DAY:
                active["7d"] += 1
            if mtime >= today - 29 * DAY:
                active["30d"] += 1
            if mtime >= today - 89 * DAY:
                active["90d"] += 1
            if mtime % DAY:  # never rounded by lastlogin => no login recorded yet
                never_tracked += 1
            if with_birth:
                dirs.append(e.path)

    res = {"total": total, "active": active, "no_login_recorded": never_tracked}
    if with_birth:
        bts = [b for b in birth_times(dirs) if b]
        res["new"] = {"24h": sum(b >= now - DAY for b in bts),
                      "7d": sum(b >= now - 7 * DAY for b in bts)} if bts else None
    return res


# ---------------------------------------------------------------- IMAP

def run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def parse_who_tab(text):
    """Parse `doveadm -f tab who -1` → list of dicts keyed by header names."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    header = [h.strip().lower() for h in lines[0].split("\t")]
    rows = []
    for l in lines[1:]:
        parts = l.split("\t")
        rows.append({header[i]: parts[i].strip() for i in range(min(len(header), len(parts)))})
    return rows


def imap_sessions():
    """Returns dict with per-connection data from Dovecot anvil, or None."""
    try:
        p = run(["doveadm", "-f", "tab", "who", "-1"])
    except (OSError, subprocess.SubprocessError) as ex:
        return {"error": str(ex)}
    if p.returncode != 0:
        return {"error": (p.stderr or p.stdout).strip()[:200]}

    rows = parse_who_tab(p.stdout)
    svc_key = next((k for k in ("service", "proto", "protocol") if rows and k in rows[0]), None)
    imap = [r for r in rows if svc_key is None or r.get(svc_key, "").lower() == "imap"]
    users = Counter(r.get("username", "?") for r in imap)
    ips = {r.get("ip") for r in imap if r.get("ip")}
    return {
        "connections": len(imap),
        "users": len(users),
        "unique_ips": len(ips),
        "per_user": users,
    }


def ss_count(ports):
    """Established TCP connections to local ports (fallback / extra view)."""
    flt = " or ".join(f"sport = :{p}" for p in ports)
    try:
        p = run(["ss", "-Htn", "state", "established", f"( {flt} )"])
        return len([l for l in p.stdout.splitlines() if l.strip()])
    except (OSError, subprocess.SubprocessError):
        return None


def imap_processes():
    """Count Dovecot imap worker processes and their total RSS (MiB)."""
    n, rss_kb = 0, 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() != "imap":
                    continue
            n += 1
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss_kb += int(line.split()[1])
                        break
        except (OSError, ValueError):
            continue
    return {"count": n, "rss_mib": round(rss_kb / 1024, 1)}


# ---------------------------------------------------------------- main

def collect(args):
    now = int(time.time())
    mbdir = resolve_mailboxes_dir(args)
    acc = account_stats(mbdir, now, with_birth=not args.no_birth)
    who = imap_sessions()
    data = {
        "timestamp": now,
        "mailboxes_dir": str(mbdir),
        "accounts": acc,
        "imap": {k: v for k, v in who.items() if k != "per_user"},
        "tcp_established": {"993": ss_count([993]), "143": ss_count([143]), "443": ss_count([443])},
        "imap_processes": imap_processes(),
        "loadavg": [round(x, 2) for x in os.getloadavg()],
    }
    top = who.get("per_user", Counter()).most_common(args.top) if args.top else []
    return data, top


def print_human(d, top):
    a, i, t, p = d["accounts"], d["imap"], d["tcp_established"], d["imap_processes"]
    print(f"chatmail-stats {VERSION}  {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(d['timestamp']))}")
    print(f"mailboxes: {d['mailboxes_dir']}\n")
    print("Accounts")
    print(f"  total                {a['total']}")
    act = a["active"]
    print(f"  logged in today(UTC) {act['today']}")
    print(f"  active 7d / 30d / 90d {act['7d']} / {act['30d']} / {act['90d']}")
    print(f"  no login recorded    {a['no_login_recorded']}")
    if "new" in a:
        n = a["new"]
        print(f"  new 24h / 7d         " + (f"{n['24h']} / {n['7d']}" if n else "n/a (no birth time on this FS)"))
    print("\nIMAP (dovecot anvil)")
    if "error" in i:
        print(f"  doveadm who failed: {i['error']}")
    else:
        print(f"  connections          {i['connections']}")
        print(f"  distinct accounts    {i['users']}")
        print(f"  distinct IPs         {i['unique_ips']}")
        if i["users"]:
            print(f"  conns per account    {i['connections'] / i['users']:.2f}")
    print(f"  imap processes       {p['count']}  ({p['rss_mib']} MiB RSS)")
    print("\nTCP established")
    fmt = lambda v: "n/a" if v is None else v
    print(f"  :993 {fmt(t['993'])}   :143 {fmt(t['143'])}   :443 {fmt(t['443'])}"
          "  (443 = IMAP/SMTP via ALPN + web)")
    print(f"\nloadavg {' '.join(map(str, d['loadavg']))}  (cpus: {os.cpu_count()})")
    if top:
        print(f"\nTop {len(top)} accounts by IMAP sessions")
        for user, c in top:
            print(f"  {c:4d}  {user}")


CSV_FIELDS = ["timestamp", "accounts_total", "active_today", "active_7d", "active_30d",
              "new_24h", "imap_connections", "imap_users", "imap_ips",
              "imap_procs", "imap_rss_mib", "tcp_993", "tcp_443", "load1"]


def append_csv(path, d):
    a, i, p = d["accounts"], d["imap"], d["imap_processes"]
    new = (a.get("new") or {}).get("24h", "")
    row = [d["timestamp"], a["total"], a["active"]["today"], a["active"]["7d"], a["active"]["30d"],
           new, i.get("connections", ""), i.get("users", ""), i.get("unique_ips", ""),
           p["count"], p["rss_mib"], d["tcp_established"]["993"], d["tcp_established"]["443"],
           d["loadavg"][0]]
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(CSV_FIELDS)
        w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="chatmail relay account / IMAP stats")
    ap.add_argument("--ini", default=DEFAULT_INI, help=f"chatmail.ini (default {DEFAULT_INI})")
    ap.add_argument("--mailboxes-dir", help="override mailboxes dir")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--csv", metavar="FILE", help="append a line to FILE (quiet)")
    ap.add_argument("--top", type=int, default=0, metavar="N", help="show N accounts with most IMAP sessions")
    ap.add_argument("--no-birth", action="store_true", help="skip new-account count (faster on huge relays)")
    args = ap.parse_args()

    if args.csv:
        args.top = 0
    d, top = collect(args)

    if args.csv:
        append_csv(args.csv, d)
    elif args.json:
        if top:
            d["imap"]["top"] = [{"user": u, "connections": c} for u, c in top]
        print(json.dumps(d, indent=2))
    else:
        print_human(d, top)


if __name__ == "__main__":
    main()
