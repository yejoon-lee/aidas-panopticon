#!/usr/bin/env python3
"""Publish the AIDAS storage dashboard to GitHub Pages.

Runs on aidas as an ordinary user, from crontab every 15 minutes. Reads the hourly scan
(/mnt/TrueNAS/yejoon/aidas-storage/latest.json, written by the root job in aidas/) and the
/mnt/TrueNAS scan next to it (truenas.json, from aidas/truenas-scan), checks their shape, keeps one
snapshot per day for 30 days and one per week (the week's last day) for 27 weeks, renders
template.html into a static page, and
force-pushes it as a single commit to the gh-pages branch of yejoon-lee/aidas-panopticon with a
deploy key that can push to this repository only (~/.ssh/aidas_panopticon_deploy).
Does nothing when neither scan is new.

  publish.py                                          the cron job
  publish.py --force                                  publish now, even if the scan is not new
  publish.py --input scan.json --out DIR --no-push    render a local file (preview, any machine)
"""
import argparse
import datetime
import fcntl
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

if sys.version_info < (3, 7):
    sys.exit("publish.py needs Python 3.7 or newer (try /mnt/data/miniconda3/bin/python3)")

HERE = pathlib.Path(__file__).resolve().parent
STATE = HERE / "state"
HISTORY = STATE / "history.json"
WEEKLY = STATE / "weekly.json"
PUBLISHED = STATE / "published_at.txt"
LAST_ERROR = STATE / "last_error.txt"
LOCK = STATE / "publish.lock"
TEMPLATE = HERE / "template.html"

SCAN_FILE = "/mnt/TrueNAS/yejoon/aidas-storage/latest.json"
NET_SCAN_FILE = "/mnt/TrueNAS/yejoon/aidas-storage/truenas.json"
NET_FRESH_HOURS = 26   # an older TrueNAS scan is still shown (marked as old) but not kept in the history
LIVE_DATA = "https://yejoon.me/aidas-panopticon/data.json"
MAX_BYTES = 2_000_000
REPO = "yejoon-lee/aidas-panopticon"
BRANCH = "gh-pages"
# GitHub over ssh; the same service on port 443 is tried when port 22 is blocked
REMOTES = [f"git@github.com:{REPO}.git", f"ssh://git@ssh.github.com:443/{REPO}.git"]
DEPLOY_KEY = pathlib.Path.home() / ".ssh" / "aidas_panopticon_deploy"
AUTHOR_NAME, AUTHOR_EMAIL = "yejoon-lee", "50051856+yejoon-lee@users.noreply.github.com"
GIT = shutil.which("git") or "/usr/bin/git"
KST = datetime.timezone(datetime.timedelta(hours=9))
KEEP_DAYS = 30
KEEP_WEEKS = 27   # covers the page's 6-month chart
NAME_RE = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
DISK_RE = re.compile(r"/[A-Za-z0-9_./-]{0,63}")
FOLDER_RE = re.compile(r"[^/\x00-\x1f\x7f]{1,128}")   # NAS folder names; the page escapes them


