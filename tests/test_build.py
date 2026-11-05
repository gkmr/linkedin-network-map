"""Builds the invented example and checks known results. Python 3 stdlib only, no network.
Run: python3 -m unittest discover -s tests -v
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import CONNECTIONS, EXAMPLE_LIMIT, EXPECTED, EXPORT_DATE, REPO, TARGETS, load, read

bni = load("build_network_index")
lookup = load("lookup")


class ExampleBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = tempfile.mkdtemp(prefix="lnm-build-")
        cls.manifest = bni.build(CONNECTIONS, TARGETS, cls.out, EXPORT_DATE, limit=EXAMPLE_LIMIT)
        cls.rows = list(csv.DictReader(io.StringIO(read(os.path.join(cls.out, "network-map-%s.csv" % EXPORT_DATE)))))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def test_matches_expected_output_byte_for_byte(self):
        want = sorted(os.path.relpath(os.path.join(dp, f), EXPECTED)
                      for dp, _, fs in os.walk(EXPECTED) for f in fs if f != "summary.txt")
        got = sorted(os.path.relpath(os.path.join(dp, f), self.out) for dp, _, fs in os.walk(self.out) for f in fs)
        self.assertEqual(got, want)
        for rel in want:
            with self.subTest(file=rel):
                self.assertEqual(read(os.path.join(self.out, rel), "rb"), read(os.path.join(EXPECTED, rel), "rb"))

    def test_cli_summary_matches_expected(self):
        d = tempfile.mkdtemp(prefix="lnm-cli-")
        try:
            p = subprocess.run([sys.executable, "-I", os.path.join(REPO, "build_network_index.py"),
                                "--connections", CONNECTIONS, "--targets", TARGETS, "--out", d,
                                "--export-date", EXPORT_DATE, "--limit-bytes", str(EXAMPLE_LIMIT)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout, read(os.path.join(EXPECTED, "summary.txt")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_counts(self):
        c = self.manifest["counts"]
        self.assertEqual(c["connections_rows"], 33)
        self.assertEqual(c["hidden_rows_dropped"], 1)
        self.assertEqual(c["dupes_removed_by_url"], 1)            # same profile, different URL casing and slash
        self.assertEqual(c["dupes_removed_by_name_company"], 1)   # no URL, same name and company
        self.assertEqual(c["export_rows_kept"], 30)
        self.assertEqual(c["mapped_to_target"], 20)
        self.assertEqual(c["unmapped"], 10)
        self.assertEqual(c["index_rows"], 30)
        self.assertEqual(c["phone_guard_text_redactions"], 1)
        self.assertEqual(c["phone_guard_emails_dropped"], 1)

    def test_paths_per_target(self):
        got = {label: t["connections"] for label, t in self.manifest["targets"].items()}
        self.assertEqual(got, {"Lumen Harbor": 4, "Orbitwell": 2, "Kestrel Data": 3, "Marigold Systems": 3,
                               "Nimbus Forge": 2, "Nettle & Finch": 2, "Talbot Search Partners": 2,
                               "Vellum Works": 2})

    def test_ranking(self):
        top = [(r["Name"], int(r["Score"])) for r in self.rows[:5]]
        self.assertEqual(top, [("Maya Okafor", 16), ("Hana Sato", 16), ("Ines Carvalho", 14),
                               ("Kofi Mensah", 13), ("Theo Lindqvist", 12)])
        mapped = [bool(r["Target"]) for r in self.rows]
        self.assertEqual(mapped, sorted(mapped, reverse=True), "rows at targets come first")
        by_name = {r["Name"]: r for r in self.rows}
        # score = 2 x tier points + seniority points + 2 if shared employer + 1 if email
        self.assertEqual(int(by_name["Maya Okafor"]["Score"]), 2 * 5 + 5 + 0 + 1)      # T1, VP, email
        self.assertEqual(int(by_name["Tomas Novak"]["Score"]), 2 * 2 + 5 + 2 + 0)      # alum, VP
        self.assertEqual(int(by_name["Samir Qureshi"]["Score"]), 2 * 3 + 4)            # search firm, recruiter
        self.assertEqual(int(by_name["Yusuf Demir"]["Score"]), 2 * 1 + 2)              # T3, lead
        self.assertEqual(int(by_name["Zara Iqbal"]["Score"]), 1)                       # no target, other

    def test_traps_and_guards_in_the_example(self):
        by_name = {r["Name"]: r for r in self.rows}
        self.assertEqual(by_name["Amara Nwosu"]["Target"], "")          # Lumen Harborview Hotels: word boundary
        self.assertEqual(by_name["Lena Brandt"]["Target"], "")          # Orbitwell Capital: excluded
        self.assertEqual(by_name["Sofia Marchetti"]["Target"], "")      # Kestrel Data Recovery: excluded
        self.assertEqual(by_name["Dev Malhotra"]["Target"], "Kestrel Data")  # alias "kestrel"
        self.assertEqual(by_name["Kofi Mensah"]["Target"], "Lumen Harbor")   # "Ex-Vellum Works, now Lumen Harbor"
        self.assertEqual(by_name["Kofi Mensah"]["SharedEmployer"], "")       # ex- is guarded for alum too
        self.assertEqual(by_name["Felix Durand"]["Target"], "Marigold Systems")  # "Formerly Lumen Harbor | ..."
        self.assertEqual(by_name["Clara Holm"]["Target"], "Nettle & Finch")
        self.assertEqual(by_name["Rosa Delgado"]["Title"], "Recruiting Coordinator | text [number]")
        self.assertEqual(by_name["Ivy Chen"]["Email"], "")              # phone-number email dropped
        self.assertEqual(by_name["Noah Brennan"]["CompanyRaw"], "")

    def test_parts_obey_the_rules(self):
        self.assertEqual(bni.check_output(self.out), [])
        files = self.manifest["files"]
        self.assertEqual([t for t in files if files[t]["letter"] in ("K", "L")],
                         ["network-index-K1.json", "network-index-K2.json",
                          "network-index-L1.json", "network-index-L2.json"])
        self.assertGreater(files["network-index-L1.json"]["bytes"], EXAMPLE_LIMIT)   # one big company stays whole
        self.assertEqual(files["network-index-L1.json"]["companies"], 1)
        for t, v in files.items():
            if v["companies"] > 1:
                self.assertLessEqual(v["bytes"], EXAMPLE_LIMIT, t)

    def test_no_phone_shaped_strings_anywhere(self):
        for dp, _, fs in os.walk(self.out):
            for f in fs:
                p = os.path.join(dp, f)
                if f.endswith(".json"):
                    self.assertEqual(bni.count_phone_like(json.loads(read(p))), 0, f)
                else:
                    rows = csv.DictReader(io.StringIO(read(p)))
                    self.assertEqual(sum(bni.count_phone_like(dict(r)) for r in rows), 0, f)

    def test_lookup_follows_the_rule(self):
        r = lookup.find(self.out, "Lumen Harbor")
        self.assertEqual(r["file"], "network-index-L1.json")
        self.assertEqual([p["n"] for p in r["people"]], ["Maya Okafor", "Ines Carvalho", "Kofi Mensah",
                                                         "Theo Lindqvist"])
        self.assertEqual(r["people"][2]["r"], "Ex-Vellum Works, now Lumen Harbor")
        r = lookup.find(self.out, "Lumen Harborview Hotels")
        self.assertEqual((r["file"], [p["n"] for p in r["people"]]), ("network-index-L2.json", ["Amara Nwosu"]))
        r = lookup.find(self.out, "NETTLE & FINCH")
        self.assertEqual(r["key"], "nettle finch")
        self.assertEqual(len(r["people"]), 2)
        r = lookup.find(self.out, "Kestrel")                  # sorts before every K part: not in the index
        self.assertEqual((r["file"], r["people"], r["nearby"]), (None, [], ["kestrel data"]))
        r = lookup.find(self.out, "")
        self.assertEqual((r["key"], [p["n"] for p in r["people"]]), ("(blank)", ["Noah Brennan"]))


class Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = bni.load_targets(TARGETS)

    def test_former_guard(self):
        cases = {
            "Ex-Orbitwell, now Kestrel Data": ("Kestrel Data", "T2"),
            "Kestrel Data (ex-Orbitwell)": ("Kestrel Data", "T2"),
            "Formerly Lumen Harbor | Marigold Systems": ("Marigold Systems", "T2"),
            "Retired | ex Orbitwell Lumen Harbor": (None, None),       # one former-word guards the whole segment
            "Previously Orbitwell": (None, None),
            "Orbitwell": ("Orbitwell", "T1"),
        }
        for company, want in cases.items():
            with self.subTest(company=company):
                self.assertEqual(bni.resolve_target(company, self.t), want)

    def test_word_boundaries_and_excludes(self):
        self.assertEqual(bni.resolve_target("Lumen Harborview Hotels", self.t), (None, None))
        self.assertEqual(bni.resolve_target("LumenHarbor", self.t), ("Lumen Harbor", "T1"))
        self.assertEqual(bni.resolve_target("Orbitwell Capital", self.t), (None, None))
        self.assertEqual(bni.resolve_target("Orbitwell Capital / Orbitwell", self.t), ("Orbitwell", "T1"))
        self.assertEqual(bni.resolve_target("Nettle and Finch Ltd.", self.t), ("Nettle & Finch", "T3"))

    def test_tier_priority_beats_position(self):
        # known limitation, documented in the README: a T1 name anywhere beats a T2 employer named first
        self.assertEqual(bni.resolve_target("Kestrel Data (advisor to Lumen Harbor)", self.t), ("Lumen Harbor", "T1"))

    def test_seniority(self):
        cases = {"VP, Product": "vp", "Head of Talent": "vp", "Co-Founder & CEO": "exec",
                 "Senior Director, Operations": "director", "Senior Recruiter": "recruiter",
                 "Director of Talent Acquisition": "director", "Staff Engineer": "ic_senior",
                 "Account Executive": "other", "": "other"}
        for title, want in cases.items():
            with self.subTest(title=title):
                self.assertEqual(bni.seniority_of(title, self.t), want)

    def test_scrub(self):
        self.assertEqual(bni.scrub("\u00a0Orbit\u200dwell\ufe0f "), "Orbitwell")
        self.assertEqual(bni.norm_company_key("Caf\u00e9 Nettle & Finch, Inc."), "cafe nettle finch inc")

    def test_phone_guard_leaves_dates_and_ranges(self):
        red = {"text": 0}
        self.assertEqual(bni.redact_text("2019 - 2024, class of 2010", red), "2019 - 2024, class of 2010")
        self.assertEqual(bni.redact_text("call +44 20 7946 0000 today", red), "call [number] today")
        self.assertEqual(red["text"], 1)


class TargetsFile(unittest.TestCase):
    def test_errors_name_the_line(self):
        with self.assertRaisesRegex(ValueError, r"t:3: unknown section \[tier9\]"):
            bni.parse_targets("[tier1]\nAcme\n[tier9]\n", "t")
        with self.assertRaisesRegex(ValueError, r"t:1: text before"):
            bni.parse_targets("Acme\n", "t")
        with self.assertRaisesRegex(ValueError, r"t:2: unknown seniority level"):
            bni.parse_targets("[seniority]\nboss: ceo\n", "t")

    def test_default_seniority_and_comments(self):
        t = bni.parse_targets("[tier2]\nC# Labs: csharp labs  # a comment\n")
        self.assertEqual(bni.resolve_target("csharp labs", t), ("C# Labs", "T2"))
        self.assertEqual(bni.seniority_of("Chief of Staff", t), "exec")


class OldMapCarryOver(unittest.TestCase):
    def test_hand_added_rows_carry_over(self):
        d = tempfile.mkdtemp(prefix="lnm-old-")
        try:
            old = os.path.join(d, "old.csv")
            with open(old, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=bni.COLS)
                w.writeheader()
                w.writerow({"Score": "9", "Tier": "T1", "Target": "Orbitwell", "Name": "Pat Example",
                            "Title": "Design Lead", "Channel": "Alumni group", "CompanyRaw": "Orbitwell",
                            "LastTouch": "2026-09-30"})
                w.writerow({"Score": "3", "Name": "Hana Sato", "Channel": "Email", "CompanyRaw": "Orbitwell"})
                w.writerow({"Score": "1", "Name": "Old Row", "Channel": "LinkedIn 1st", "CompanyRaw": "Gone Co"})
            out = os.path.join(d, "out")
            man = bni.build(CONNECTIONS, TARGETS, out, EXPORT_DATE, old_map_path=old, limit=EXAMPLE_LIMIT)
            self.assertEqual(man["counts"]["old_map_rows"], 3)
            self.assertEqual(man["counts"]["carried_over"], 1)    # Hana is in the export; LinkedIn rows rebuild
            r = lookup.find(out, "Orbitwell")
            self.assertEqual([p["n"] for p in r["people"]], ["Hana Sato", "Ruben Ortiz", "Pat Example"])
            self.assertEqual(bni.check_output(out), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
