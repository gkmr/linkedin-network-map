#!/usr/bin/env python3
"""lookup.py: list your connections at one company from a built index, following the manifest's lookup rule.
It opens only the one index file the rule points to, the same way an agent would.

Usage: python3 lookup.py --index OUT_DIR "Company Name" [--json]
Exit 0 when the company is in the index, 1 when it is not.
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _builder():
    sys.dont_write_bytecode = True                # no __pycache__ next to the scripts
    spec = importlib.util.spec_from_file_location("build_network_index", os.path.join(HERE, "build_network_index.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bni = _builder()


def find(index_dir, company):
    """Returns dict(key, letter, file, people, nearby). file is None when no part covers the key."""
    with open(os.path.join(index_dir, "network-index-manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    key = bni.norm_company_key(company) or "(blank)"
    letter = bni.letter_of(key)
    rows = [(t, v) for t, v in man["files"].items() if v["letter"] == letter]
    pick = None
    for title, v in rows:
        if v["part"] == 0 or v["first_company"] <= key <= v["last_company"]:
            pick = title
            break
    people, nearby = [], []
    near = pick
    if not near:                     # names that start with the key can sit at the start of the next part
        later = [t for t, v in rows if v["first_company"] > key]
        near = later[0] if later else (rows[-1][0] if rows else None)
    if near:
        with open(os.path.join(index_dir, "index", near), encoding="utf-8") as f:
            d = json.load(f)
        people = d.get(key, []) if pick else []
        if not people:
            nearby = [k for k in d if key in k][:10]
    return {"company": company, "key": key, "letter": letter, "file": pick, "people": people, "nearby": nearby}


def main(argv=None):
    ap = argparse.ArgumentParser(description="List connections at a company from a built network index.")
    ap.add_argument("company")
    ap.add_argument("--index", required=True, help="folder that holds network-index-manifest.json")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args(argv)
    r = find(args.index, args.company)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["people"] else 1
    if not r["people"]:
        print('No connections filed under "%s" (letter %s, file %s).' % (r["key"], r["letter"], r["file"] or "none"))
        if r["nearby"]:
            print("Nearby keys that contain it: " + ", ".join(r["nearby"]))
        return 1
    print('%s: key "%s", file index/%s, %d connection%s, best path first'
          % (args.company, r["key"], r["file"], len(r["people"]), "" if len(r["people"]) == 1 else "s"))
    for p in r["people"]:
        line = "  %3d  %s - %s" % (p["s"], p["n"], p["t"] or "no title")
        if p.get("r"):
            line += "  (company: %s)" % p["r"]
        if p.get("e"):
            line += "  <%s>" % p["e"]
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