def log(msg):
    print(f"{datetime.datetime.now(KST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def read_scan(path):
    # Read in a child process with a timeout: the scan file is on a hard-mounted NFS share,
    # where a read from an unresponsive NAS would otherwise block forever.
    try:
        p = subprocess.run(["cat", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out reading {path} (is the NAS responding?)") from None
    if p.returncode != 0:
        raise RuntimeError(f"cannot read {path}: {p.stderr.decode(errors='replace').strip()[:200]}")
    if len(p.stdout) > MAX_BYTES:
        raise RuntimeError(f"scan file too large ({len(p.stdout)} bytes)")
    return json.loads(p.stdout)


def _check(cond, what):
    if not cond:
        raise ValueError(f"bad {what}")


def _num(v):
    return type(v) is int and v >= 0  # excludes bool, float, str


def _sizes(d, allowed):
    return isinstance(d, dict) and all(k in allowed and _num(v) for k, v in d.items())


def _names_ok(d, ok):
    return isinstance(d, dict) and all(NAME_RE.fullmatch(n) and ok(v) for n, v in d.items())


def validate(doc):
    """The scan file sits on a shared network drive, so publish only data of the expected
    shape: known keys, non-negative integer sizes, plain user and disk names."""
    fs_keys = {"size", "used", "avail"}
    _check(isinstance(doc, dict) and doc.get("schema") == 1, "schema")
    datetime.datetime.fromisoformat(doc["generated_at"])
    _check(type(doc["scan_seconds"]) in (int, float) and doc["scan_seconds"] >= 0, "scan_seconds")
    _check(type(doc["as_root"]) is bool and type(doc["complete"]) is bool, "flags")
    _check(isinstance(doc["users"], list) and all(isinstance(n, str) and NAME_RE.fullmatch(n)
                                                  for n in doc["users"]), "users")
    r = doc["root_fs"]
    _check(_sizes(r["fs"], fs_keys) and _num(r["unaccounted"]) and type(r["quota_available"]) is bool
           and _sizes(r["system"], {"home", "tmp", "var_tmp", "other"})
           and _names_ok(r["users"], lambda u: _sizes(u, {"home", "tmp", "var_tmp", "other",
                                                          "quota_used", "quota_hard"})), "root_fs")
    for group, ok in (("data_disks", lambda d: _sizes(d["fs"], fs_keys) and _num(d["other"])
                       and _num(d["unaccounted"]) and _names_ok(d["users"], _num)),
                      ("shared_disk", lambda d: _sizes(d["fs"], fs_keys) and _num(d["unaccounted"])
                       and _sizes(d["common"], {"docker", "conda_base", "root", "other"})
                       and _names_ok(d["users"], lambda u: _sizes(u, {"cache", "conda", "singularity"})))):
        _check(isinstance(doc[group], dict), group)
        for disk, d in doc[group].items():
            _check(DISK_RE.fullmatch(disk) and type(d["mounted"]) is bool, f"{group} {disk}")
            _check(not d["mounted"] or ok(d), f"{group} {disk}")


def validate_net(net):
    """Shape check for truenas.json (also on the shared drive)."""
    fs_keys = {"size", "used", "avail"}
    _check(isinstance(net, dict) and net.get("schema") == 1 and DISK_RE.fullmatch(net.get("path", "")), "schema")
    datetime.datetime.fromisoformat(net["generated_at"])
    _check(type(net["scan_seconds"]) in (int, float) and net["scan_seconds"] >= 0, "scan_seconds")
    _check(type(net["complete"]) is bool and net["mounted"] is True and _num(net["errors"]), "flags")
    _check(_sizes(net["fs"], fs_keys) and _num(net["other"]) and _num(net["unaccounted"])
           and _names_ok(net["users"], _num), "sizes")
    sh = net["shared"]
    _check(isinstance(sh, dict) and len(sh) <= 200, "shared")
    for name, f in sh.items():
        _check(FOLDER_RE.fullmatch(name) and isinstance(f, dict)
               and set(f) == {"size", "children", "rest", "rest_count"}
               and _num(f["size"]) and _num(f["rest"]) and _num(f["rest_count"])
               and isinstance(f["children"], dict) and len(f["children"]) <= 50
               and all(FOLDER_RE.fullmatch(c) and _num(b) for c, b in f["children"].items()), f"shared {name}")


def read_net():
    """(TrueNAS scan, None) or (None, why). Without it the rest of the page still publishes."""
    try:
        net = read_scan(NET_SCAN_FILE)
        validate_net(net)
        return net, None
    except Exception as e:  # noqa: BLE001 - reported with the next publish
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def _net_fresh(doc, net):
    t = lambda s: datetime.datetime.fromisoformat(s)
    return abs((t(doc["generated_at"]) - t(net["generated_at"])).total_seconds()) < NET_FRESH_HOURS * 3600


def summarize(doc):
    """What the history keeps per day: filesystem usage and per-user totals per space."""
    snap = {"generated_at": doc["generated_at"], "fs": {}, "root": {}, "data": {}, "shared": {}}
    root = doc["root_fs"]
    snap["fs"]["/"] = {k: root["fs"][k] for k in ("used", "avail", "size")}
    for name, u in root["users"].items():
        snap["root"][name] = sum(v for k, v in u.items() if not k.startswith("quota"))
    for disk, d in doc["data_disks"].items():
        if d.get("mounted"):
            snap["fs"][disk] = {k: d["fs"][k] for k in ("used", "avail", "size")}
            for name, v in d["users"].items():
                snap["data"][name] = snap["data"].get(name, 0) + v
    for disk, d in doc["shared_disk"].items():
        if d.get("mounted"):
            snap["fs"][disk] = {k: d["fs"][k] for k in ("used", "avail", "size")}
            for name, u in d["users"].items():
                snap["shared"][name] = sum(u.values())
    for disk, d in doc.get("network_disks", {}).items():
        if _net_fresh(doc, d):
            snap["fs"][disk] = {k: d["fs"][k] for k in ("used", "avail", "size")}
            net = snap.setdefault("net", {})   # only on days with a TrueNAS scan
            for name, v in d["users"].items():
                net[name] = net.get(name, 0) + v
    return snap


def _snapshot_ok(day, s):
    try:
        datetime.date.fromisoformat(day)
        datetime.datetime.fromisoformat(s["generated_at"])
        return bool(isinstance(s["fs"], dict)
                    and all(DISK_RE.fullmatch(k) and _sizes(v, {"used", "avail", "size"})
                            for k, v in s["fs"].items())
                    and all(_names_ok(s[k], _num) for k in ("root", "data", "shared"))
                    and _names_ok(s.get("net", {}), _num))
    except (KeyError, TypeError, ValueError):
        return False


def _live(key):
    """First run on a new machine: start from the history the live page already shows."""
    try:
        # Cloudflare (in front of yejoon.me) answers Python's default User-Agent with 403
        req = urllib.request.Request(LIVE_DATA, headers={"User-Agent": "aidas-panopticon publisher"})
        with urllib.request.urlopen(req, timeout=30) as r:
            hist = json.loads(r.read(MAX_BYTES))[key]
        return {d: s for d, s in sorted(hist.items()) if _snapshot_ok(d, s)}
    except Exception as e:  # noqa: BLE001 - starting with an empty history is fine
        log(f"no {key} copied from the live page ({type(e).__name__}: {e})")
        return None


def seed_history():
    hist = _live("history") or {}
    HISTORY.write_text(json.dumps(hist, indent=1, sort_keys=True) + "\n")
    log(f"copied {len(hist)} days of history from the live page")
    return hist


def load_history():
    return json.loads(HISTORY.read_text()) if HISTORY.exists() else seed_history()


def update_history(doc):
    """Keep the latest complete scan of each KST day, for the last KEEP_DAYS days."""
    hist = load_history()
    day = datetime.datetime.fromisoformat(doc["generated_at"]).astimezone(KST).date()
    hist[day.isoformat()] = summarize(doc)
    cutoff = day - datetime.timedelta(days=KEEP_DAYS - 1)
    hist = {k: v for k, v in sorted(hist.items()) if datetime.date.fromisoformat(k) >= cutoff}
    HISTORY.write_text(json.dumps(hist, indent=1, sort_keys=True) + "\n")
    return hist


def _week(day):
    """Monday of the (KST) week of an ISO date."""
    d = datetime.date.fromisoformat(day)
    return (d - datetime.timedelta(days=d.weekday())).isoformat()


def update_weekly(hist):
    """Keep the last daily snapshot of each week, for the last KEEP_WEEKS weeks. Weeks still in the
    daily history are recomputed from it, so the first run fills them in; older ones are kept as stored."""
    new = not WEEKLY.exists()
    weekly = (_live("weekly") or {}) if new else json.loads(WEEKLY.read_text())
    for day, snap in sorted(hist.items()):   # later days overwrite earlier ones of the same week
        weekly[_week(day)] = snap
    if new:
        log(f"started the weekly history: {len(weekly)} weeks (live page + daily history)")
    if hist:
        cutoff = datetime.date.fromisoformat(_week(max(hist))) - datetime.timedelta(weeks=KEEP_WEEKS - 1)
        weekly = {k: v for k, v in sorted(weekly.items()) if datetime.date.fromisoformat(k) >= cutoff}
    WEEKLY.write_text(json.dumps(weekly, indent=1, sort_keys=True) + "\n")
    return weekly


def render(payload):
    # every "<" becomes \u003c, so nothing in the data can end the <script> element
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    tpl = TEMPLATE.read_text()
    assert "/*__DATA__*/null" in tpl
    return tpl.replace("/*__DATA__*/null", data)


def write_site(site, html, payload):
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(html)
    (site / "data.json").write_text(json.dumps(payload, indent=1) + "\n")
    (site / ".nojekyll").write_text("")


def push(site, message, remotes, key, branch):
    """Commit the site as a single commit and force-push it; returns the remote that worked."""
    if any(r.startswith(("git@", "ssh://")) for r in remotes) and not key.exists():
        raise RuntimeError(f"deploy key {key} not found (see README)")
    env = dict(os.environ, GIT_SSH_COMMAND=(
        f"ssh -i '{key}' -o IdentitiesOnly=yes -o BatchMode=yes"
        " -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"))

    def git(*args):
        p = subprocess.run([GIT, "-C", str(site), *args], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=180, env=env)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.decode(errors="replace").strip() or f"git exit {p.returncode}")

    git("init", "-q")
    git("symbolic-ref", "HEAD", f"refs/heads/{branch}")  # same as `init -b`, which needs git 2.28+
    git("add", "-A")
    git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
        "commit", "-q", "-m", message)
    # One commit, force-pushed: the repository does not grow; the history lives in data.json.
    errors = []
    for remote in remotes:
        try:
            git("push", "-q", "-f", remote, f"HEAD:refs/heads/{branch}")
            return remote
        except RuntimeError as e:
            errors.append(f"{remote}: {e}")
    raise RuntimeError(" | ".join(errors))


def note_error(msg):
    """Log an error only when it differs from the previous one (cron runs every 15 min)."""
    prev = LAST_ERROR.read_text() if LAST_ERROR.exists() else ""
    if msg != prev:
        log(f"ERROR {msg}")
        LAST_ERROR.write_text(msg)


def main():
    ap = argparse.ArgumentParser(description="Publish the AIDAS storage dashboard.")
    ap.add_argument("--input", default=SCAN_FILE, help="scan JSON to publish (default: %(default)s)")
    ap.add_argument("--net-input", help="TrueNAS scan JSON to use with --input (preview)")
    ap.add_argument("--out", help="also write the site to this directory")
    ap.add_argument("--no-push", action="store_true", help="do not push to GitHub")
    ap.add_argument("--force", action="store_true", help="publish even if the scan is not new")
    ap.add_argument("--deploy-key", type=pathlib.Path, default=DEPLOY_KEY, help="default: %(default)s")
    ap.add_argument("--remote", action="append", help="push here instead (repeatable; for testing)")
    ap.add_argument("--branch", default=BRANCH, help="default: %(default)s")
    args = ap.parse_args()
    STATE.mkdir(exist_ok=True)

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0  # the previous run is still going (e.g. waiting on the NAS)

    try:
        doc = read_scan(args.input)
        validate(doc)
    except Exception as e:  # noqa: BLE001 - logged, retried next run
        note_error(f"{type(e).__name__}: {e}")
        return 1

    net, net_err = None, None
    if args.net_input:
        net = read_scan(args.net_input)
        validate_net(net)
    elif args.input == SCAN_FILE:
        net, net_err = read_net()
    if net:
        doc["network_disks"] = {net["path"]: net}
    stamp = doc["generated_at"] + (" " + net["generated_at"] if net else "")
    last = PUBLISHED.read_text().strip() if PUBLISHED.exists() else ""
    if stamp == last and not args.force:
        return 0
    hist = update_history(doc) if doc.get("complete") else load_history()
    weekly = update_weekly(hist)
    payload = {"latest": doc, "history": hist, "weekly": weekly}
    html = render(payload)
    if args.out:
        write_site(pathlib.Path(args.out), html, payload)
    if args.no_push:
        return 0
    remotes = args.remote or REMOTES
    try:
        with tempfile.TemporaryDirectory() as tmp:
            site = pathlib.Path(tmp) / "site"
            write_site(site, html, payload)
            used = push(site, f"Update {doc['generated_at']}", remotes, args.deploy_key, args.branch)
    except Exception as e:  # noqa: BLE001 - logged, retried next run
        note_error(f"push failed: {str(e)[:500]}")
        return 1
    PUBLISHED.write_text(stamp + "\n")
    if LAST_ERROR.exists():
        LAST_ERROR.unlink()
    via = f" via {used}" if used != remotes[0] else ""
    log(f"published scan {doc['generated_at']} (complete={doc.get('complete')}, "
        f"TrueNAS {net['generated_at'] if net else 'not used: ' + str(net_err)}, "
        f"{len(hist)} days + {len(weekly)} weeks of history){via}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
