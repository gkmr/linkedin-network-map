#!/usr/bin/env python3
"""refresh.py: rebuild the network index when a new LinkedIn archive lands, check it, and publish it atomically.
Meant to run daily from launchd; on a day with no new archive it exits in well under a second.

Run:  /usr/bin/python3 -I refresh.py [--force] [--keep-temp]

Settings come from environment variables (or the flag of the same name, which wins):
  LINKEDIN_MAP_EXPORTS_DIR  --exports-dir  folder where you save LinkedIn archive zips. If it has YYYY-MM
                                           subfolders, the newest one with a zip is used; else the folder itself.
  LINKEDIN_MAP_OUT_DIR      --out-dir      folder the index is published to
  LINKEDIN_MAP_TARGETS      --targets      targets file (default: <out dir>/targets.txt)
  LINKEDIN_MAP_STATE_DIR    --state-dir    refresh.log and the lock file (default: ~/.local/state/linkedin-network-map)
  LINKEDIN_MAP_LIMIT_BYTES  --limit-bytes  max bytes per index file (default 44000)

Steps:
  a. pick the archive: the largest .zip in the newest month folder (a full archive beats the basic one that
     LinkedIn sends first); a month with no zip yet falls back to the one before
  b. stop with "no change" when archive-manifest.json already names that zip with the same size (unless --force)
  c. copy the zip to a fresh temp folder and extract only Connections.csv, to a fixed name; entries with absolute
     paths, '..' or symlinks are skipped, so the archive never chooses where anything is written
  d. build with build_network_index.py; the newest network-map-*.csv in the out dir is the old map, so rows you
     added by hand (any Channel other than LinkedIn) carry over
  e. check the build: every part parses, keys are sorted and within their letter, parts stay under the size limit,
     part ranges do not overlap, index rows equal map rows, and nothing phone-shaped is left anywhere
  f. publish: copy each file to a temp name in the out dir, then os.replace it onto the final name; index parts
     that are no longer in the set move to _superseded/<date>/ (never deleted); archive-manifest.json goes last,
     so a run that fails partway simply rebuilds next time
  g. log each step with timings to refresh.log, print one JSON summary line, exit 1 on any failure

Python 3 standard library only. Nothing from the archive is executed or imported.
"""
import argparse
import fcntl
import glob
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import zipfile
from collections import OrderedDict
from datetime import date, datetime, timezone

VERSION = "2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_MANIFEST = "archive-manifest.json"
INDEX_MANIFEST = "network-index-manifest.json"
MAP_RE = re.compile(r"^network-map-(\d{4}-\d{2}-\d{2})\.csv$")
MAX_CONNECTIONS_BYTES = 512 * 1024 * 1024     # refuse a Connections.csv bigger than this (zip bomb guard)


class Fail(Exception):
    pass


