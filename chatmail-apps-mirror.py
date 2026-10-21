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

Icons above 192 px are downscaled in their own format if python3-pil is
installed (the store shows them at 64 CSS px).

Each run builds a complete new release next to the old one, checks it, and
then switches the `--dest` symlink atomically. If anything fails, the
previous copy stays. Unchanged files are hard-linked from the old release
(.xdc by name, everything else by content), so they keep their mtime and
nginx keeps the same Last-Modified/ETag: browsers reuse their cached icons.

Usage: chatmail-apps-mirror [--dest /var/www/html/apps] [--public-url URL]
"""
import argparse
import email.utils
import fcntl
import io
import json
import os
import re
import shutil
import socket
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


# urllib tries addresses in getaddrinfo order and waits the full timeout on
# each. On relays with an IPv6 route that silently drops packets (e.g. a
# Yggdrasil 200::/7 address) every request then hangs for a minute before
# falling back. curl avoids this with Happy Eyeballs; we just try IPv4 first.
_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_v4_first(*args, **kwargs):
    return sorted(_getaddrinfo(*args, **kwargs), key=lambda ai: ai[0] != socket.AF_INET)


socket.getaddrinfo = _getaddrinfo_v4_first


def log(*a):
    print(*a, flush=True)


def get(url, headers=None, tries=3):
    """GET url, return (status, body, response headers). 304 is not an error."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
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


def put(rel, data, new, old, mtime=None):
    """Write `rel` into the new release. If the old release has the same
    bytes, hard-link that file instead: it keeps its mtime, so nginx keeps
    sending the same Last-Modified/ETag and browsers can reuse their cache.
    Returns True if the content changed."""
    src, dst = os.path.join(old, rel) if old else None, os.path.join(new, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if src and os.path.isfile(src) and os.path.getsize(src) == len(data):
        with open(src, "rb") as f:
            if f.read() == data:
                os.link(src, dst)
                return False
    write(dst, data, mtime)
    return True


def fetch_file(url, rel, new, old, immutable=False, transform=None):
    """Put `rel` into the new release, reusing the old copy when possible.
    Returns True if the content changed.

    apps.testrun.org regenerates its export every few hours and every file
    gets a fresh mtime, so If-Modified-Since alone would re-download (and
    re-date) everything. Hence:
      * immutable files (.xdc, the version is in the name) are reused
        without asking the server;
      * other files are compared by content after download (see put()).
    The lock's `size` field is not used: it does not always match the file."""
    src = os.path.join(old, rel) if old else None
    if immutable and src and os.path.isfile(src):
        os.makedirs(os.path.dirname(os.path.join(new, rel)), exist_ok=True)
        os.link(src, os.path.join(new, rel))
        return False
    headers = {}
    if src and os.path.isfile(src) and not transform:
        # Still useful for webxdc.org, whose files keep their dates.
        headers["If-Modified-Since"] = email.utils.formatdate(
            os.path.getmtime(src), usegmt=True)
    status, body, h = get(url, headers)
    if status == 304:
        os.makedirs(os.path.dirname(os.path.join(new, rel)), exist_ok=True)
        os.link(src, os.path.join(new, rel))
        return False
    if not body:
        raise RuntimeError(f"{rel}: empty response")
    if transform:
        body = transform(body)
    return put(rel, body, new, old, last_modified(h))


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

    def patch_main_js(body):
        # Point the page at our copy of the export.
        js, count = re.subn(rb'(const\s+xdcget_export\s*=\s*)"[^"]*"',
                            lambda m: m.group(1) + json.dumps(public_path).encode(), body)
        if count != 1:
            raise RuntimeError("main.js: xdcget_export not found, upstream changed")
        return js

    todo, seen, n = ["index.html"], set(), 0
    while todo:
        rel = todo.pop()
        if rel in seen:
            continue
        seen.add(rel)
        n += fetch_file(STORE_URL + rel, rel, new, old,
                        transform=patch_main_js if rel == "main.js" else None)
        if rel.endswith((".html", ".js", ".css")):
            with open(os.path.join(new, rel), encoding="utf-8") as f:
                todo += store_assets(f.read(), os.path.dirname(rel))
    if "main.js" not in seen:
        raise RuntimeError("store page no longer loads main.js, upstream changed")
    return seen, n


ICON_MAX = 192  # px; the store shows icons at 64 CSS px, so this covers 3x screens
icon_stats = {"shrunk": 0, "before": 0, "after": 0}


def shrink_icon(body):
    """Downscale an icon to ICON_MAX px on its longer side, keeping its format
    (PNG stays PNG, JPEG stays JPEG), so file names, the lock and main.js stay
    as they are. Returns the original bytes when Pillow is missing, the image
    is small already, or the result would not be smaller.

    Pillow writes the same bytes for the same input, so put() can still tell
    that an icon did not change and reuse the old file with its ETag."""
    if Image is None:
        return body
    try:
        im = Image.open(io.BytesIO(body))
        fmt = im.format
        if fmt not in ("PNG", "JPEG") or max(im.size) <= ICON_MAX \
                or getattr(im, "n_frames", 1) > 1:
            return body
        if fmt == "JPEG":
            im = ImageOps.exif_transpose(im)  # the EXIF tag is not written back
        elif im.mode == "P":
            # Palette images would be resized with NEAREST; go to RGBA first.
            im = im.convert("RGBA")
        im.thumbnail((ICON_MAX, ICON_MAX), Image.LANCZOS)
        out = io.BytesIO()
        if fmt == "PNG":
            im.save(out, "PNG", optimize=True)
        else:
            im.save(out, "JPEG", quality=85, optimize=True)
    except Exception as e:  # a broken icon must not stop the mirror
        log(f"icon left as is: {e}")
        return body
    if out.tell() >= len(body):
        return body
    icon_stats["shrunk"] += 1
    icon_stats["before"] += len(body)
    icon_stats["after"] += out.tell()
    return out.getvalue()


try:
    from PIL import Image, ImageOps
except ImportError:
    Image = ImageOps = None


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
            if key == "cache_relname":
                n += fetch_file(EXPORT_URL + urllib.parse.quote(rel), rel, new, old,
                                immutable=True)
            else:
                n += fetch_file(EXPORT_URL + urllib.parse.quote(rel), rel, new, old,
                                transform=shrink_icon)
        if app.get("cache_relname"):
            app["url"] = public_url + urllib.parse.quote(app["cache_relname"])
    n += put(LOCK_NAME, json.dumps(apps, indent=1).encode(), new, old)
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

    if Image is None:
        log("python3-pil not installed: icons are served at their original size")
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
    log(f"{len(apps)} apps, changed {n_export} export + {n_store} store files, "
        f"now serving {new} at {public_url}")
    if icon_stats["shrunk"]:
        log(f"icons: {icon_stats['shrunk']} downscaled to {ICON_MAX}px, "
            f"{icon_stats['before'] // 1024} KiB -> {icon_stats['after'] // 1024} KiB")


if __name__ == "__main__":
    main()
