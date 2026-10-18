#!/usr/bin/env python3
"""chatmail-apps-mirror — local mirror of the webxdc App Picker.

Copies two upstream sites into one directory served by the relay's nginx:

  * the xdcget export (https://apps.testrun.org/): xdcget-lock.json,
    the .xdc files and their icons — what Delta Chat Desktop reads;
  * the app store page (https://webxdc.org/apps/): index.html, main.js,
    css and js deps — what Android/iOS users open in a browser.

Their file names do not overlap, so one URL (e.g. https://relay/apps/) serves
both clients. Two rewrites make the copy self-contained:

  * main.js: `xdcget_export` points at our directory instead of testrun.org;
  * xdcget-lock.json: every app's `url` points at our copy of the .xdc.
    Desktop with a custom store URL downloads from `url` (the developer's
    GitHub/Codeberg release), not from the store, so without this the files
    would still come from abroad. The store page does not read `url`.

Each run builds a complete new release next to the old one (unchanged files
are hard-linked, not downloaded again), checks it, and then switches the
`--dest` symlink atomically. If anything fails, the previous copy stays.

Usage: chatmail-apps-mirror [--dest /var/www/html/apps] [--public-url URL]
"""
import argparse
import email.utils
import fcntl
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXPORT_URL = "https://apps.testrun.org/"
STORE_URL = "https://webxdc.org/apps/"
LOCK_NAME = "xdcget-lock.json"
INI = "/usr/local/lib/chatmaild/chatmail.ini"
UA = "chatmail-apps-mirror (+https://github.com/mrgluek/chatmail-relay-tools)"


def log(*a):
    print(*a, flush=True)