def load_builder():
    """Load the builder by its path next to this file. With -I, sys.path has neither this folder nor the cwd."""
    sys.dont_write_bytecode = True                # no __pycache__ next to the scripts
    spec = importlib.util.spec_from_file_location("build_network_index", os.path.join(HERE, "build_network_index.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Log:
    def __init__(self, path):
        self.path, self.t0 = path, time.time()
        self.t = self.t0
        try:
            if os.path.getsize(path) > 2000000:      # keep the log small: last 5,000 lines
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()[-5000:]
                with open(path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
        except OSError:
            pass

    def __call__(self, step, msg):
        now = time.time()
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
        line = "%s [%s] %s (+%.1fs, %.1fs total)" % (stamp, step, msg, now - self.t, now - self.t0)
        self.t = now
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def total(self):
        return round(time.time() - self.t0, 2)


def iso_utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def settings(args):
    env = os.environ.get

    def path(v):
        return os.path.abspath(os.path.expanduser(v)) if v else None

    s = OrderedDict()
    s["exports"] = path(args.exports_dir or env("LINKEDIN_MAP_EXPORTS_DIR"))
    s["out"] = path(args.out_dir or env("LINKEDIN_MAP_OUT_DIR"))
    s["targets"] = path(args.targets or env("LINKEDIN_MAP_TARGETS")) or (os.path.join(s["out"], "targets.txt")
                                                                        if s["out"] else None)
    s["state"] = path(args.state_dir or env("LINKEDIN_MAP_STATE_DIR")
                      or os.path.join(env("XDG_STATE_HOME") or "~/.local/state", "linkedin-network-map"))
    s["limit"] = int(args.limit_bytes or env("LINKEDIN_MAP_LIMIT_BYTES") or 44000)
    return s


# ---------------------------------------------------------------- a. choose the zip
def choose_zip(exports, log):
    if not os.path.isdir(exports):
        raise Fail("exports folder not found: %s" % exports)
    months = sorted((d for d in os.listdir(exports)
                     if re.fullmatch(r"\d{4}-\d{2}", d) and os.path.isdir(os.path.join(exports, d))), reverse=True)
    folders = [(m, os.path.join(exports, m)) for m in months] or [("", exports)]
    for m, folder in folders:
        zips = []
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if f.lower().endswith(".zip") and not f.startswith(".") and os.path.isfile(p):
                st = os.stat(p)
                zips.append((st.st_size, st.st_mtime, f, p))
        if not zips:
            log("a.zip", "folder %s has no .zip yet; trying the next older month" % (m or "."))
            continue
        size, mtime, name, path = max(zips)
        others = ", ".join("%s (%d bytes)" % (z[2], z[0]) for z in sorted(zips, reverse=True)[1:])
        log("a.zip", "chose %s (%d bytes, mtime %s)%s" % (os.path.join(m, name), size, iso_utc(mtime),
                                                         "; also there: " + others if others else ""))
        return OrderedDict([("folder", m), ("name", name), ("path", path), ("bytes", size), ("mtime", mtime)])
    raise Fail("no .zip in %s" % exports)


def export_date_of(z):
    """From the zip name (MM-DD-YYYY as LinkedIn names it, or YYYY-MM-DD), else from the file's mtime."""
    m = re.search(r"(?<!\d)(\d{2})-(\d{2})-(\d{4})(?!\d)", z["name"])
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat(), "zip name"
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)", z["name"])
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat(), "zip name"
        except ValueError:
            pass
    return datetime.fromtimestamp(z["mtime"]).date().isoformat(), "zip mtime"


# ---------------------------------------------------------------- c. extract
def extract_connections(zpath, dest, log):
    """Extract the one safe Connections.csv entry (at most two folders deep) to dest/Connections.csv."""
    os.makedirs(dest)                                   # must not exist yet: a fresh, empty folder
    cands, skipped = [], 0
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or parts[-1].lower() != "connections.csv" or name.endswith("/"):
                continue
            is_link = ((info.external_attr >> 16) & 0o170000) == 0o120000
            if name.startswith("/") or ".." in parts or is_link or ":" in parts[0] or len(parts) > 3:
                skipped += 1
                continue
            cands.append(info)
        if len(cands) != 1:
            raise Fail("expected one Connections.csv in the archive, found %d (skipped %d unsafe)" % (len(cands),
                                                                                                     skipped))
        info = cands[0]
        if info.file_size > MAX_CONNECTIONS_BYTES:
            raise Fail("Connections.csv is %d bytes uncompressed; refusing" % info.file_size)
        target = os.path.join(dest, "Connections.csv")
        with z.open(info) as src, open(target, "wb") as dst:   # the CRC is checked while reading
            shutil.copyfileobj(src, dst, 1 << 20)
    log("c.extract", "extracted %s (%d bytes)%s" % (info.filename, info.file_size,
                                                    ", skipped %d unsafe entries" % skipped if skipped else ""))
    return target


# ---------------------------------------------------------------- d. old map
def pick_old_map(out, prev, export_date, log):
    today = date.today().isoformat()
    ours = {f.get("title") for f in ((prev or {}).get("files") or []) if isinstance(f, dict)}
    cands = []
    for p in glob.glob(os.path.join(out, "network-map-*.csv")):
        b = os.path.basename(p)
        m = MAP_RE.match(b)
        if not m:
            continue
        if b in ours and m.group(1) in (today, export_date):
            log("d.oldmap", "skipping %s (written by an earlier run for today or this export)" % b)
            continue
        cands.append((m.group(1), os.path.getmtime(p), p))
    if not cands:
        log("d.oldmap", "no earlier network map; nothing to carry over")
        return None
    best = max(cands)[2]
    log("d.oldmap", "old map: %s" % os.path.basename(best))
    return best


# ---------------------------------------------------------------- f. publish
def publish(out, items, log):
    """items: [(src_path, relative_title)]. Stage all under temp names, then os.replace each onto its name."""
    staged = []
    try:
        for src, rel in items:
            final = os.path.join(out, rel)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            tmp = os.path.join(os.path.dirname(final), ".%s.refresh-tmp" % os.path.basename(final))
            shutil.copyfile(src, tmp)
            if os.path.getsize(tmp) != os.path.getsize(src):
                raise Fail("staged copy of %s has the wrong size" % rel)
            staged.append((tmp, final, rel, os.path.getsize(src)))
    except Exception:
        for tmp, _, _, _ in staged:
            try:
                os.remove(tmp)                            # our own temp files only
            except OSError:
                pass
        raise
    log("f.publish", "staged %d files (%d bytes)" % (len(staged), sum(s[3] for s in staged)))
    done = []
    for tmp, final, rel, size in staged:
        os.replace(tmp, final)
        done.append(OrderedDict([("title", rel), ("bytes", size)]))
    log("f.publish", "replaced %d files" % len(done))
    return done


def supersede(out, keep, part_re, log):
    idx = os.path.join(out, "index")
    stale = sorted(f for f in os.listdir(idx) if part_re.match(f) and f not in keep
                   and os.path.isfile(os.path.join(idx, f))) if os.path.isdir(idx) else []
    if not stale:
        log("f.supersede", "no stale index parts")
        return []
    dest = os.path.join(out, "_superseded", date.today().isoformat())
    os.makedirs(dest, exist_ok=True)
    moved = []
    for f in stale:
        target, n = os.path.join(dest, f), 2
        while os.path.exists(target):
            base, ext = os.path.splitext(f)
            target = os.path.join(dest, "%s.%d%s" % (base, n, ext))
            n += 1
        shutil.move(os.path.join(idx, f), target)
        moved.append(f)
    log("f.supersede", "moved %d stale parts to _superseded/%s/: %s" % (len(moved), os.path.basename(dest),
                                                                         ", ".join(moved)))
    return moved


def write_json_atomic(out, title, obj):
    final = os.path.join(out, title)
    tmp = os.path.join(out, ".%s.refresh-tmp" % title)
    text = json.dumps(obj, ensure_ascii=False, indent=1) + "\n"
    json.loads(text)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, final)
    return os.path.getsize(final)


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="LinkedIn archive -> checked, published network index")
    ap.add_argument("--force", action="store_true", help="rebuild even when the manifest names the chosen zip")
    ap.add_argument("--exports-dir")
    ap.add_argument("--out-dir")
    ap.add_argument("--targets")
    ap.add_argument("--state-dir")
    ap.add_argument("--limit-bytes", type=int)
    ap.add_argument("--keep-temp", action="store_true", help="keep the temp work folder (debugging)")
    args = ap.parse_args(argv)
    s = settings(args)
    summary = OrderedDict([("status", "failed"), ("version", VERSION)])
    if not s["exports"] or not s["out"]:
        summary["error"] = "set LINKEDIN_MAP_EXPORTS_DIR and LINKEDIN_MAP_OUT_DIR (or --exports-dir and --out-dir)"
        print(json.dumps(summary))
        return 2
    os.makedirs(s["state"], exist_ok=True)
    log = Log(os.path.join(s["state"], "refresh.log"))
    work = None
    lockf = open(os.path.join(s["state"], ".refresh.lock"), "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("start", "another refresh is running; exiting")
        summary["status"] = "busy"
        print(json.dumps(summary))
        lockf.close()
        return 0
    try:
        log("start", "refresh.py v%s pid %d python %s force=%s" % (VERSION, os.getpid(), sys.version.split()[0],
                                                                args.force))
        out = s["out"]
        if not os.path.isdir(out):
            raise Fail("out folder not found: %s" % out)
        log("start", "exports: %s; out: %s" % (s["exports"], out))

        # a. which zip
        z = choose_zip(s["exports"], log)
        summary.update([("zip", z["name"]), ("zip_bytes", z["bytes"])])

        # b. already built?
        man_path = os.path.join(out, ARCHIVE_MANIFEST)
        prev = None
        if os.path.isfile(man_path):
            try:
                with open(man_path, encoding="utf-8") as f:
                    prev = json.load(f)
            except (OSError, ValueError) as e:
                log("b.check", "existing %s unreadable (%s); rebuilding" % (ARCHIVE_MANIFEST, e))
        pz = (prev or {}).get("zip") or {}
        if pz.get("name") == z["name"] and str(pz.get("bytes")) == str(z["bytes"]) and not args.force:
            log("b.check", "no change: %s (%d bytes) was built %s" % (z["name"], z["bytes"], prev.get("built_utc")))
            summary.update([("status", "no change"), ("built_utc", prev.get("built_utc", "")),
                            ("seconds", log.total())])
            print(json.dumps(summary))
            return 0
        why = "--force" if args.force else ("manifest names %s (%s bytes)" % (pz.get("name"), pz.get("bytes"))
                                           if prev else "no %s yet" % ARCHIVE_MANIFEST)
        log("b.check", "rebuilding (%s)" % why)
        if not os.path.isfile(s["targets"]):
            raise Fail("targets file not found: %s" % s["targets"])
        bni = load_builder()
        try:
            targets = bni.load_targets(s["targets"])
        except ValueError as e:
            raise Fail("targets file: %s" % e)
        log("b.preflight", "targets: %d labels" % len(targets.labels()))

        # c. copy + extract
        work = tempfile.mkdtemp(prefix="linkedin-network-map-")
        os.makedirs(os.path.join(work, "zip"))
        zcopy = os.path.join(work, "zip", "archive.zip")
        shutil.copyfile(z["path"], zcopy)                 # also downloads a cloud-only file
        if os.path.getsize(zcopy) != z["bytes"]:
            raise Fail("copied zip is %d bytes, expected %d" % (os.path.getsize(zcopy), z["bytes"]))
        log("c.copy", "copied zip to temp (%d bytes)" % z["bytes"])
        conn = extract_connections(zcopy, os.path.join(work, "extract"), log)

        # d. build
        export_date, ed_src = export_date_of(z)
        log("d.inputs", "export_date %s (from %s)" % (export_date, ed_src))
        old_map = pick_old_map(out, prev, export_date, log)
        build_dir = os.path.join(work, "out")
        os.makedirs(build_dir)
        try:
            manifest = bni.build(conn, s["targets"], build_dir, export_date, old_map, s["limit"])
        except ValueError as e:
            raise Fail("build: %s" % e)
        c = manifest["counts"]
        log("d.build", "%d rows kept of %d (%d hidden, %d duplicates), %d at targets, %d carried over, "
                       "%d index files; phone guard: %d redactions, %d emails dropped" % (
                           c["export_rows_kept"], c["connections_rows"], c["hidden_rows_dropped"],
                           c["dupes_removed_by_url"] + c["dupes_removed_by_name_company"], c["mapped_to_target"],
                           c["carried_over"], c["index_files"], c["phone_guard_text_redactions"],
                           c["phone_guard_emails_dropped"]))

        # e. independent check of what is about to be published
        problems = bni.check_output(build_dir)
        if problems:
            raise Fail("check: " + "; ".join(problems))
        log("e.check", "build passed the output checks")

        # f. publish (everything is built and checked by now)
        items = [(os.path.join(build_dir, "index", t), os.path.join("index", t)) for t in manifest["files"]]
        items.append((os.path.join(build_dir, manifest["map_file"]), manifest["map_file"]))
        items.append((os.path.join(build_dir, INDEX_MANIFEST), INDEX_MANIFEST))
        published = publish(out, items, log)
        moved = supersede(out, set(manifest["files"]), bni.PART_RE, log)
        for f in published:
            p = os.path.join(out, f["title"])
            if not os.path.isfile(p) or os.path.getsize(p) != f["bytes"]:
                raise Fail("after publish %s is missing or the wrong size" % f["title"])
        built_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        amani = OrderedDict([
            ("schema", "archive-manifest v1: which LinkedIn archive the published index was built from. refresh.py "
                       "rebuilds when the chosen zip differs by name or size."),
            ("zip", OrderedDict([("folder", z["folder"]), ("name", z["name"]), ("bytes", z["bytes"]),
                                 ("mtime", iso_utc(z["mtime"]))])),
            ("export_date", export_date), ("export_date_source", ed_src), ("built_utc", built_utc),
            ("builder", "%s via refresh.py v%s" % (manifest["generator"], VERSION)),
            ("old_map", os.path.basename(old_map) if old_map else ""),
            ("files", published), ("counts", c), ("superseded", moved),
        ])
        size = write_json_atomic(out, ARCHIVE_MANIFEST, amani)
        log("f.publish", "%s written last (%d bytes)" % (ARCHIVE_MANIFEST, size))
        summary.update([
            ("status", "published"), ("export_date", export_date), ("rows", c["map_rows_total"]),
            ("at_targets", c["mapped_to_target"]), ("index_files", c["index_files"]),
            ("published_files", len(published) + 1), ("superseded", len(moved)), ("seconds", log.total()),
        ])
        log("done", json.dumps(summary))
        print(json.dumps(summary))
        return 0
    except Exception as e:
        reason = str(e) if isinstance(e, Fail) else "%s: %s" % (type(e).__name__, e)
        log("FAIL", reason)
        if not isinstance(e, Fail):
            for ln in traceback.format_exc().splitlines():
                log("FAIL", "  " + ln)
        summary.update([("error", reason[:500]), ("seconds", log.total())])
        print(json.dumps(summary))
        return 1
    finally:
        if work and not args.keep_temp:
            shutil.rmtree(work, ignore_errors=True)
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
            lockf.close()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    sys.exit(main())
