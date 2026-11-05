"""End-to-end tests for refresh.py, refresh.sh and the launchd template, on invented data in temp folders.
Python 3 stdlib only, no network. Run: python3 -m unittest discover -s tests -v
"""
import fcntl
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from helpers import CONNECTIONS, REPO, TARGETS, load, read

refresh = load("refresh")
ZIP_NAME = "Complete_LinkedInDataExport_11-01-2026.zip"


class Sandbox:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="lnm-refresh-")
        self.exports = os.path.join(self.dir, "exports")
        self.out = os.path.join(self.dir, "out")
        self.state = os.path.join(self.dir, "state")
        for d in (os.path.join(self.exports, "2026-10"), os.path.join(self.exports, "2026-11"), self.out):
            os.makedirs(d)
        shutil.copyfile(TARGETS, os.path.join(self.out, "targets.txt"))
        conn = read(CONNECTIONS, "rb")
        with zipfile.ZipFile(os.path.join(self.exports, "2026-11", ZIP_NAME), "w") as z:
            z.writestr("Connections.csv", conn)
            z.writestr("../Connections.csv", b"outside the extract folder")
            z.writestr("messages.csv", b"never extracted")
        with zipfile.ZipFile(os.path.join(self.exports, "2026-11", "Basic_LinkedInDataExport_11-01-2026.zip"), "w") as z:
            z.writestr("Connections.csv", conn[:200])
        with zipfile.ZipFile(os.path.join(self.exports, "2026-10", "Complete_LinkedInDataExport_10-01-2026.zip"), "w") as z:
            z.writestr("Connections.csv", conn)

    def env(self):
        e = dict(os.environ, LINKEDIN_MAP_EXPORTS_DIR=self.exports, LINKEDIN_MAP_OUT_DIR=self.out,
                 LINKEDIN_MAP_STATE_DIR=self.state)
        e.pop("LINKEDIN_MAP_TARGETS", None)
        return e

    def run(self, *args, via_sh=False, env=None):
        cmd = (["bash", os.path.join(REPO, "refresh.sh")] if via_sh
               else [sys.executable, "-I", os.path.join(REPO, "refresh.py")]) + list(args)
        e = env or self.env()
        if via_sh:
            e["LINKEDIN_MAP_PYTHON"] = sys.executable
            e["LINKEDIN_MAP_ENV_FILE"] = os.path.join(self.dir, "no-such-env-file")
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, universal_newlines=True,
                           timeout=60)
        lines = p.stdout.strip().splitlines()
        return p.returncode, (json.loads(lines[-1]) if lines else {}), p.stderr

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class RefreshEndToEnd(unittest.TestCase):
    def setUp(self):
        self.sb = Sandbox()

    def tearDown(self):
        self.sb.close()

    def test_publish_then_no_change_then_force(self):
        rc, s, err = self.sb.run()
        self.assertEqual(rc, 0, err)
        self.assertEqual(s["status"], "published", s)
        self.assertEqual(s["zip"], ZIP_NAME)                 # newest month, largest zip
        self.assertEqual(s["export_date"], "2026-11-01")     # from the MM-DD-YYYY in the name
        self.assertEqual((s["rows"], s["at_targets"]), (30, 20))
        out = self.sb.out
        man = json.loads(read(os.path.join(out, "archive-manifest.json")))
        self.assertEqual(man["zip"]["name"], ZIP_NAME)
        for f in man["files"]:
            self.assertEqual(os.path.getsize(os.path.join(out, f["title"])), f["bytes"])
        self.assertTrue(os.path.isfile(os.path.join(out, "network-map-2026-11-01.csv")))
        self.assertFalse([f for f in os.listdir(out) if f.endswith(".refresh-tmp")])
        log = read(os.path.join(self.sb.state, "refresh.log"))
        self.assertIn("skipped 1 unsafe entries", log)

        rc, s, err = self.sb.run()
        self.assertEqual((rc, s["status"]), (0, "no change"), err)

        stale = os.path.join(out, "index", "network-index-Z.json")
        with open(stale, "w") as f:
            f.write("{}")
        rc, s, err = self.sb.run("--force", via_sh=True)
        self.assertEqual((rc, s["status"], s["superseded"]), (0, "published", 1), err)
        self.assertFalse(os.path.exists(stale))
        moved = [os.path.join(dp, f) for dp, _, fs in os.walk(os.path.join(out, "_superseded")) for f in fs]
        self.assertEqual([os.path.basename(m) for m in moved], ["network-index-Z.json"])

        lookup = load("lookup")
        r = lookup.find(out, "Orbitwell")
        self.assertEqual([p["n"] for p in r["people"]], ["Hana Sato", "Ruben Ortiz"])

    def test_bigger_zip_in_the_same_month_triggers_a_rebuild(self):
        rc, s, _ = self.sb.run()
        self.assertEqual(s["status"], "published")
        p = os.path.join(self.sb.exports, "2026-11", ZIP_NAME)
        with zipfile.ZipFile(p, "a") as z:
            z.writestr("Profile.csv", b"x" * 500)
        rc, s, _ = self.sb.run()
        self.assertEqual((rc, s["status"]), (0, "published"))

    def test_month_without_zip_falls_back(self):
        shutil.rmtree(os.path.join(self.sb.exports, "2026-11"))
        os.makedirs(os.path.join(self.sb.exports, "2026-12"))
        rc, s, err = self.sb.run()
        self.assertEqual((rc, s["status"], s["export_date"]), (0, "published", "2026-10-01"), err)

    def test_failures_are_reported_not_published(self):
        os.remove(os.path.join(self.sb.out, "targets.txt"))
        rc, s, _ = self.sb.run()
        self.assertEqual((rc, s["status"]), (1, "failed"))
        self.assertIn("targets file not found", s["error"])
        self.assertFalse(os.path.exists(os.path.join(self.sb.out, "archive-manifest.json")))

        env = self.sb.env()
        del env["LINKEDIN_MAP_OUT_DIR"]
        rc, s, _ = self.sb.run(env=env)
        self.assertEqual(rc, 2)
        self.assertIn("LINKEDIN_MAP_OUT_DIR", s["error"])

    def test_second_run_while_locked_exits_busy(self):
        os.makedirs(self.sb.state)
        with open(os.path.join(self.sb.state, ".refresh.lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            rc, s, _ = self.sb.run()
        self.assertEqual((rc, s["status"]), (0, "busy"))


class Extraction(unittest.TestCase):
    def test_only_connections_csv_is_extracted(self):
        d = tempfile.mkdtemp(prefix="lnm-zip-")
        try:
            zp = os.path.join(d, "a.zip")
            with zipfile.ZipFile(zp, "w") as z:
                z.writestr("Export/Connections.csv", b"First Name,Last Name\n")
                z.writestr("/abs/Connections.csv", b"no")
                z.writestr("messages.csv", b"no")
                link = zipfile.ZipInfo("link/Connections.csv")
                link.external_attr = (0o120777 << 16)
                z.writestr(link, "/etc/passwd")
            dest = os.path.join(d, "x")
            got = refresh.extract_connections(zp, dest, lambda *a: None)
            self.assertEqual(os.listdir(dest), ["Connections.csv"])
            self.assertEqual(read(got), "First Name,Last Name\n")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_export_date_sources(self):
        self.assertEqual(refresh.export_date_of({"name": "Basic_LinkedInDataExport_03-15-2026.zip", "mtime": 0}),
                         ("2026-03-15", "zip name"))
        self.assertEqual(refresh.export_date_of({"name": "linkedin 2026-04-02.zip", "mtime": 0}),
                         ("2026-04-02", "zip name"))
        self.assertEqual(refresh.export_date_of({"name": "export.zip", "mtime": 0})[1], "zip mtime")


class LaunchdTemplate(unittest.TestCase):
    def test_template_renders_to_a_valid_plist(self):
        text = read(os.path.join(REPO, "launchd", "local.linkedin-network-map.plist.template"))
        for k, v in {"__REPO_DIR__": "/opt/lnm", "__EXPORTS_DIR__": "/data/LinkedIn exports",
                     "__OUT_DIR__": "/data/network", "__STATE_DIR__": "/data/state"}.items():
            self.assertIn(k, text)
            text = text.replace(k, v)
        self.assertNotIn("__", text.split("-->", 1)[1])
        pl = plistlib.loads(text.encode("utf-8"))
        self.assertEqual(pl["ProgramArguments"], ["/bin/bash", "/opt/lnm/refresh.sh"])
        self.assertEqual(pl["EnvironmentVariables"]["LINKEDIN_MAP_EXPORTS_DIR"], "/data/LinkedIn exports")
        self.assertEqual((pl["StartCalendarInterval"]["Hour"], pl["StartCalendarInterval"]["Minute"]), (10, 15))

    def test_shell_wrapper_parses_and_scripts_have_no_home_paths(self):
        p = subprocess.run(["bash", "-n", os.path.join(REPO, "refresh.sh")], stderr=subprocess.PIPE)
        self.assertEqual(p.returncode, 0)
        for f in ("refresh.sh", "refresh.py", "build_network_index.py", "lookup.py",
                  "launchd/local.linkedin-network-map.plist.template"):
            self.assertNotIn("/Users/", read(os.path.join(REPO, f)), f)


if __name__ == "__main__":
    unittest.main()