def get(url, headers=None, tries=3):
    """GET url, return (status, body, response headers). 304 is not an error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, b"", e.headers
            if e.code < 500 or attempt == tries:
                raise
        except OSError:
            if attempt == tries:
                raise
        time.sleep(5 * attempt)


def write(path, data, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))


def last_modified(headers):
    v = headers.get("Last-Modified")
    if v:
        try:
            return email.utils.parsedate_to_datetime(v).timestamp()
        except (TypeError, ValueError):
            pass
    return None


def fetch_file(url, rel, new, old):
    """Put `rel` into the new release: hard-link it from the old one if the
    server says it has not changed since (If-Modified-Since -> 304),
    otherwise download it. Returns True if it was downloaded.

    The lock's `size` field is not used: it does not always match the cached
    file, so it cannot tell whether our copy is current."""
    src, dst = os.path.join(old, rel) if old else None, os.path.join(new, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    headers = {}
    if src and os.path.isfile(src):
        headers["If-Modified-Since"] = email.utils.formatdate(
            os.path.getmtime(src), usegmt=True)
    status, body, h = get(url, headers)
    if status == 304:
        os.link(src, dst)
        return False
    if not body:
        raise RuntimeError(f"{rel}: empty response")
    write(dst, body, last_modified(h))
    return True


def store_assets(text, base):
    """Relative files referenced from an html/js/css file, resolved against
    its directory `base` (a path inside the store, '' for the root)."""
    refs = re.findall(r'(?:href|src)="(\./[^"]+|[\w.-][^":#?]*)"', text)  # html
    refs += re.findall(r'(?:from|import)\s*["\'](\.{1,2}/[^"\']+)["\']', text)  # js
    refs += re.findall(r'url\(["\']?(?!data:|https?:|#)([^"\')]+)["\']?\)', text)  # css
    out = set()
    for r in refs:
        if "${" in r or "://" in r:
            continue
        p = os.path.normpath(os.path.join(base, r))
        if not p.startswith(".."):
            out.add(p)
    return out


def mirror_store(new, old, public_path):
    """Copy the store page with all files it references."""
    todo, seen, n = ["index.html"], set(), 0
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        n += fetch_file(STORE_URL + rel, rel, new, old)
        if rel.endswith((".html", ".js", ".css")):
            with open(os.path.join(new, rel), encoding="utf-8") as f:
                todo += store_assets(f.read(), os.path.dirname(rel))

    # Point the page at our copy of the export. The file may be a hard link
    # into the old release, so write a new file instead of editing in place.
    path = os.path.join(new, "main.js")
    with open(path, encoding="utf-8") as f:
        js = f.read()
    js, count = re.subn(r'(const\s+xdcget_export\s*=\s*)"[^"]*"',
                        lambda m: m.group(1) + json.dumps(public_path), js)
    if count != 1:
        raise RuntimeError("main.js: xdcget_export not found, upstream changed")
    os.unlink(path)
    write(path, js.encode())
    return seen, n


def mirror_export(new, old, public_url):
    _, body, _ = get(EXPORT_URL + LOCK_NAME)
    apps = json.loads(body)
    if not isinstance(apps, list) or not apps:
        raise RuntimeError(f"{LOCK_NAME}: expected a non-empty list")
    n = 0
    for app in apps:
        for key in ("cache_relname", "icon_relname"):
            rel = app.get(key)
            if not rel:
                continue
            if "/" in rel or rel.startswith("."):
                raise RuntimeError(f"{app.get('app_id')}: bad {key} {rel!r}")
            n += fetch_file(EXPORT_URL + urllib.parse.quote(rel), rel, new, old)
        if app.get("cache_relname"):
            app["url"] = public_url + urllib.parse.quote(app["cache_relname"])
    write(os.path.join(new, LOCK_NAME), json.dumps(apps, indent=1).encode())
    return apps, n


def default_public_url():
    try:
        with open(INI) as f:
            m = re.search(r"^mail_domain\s*=\s*(\S+)", f.read(), re.M)
        if m:
            return f"https://{m.group(1)}/apps/"
    except OSError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dest", default="/var/www/html/apps",
                    help="symlink served by nginx (default: %(default)s)")
    ap.add_argument("--releases", default="/var/lib/chatmail-apps-mirror",
                    help="where releases are kept (default: %(default)s)")
    ap.add_argument("--public-url", default=default_public_url(),
                    help="URL of --dest, ends with / (default: https://<mail_domain>/apps/)")
    args = ap.parse_args()
    if not args.public_url:
        ap.error("--public-url is required when chatmail.ini is not found")
    public_url = args.public_url.rstrip("/") + "/"
    public_path = urllib.parse.urlsplit(public_url).path.rstrip("/")
    if os.path.lexists(args.dest) and not os.path.islink(args.dest):
        sys.exit(f"{args.dest} exists and is not a symlink, move it away first")

    os.umask(0o022)  # nginx runs as www-data and must read everything
    os.makedirs(args.releases, exist_ok=True)
    lockf = open(os.path.join(args.releases, ".lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("another run is in progress")

    old = os.path.realpath(args.dest) if os.path.islink(args.dest) else None
    new = os.path.join(args.releases, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    if os.path.exists(new):  # two runs in the same second: never reuse `old`
        sys.exit(f"{new} already exists, try again in a second")
    os.makedirs(new)
    try:
        apps, n_export = mirror_export(new, old, public_url)
        store_files, n_store = mirror_store(new, old, public_path)
        clash = store_files & {a.get(k) for a in apps for k in ("cache_relname", "icon_relname")}
        if clash or LOCK_NAME in store_files:
            raise RuntimeError(f"store and export share file names: {sorted(clash)}")
    except Exception as e:
        shutil.rmtree(new, ignore_errors=True)
        sys.exit(f"mirror failed, keeping the previous copy: {e}")

    # Atomic switch: a new symlink is renamed over the old one, so nginx sees
    # either the old release or the new one, never a half-written directory.
    tmp = args.dest + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    os.symlink(new, tmp)
    os.replace(tmp, args.dest)

    # Keep the previous release for a manual rollback, drop older ones.
    keep = {new, old}
    for d in os.listdir(args.releases):
        p = os.path.join(args.releases, d)
        if os.path.isdir(p) and p not in keep:
            shutil.rmtree(p)
    log(f"{len(apps)} apps, downloaded {n_export} export + {n_store} store files, "
        f"now serving {new} at {public_url}")


if __name__ == "__main__":
    main()
