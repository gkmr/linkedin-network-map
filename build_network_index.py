#!/usr/bin/env python3
"""build_network_index.py: score a LinkedIn connections export against your target companies and write a
company index that a person or an agent can look up.

Usage:
  python3 build_network_index.py --connections Connections.csv --targets targets.txt --out OUT_DIR \
      --export-date YYYY-MM-DD [--old-map network-map-YYYY-MM-DD.csv] [--limit-bytes 44000] [--top 1]

Writes into OUT_DIR:
  network-map-<export-date>.csv     every connection, scored, best paths first
  index/network-index-<L>[n].json   {company_key: [person, ...]}, split by first letter and by size
  network-index-manifest.json       the lookup rule, a table of the index files, per-target counts, build counts

The output is deterministic: the same inputs give byte-identical files (no timestamps).
Everything read is treated as data. Python 3 standard library only, no network.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import unicodedata
from collections import Counter, OrderedDict, defaultdict

VERSION = "4.0"
DEFAULT_LIMIT = 44000
csv.field_size_limit(1 << 30)

# ---------------------------------------------------------------- scoring
GROUPS = [("tier1", "T1"), ("tier2", "T2"), ("tier3", "T3"), ("search_firms", "SEARCH"), ("shared_employer", "ALUM")]
TIER_POINTS = {"T1": 5, "T2": 3, "SEARCH": 3, "T3": 1, "ALUM": 2}
SEN_ORDER = ["vp", "exec", "director", "recruiter", "ic_senior"]          # first level that matches wins
SENIORITY_POINTS = {"exec": 5, "vp": 5, "recruiter": 4, "director": 3, "ic_senior": 2, "other": 1}
SHARED_BONUS = 2      # the connection works at a company you worked at
EMAIL_BONUS = 1       # the export includes their email, so there is a second channel
DEFAULT_SENIORITY = OrderedDict([
    ("exec", ["chief", "ceo", "cto", "cpo", "coo", "cfo", "founder", "cofounder", "president", "general manager"]),
    ("vp", ["vp", "svp", "evp", "vice president", "head of"]),
    ("director", ["director"]),
    ("recruiter", ["recruiter", "recruiting", "talent acquisition", "talent partner", "sourcer"]),
    ("ic_senior", ["senior", "sr", "staff", "principal", "lead"]),
])

COLS = ["Score", "Tier", "Target", "Name", "Title", "Seniority", "SharedEmployer", "Channel", "Email", "LinkedIn",
        "ConnectedOn", "CompanyRaw", "Status", "LastTouch"]
PERSON_FIELDS = OrderedDict([
    ("n", "name"), ("t", "title"), ("s", "score (higher is a better path)"), ("u", "LinkedIn URL"),
    ("c", "connected-on date"), ("e", "email or empty"),
    ("r", "the raw company text, only when it differs from the key (for example 'Ex-X, now Y')"),
])
LOOKUP_RULE = (
    "Normalize the company name: Unicode NFKD, drop combining marks, lowercase, turn every run of characters "
    "outside a-z and 0-9 into one space, trim (so 'Nettle & Finch' becomes 'nettle finch'). The first character "
    "of that key gives the letter: A-Z, or 0 for anything else. Take the rows of 'files' with that letter. Part 0 "
    "means the whole letter is one file. Otherwise pick the part where first_company <= key <= last_company in "
    "plain string order. Open index/<file>. If no part covers the key, the company is not in the index. "
    "A connection whose company maps to a target is filed under the target's name; any other connection is "
    "filed under its own company, and a blank company is '(blank)' under letter 0. Within a company, people are "
    "listed best path first. A single company bigger than the size limit gets a part to itself.")

# ---------------------------------------------------------------- text helpers
_VARIATION_SELECTORS = re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def scrub(s):
    """Transport-safe text: no-break space becomes a space; drop format characters (Cf: zero-width joiner, soft
    hyphen, BOM), private-use glyphs (Co) and emoji variation selectors. Letters, digits, emoji and real
    combining marks stay. Then strip."""
    s = (s or "").replace("\xa0", " ")
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cf", "Co"))
    return _VARIATION_SELECTORS.sub("", s).strip()


def _fold(s):
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def norm(s):
    """Matching form: lowercase, keep a-z 0-9 space & and dot, everything else becomes a space, collapse."""
    s = re.sub(r"[^a-z0-9 &.]+", " ", _fold(s))
    return re.sub(r"\s+", " ", s).strip()


def norm_company_key(s):
    """Index key: lowercase, every run outside a-z0-9 becomes one space, trim."""
    s = re.sub(r"[^a-z0-9]+", " ", _fold(s))
    return re.sub(r"\s+", " ", s).strip()


def letter_of(key):
    ch = key[:1]
    return ch.upper() if ch.isalpha() and ch.isascii() else "0"


def word_re(alias):
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


FORMER = {"ex", "former", "formerly", "previously", "retired", "past", "prev", "formally"}
SEG_SPLIT = re.compile(r"[|,;/()\[\]\u2022\u00b7\u2192>]|\s[-\u2013\u2014]\s|\b(?:now|currently|presently|joined)\b",
                       re.I)


def segments(company):
    """Split a raw Company string into normalized segments. A former-word earlier in the same segment marks every
    alias after it as former: 'Ex Acme Globex' guards both, 'ex-Acme, now Globex' keeps Globex."""
    return [n for n in (norm(seg) for seg in SEG_SPLIT.split(company or "")) if n]


def former_guarded(seg_text, start):
    return any(t in FORMER for t in seg_text[:start].split())


def dump(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def nbytes(text):
    return len(text.encode("utf-8"))


def int_or0(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- phone guard
PHONE_RE = re.compile(r"(?<![\w/.])(?:\+?\d[\d\s().-]{6,}\d)(?![\w/])")
URL_KEYS = {"u", "LinkedIn"}
EMAIL_KEYS = {"e", "Email"}


def _digits(s):
    return sum(ch.isdigit() for ch in s)


def redact_text(s, red):
    """A run of 9 or more digits that looks like a phone number becomes [number]. Year ranges and dates stay."""
    def rep(m):
        if _digits(m.group(0)) >= 9:
            red["text"] += 1
            return "[number]"
        return m.group(0)
    return PHONE_RE.sub(rep, s or "")


def clean_email(s, red):
    """Drop an email whose local part is a phone number (an SMS gateway address, for example)."""
    s = (s or "").strip()
    if s and _digits(s.split("@", 1)[0]) >= 9:
        red["emails"] += 1
        return ""
    return s


def count_phone_like(o, key=None):
    """Phone-shaped strings left in a parsed JSON value or a CSV row dict. URL fields are skipped."""
    if isinstance(o, dict):
        return sum(count_phone_like(k) + count_phone_like(v, k) for k, v in o.items())
    if isinstance(o, list):
        return sum(count_phone_like(v, key) for v in o)
    if isinstance(o, str):
        if key in URL_KEYS:
            return 0
        if key in EMAIL_KEYS:
            return 1 if _digits(o.split("@", 1)[0]) >= 9 else 0
        return sum(1 for m in PHONE_RE.finditer(o) if _digits(m.group(0)) >= 9)
    return 0


# ---------------------------------------------------------------- targets file
class Targets:
    def __init__(self, groups, exclude, seniority):
        self.groups = groups                      # group -> OrderedDict(label -> [aliases])
        self.exclude = exclude
        self.seniority = seniority
        self.tables = []                          # (group, tier, [(label, alias_norm, regex)])
        for group, tier in GROUPS:
            tbl = []
            for label, aliases in groups[group].items():
                for a in [label] + aliases:
                    an = norm(a)
                    if an and all(an != x[1] or label != x[0] for x in tbl):
                        tbl.append((label, an, word_re(an)))
            self.tables.append((group, tier, tbl))
        self.shared_table = self.tables[-1][2]
        self.exclude_re = [word_re(norm(x)) for x in exclude if norm(x)]
        self.sen_re = OrderedDict((lvl, [word_re(norm(k)) for k in seniority.get(lvl, []) if norm(k)])
                                  for lvl in SEN_ORDER)

    def labels(self):
        """[(label, tier)] in priority order."""
        return [(label, tier) for group, tier in GROUPS for label in self.groups[group]]


def parse_targets(text, source="targets"):
    """Plain-text targets file. Sections: [tier1] [tier2] [tier3] [search_firms] [shared_employer] hold lines of
    'Label: alias, alias' (the label is an alias too); [exclude] holds phrases to blank out before matching;
    [seniority] holds 'level: keyword, keyword' for exec, vp, director, recruiter, ic_senior. '#' starts a comment."""
    groups = OrderedDict((g, OrderedDict()) for g, _ in GROUPS)
    exclude, seniority, have_sen = [], OrderedDict((lvl, []) for lvl in SEN_ORDER), False
    section = None
    for n, raw in enumerate(text.splitlines(), 1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        m = re.fullmatch(r"\[\s*([a-z0-9_]+)\s*\]", line)
        if m:
            section = m.group(1)
            if section not in groups and section not in ("exclude", "seniority"):
                raise ValueError("%s:%d: unknown section [%s]" % (source, n, section))
            have_sen = have_sen or section == "seniority"
            continue
        if section is None:
            raise ValueError("%s:%d: text before the first [section]" % (source, n))
        if section == "exclude":
            exclude += [x.strip() for x in line.split(",") if x.strip()]
            continue
        label, _, rest = line.partition(":")
        label = label.strip()
        aliases = [x.strip() for x in rest.split(",") if x.strip()]
        if not label:
            raise ValueError("%s:%d: missing label" % (source, n))
        if section == "seniority":
            if label not in seniority:
                raise ValueError("%s:%d: unknown seniority level %r (use %s)" % (source, n, label, ", ".join(SEN_ORDER)))
            seniority[label] += aliases
        else:
            groups[section].setdefault(label, []).extend(aliases)
    if not have_sen:
        seniority = OrderedDict((k, list(v)) for k, v in DEFAULT_SENIORITY.items())
    return Targets(groups, exclude, seniority)


def load_targets(path):
    with open(path, encoding="utf-8") as f:
        return parse_targets(f.read(), os.path.basename(path))


# ---------------------------------------------------------------- matching
def _scrub_excludes(c, targets):
    for rx in targets.exclude_re:
        c = rx.sub(lambda m: " " * len(m.group(0)), c)
    return c


def company_segments(company, targets):
    return [_scrub_excludes(s, targets) for s in segments(company)]


def resolve_in_table(segs, tbl):
    """First alias, by segment then position, that is not former-guarded."""
    best = None
    for si, c in enumerate(segs):
        for label, an, rx in tbl:
            for m in rx.finditer(c):
                if former_guarded(c, m.start()):
                    continue
                if best is None or (si, m.start()) < best[0]:
                    best = ((si, m.start()), label)
                break
        if best and best[0][0] == si:
            break
    return best[1] if best else None


def resolve_target(company, targets):
    """(label, tier) of the highest-priority group that matches, or (None, None)."""
    segs = company_segments(company, targets)
    if not any(s.strip() for s in segs):
        return None, None
    for group, tier, tbl in targets.tables:
        hit = resolve_in_table(segs, tbl)
        if hit:
            return hit, tier
    return None, None


def shared_employer(company, targets):
    return resolve_in_table(company_segments(company, targets), targets.shared_table) or ""


def seniority_of(position, targets):
    p = norm(position)
    for lvl in SEN_ORDER:
        if any(rx.search(p) for rx in targets.sen_re[lvl]):
            return lvl
    return "other"


def score_of(tier, seniority, shared, email):
    return (TIER_POINTS.get(tier, 0) * 2 + SENIORITY_POINTS[seniority]
            + (SHARED_BONUS if shared else 0) + (EMAIL_BONUS if email else 0))


def li_slug(url):
    m = re.search(r"linkedin\.com/(?:in|pub)/([^/?#]+)", (url or "").lower())
    return m.group(1).rstrip("/") if m else ""


# ---------------------------------------------------------------- reading
def read_csv_text(text, header_prefix=None):
    if header_prefix:
        i = text.find(header_prefix)
        if i > 0:
            text = text[i:]               # LinkedIn puts a "Notes:" preamble above the header
    return [{k.strip(): scrub(v) for k, v in r.items() if k is not None}
            for r in csv.DictReader(io.StringIO(text))]


def read_connections(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        return read_csv_text(f.read(), "First Name,")


def read_map(path):
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        return list(csv.DictReader(io.StringIO(f.read())))


# ---------------------------------------------------------------- build
def build_map(connections, targets, old_rows=None):
    """Score every connection. Returns (rows sorted best first, stats, phone-guard counter)."""
    stats = OrderedDict()
    red = Counter()
    seen_url, seen_nc = set(), set()
    dup_url = dup_nc = hidden = 0
    rows = []
    for r in connections:
        name = ("%s %s" % (r.get("First Name", ""), r.get("Last Name", ""))).strip()
        url = r.get("URL", "")
        company = r.get("Company", "")
        pos = r.get("Position", "")
        if not name and not url:
            hidden += 1                   # LinkedIn hides some connections: only Connected On is filled
            continue
        key_u, key_nc = li_slug(url), (norm(name), norm(company))
        if key_u:
            if key_u in seen_url:
                dup_url += 1
                continue
            seen_url.add(key_u)
        elif key_nc in seen_nc:
            dup_nc += 1
            continue
        seen_nc.add(key_nc)
        label, tier = resolve_target(company, targets)
        sen = seniority_of(pos, targets)
        shared = shared_employer(company, targets)
        email = clean_email(r.get("Email Address", ""), red)
        rows.append({
            "Score": score_of(tier, sen, shared, email), "Tier": tier or "", "Target": label or "",
            "Name": redact_text(name, red), "Title": redact_text(pos, red), "Seniority": sen,
            "SharedEmployer": shared, "Channel": "LinkedIn 1st", "Email": email, "LinkedIn": url,
            "ConnectedOn": r.get("Connected On", ""), "CompanyRaw": redact_text(company, red),
            "Status": "export", "LastTouch": "",
        })
    stats["connections_rows"] = len(connections)
    stats["hidden_rows_dropped"] = hidden
    stats["dupes_removed_by_url"] = dup_url
    stats["dupes_removed_by_name_company"] = dup_nc
    stats["export_rows_kept"] = len(rows)
    stats["rows_without_url"] = sum(1 for m in rows if not m["LinkedIn"])
    stats["mapped_to_target"] = sum(1 for m in rows if m["Target"])
    stats["unmapped"] = sum(1 for m in rows if not m["Target"])

    # rows you added by hand (any Channel that is not LinkedIn) carry over from the previous map
    carried = 0
    old_rows = old_rows or []
    for o in old_rows:
        if "linkedin" in (o.get("Channel") or "").lower():
            continue
        ku = li_slug(o.get("LinkedIn", ""))
        knc = (norm(o.get("Name", "")), norm(o.get("CompanyRaw", "")))
        if (ku and ku in seen_url) or knc in seen_nc:
            continue
        row = {c: (o.get(c) or "") for c in COLS}
        for c in COLS:
            if c in EMAIL_KEYS:
                row[c] = clean_email(row[c], red)
            elif c not in URL_KEYS:
                row[c] = redact_text(row[c], red)
        row["Score"] = int_or0(row["Score"])
        row["Status"] = row["Status"] or "carried"
        rows.append(row)
        if ku:
            seen_url.add(ku)
        seen_nc.add(knc)
        carried += 1
    stats["old_map_rows"] = len(old_rows)
    stats["carried_over"] = carried

    rows.sort(key=lambda x: (0 if x["Target"] else 1, -int_or0(x["Score"]), x["Target"], x["Name"]))
    stats["map_rows_total"] = len(rows)
    return rows, stats, red


def index_key(row):
    return norm_company_key(row["Target"]) if row["Target"] else (norm_company_key(row["CompanyRaw"]) or "(blank)")


def build_index(rows):
    """{company_key: [person, ...]} sorted by key; people keep the map's best-first order."""
    index = defaultdict(list)
    for m in rows:
        k = index_key(m)
        p = OrderedDict([("n", m["Name"]), ("t", m["Title"]), ("s", int_or0(m["Score"])), ("u", m["LinkedIn"]),
                         ("c", m["ConnectedOn"]), ("e", m["Email"])])
        if m["CompanyRaw"] and norm_company_key(m["CompanyRaw"]) != k:
            p["r"] = m["CompanyRaw"]
        index[k].append(p)
    return OrderedDict(sorted(index.items()))


def split_parts(index, limit):
    """One file per first letter when it fits in `limit` bytes (part 0); otherwise cut greedily by company, in
    key order. A single company bigger than the limit stays whole. Returns {title: (letter, part, dict)}."""
    by_letter = defaultdict(OrderedDict)
    for k in sorted(index):
        by_letter[letter_of(k)][k] = index[k]
    files = OrderedDict()
    for L in sorted(by_letter):
        d = by_letter[L]
        if nbytes(dump(d)) <= limit:
            files["network-index-%s.json" % L] = (L, 0, d)
            continue
        chunks, cur, size = [], OrderedDict(), 2
        for k, v in d.items():
            inner = nbytes(dump({k: v})) - 2              # '"key":[...]' without the braces
            grown = size + inner + (1 if cur else 0)       # plus a comma
            if cur and grown > limit:
                chunks.append(cur)
                cur, grown = OrderedDict(), 2 + inner
            cur[k] = v
            size = grown
        if cur:
            chunks.append(cur)
        for i, chunk in enumerate(chunks, 1):
            files["network-index-%s%d.json" % (L, i)] = (L, i, chunk)
    return files


PART_RE = re.compile(r"^network-index-(?:0|[A-Z])\d*\.json$")


def build(connections_path, targets_path, out_dir, export_date, old_map_path=None, limit=DEFAULT_LIMIT):
    """Build everything into out_dir. Returns the manifest (an OrderedDict)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", export_date or ""):
        raise ValueError("export date must be YYYY-MM-DD, got %r" % export_date)
    targets = load_targets(targets_path)
    connections = read_connections(connections_path)
    old_rows = read_map(old_map_path) if old_map_path else []
    rows, stats, red = build_map(connections, targets, old_rows)
    index = build_index(rows)
    files = split_parts(index, limit)

    idx_dir = os.path.join(out_dir, "index")
    os.makedirs(idx_dir, exist_ok=True)
    for f in os.listdir(idx_dir):                         # stale parts from an earlier build in this folder
        if PART_RE.match(f) and f not in files:
            os.remove(os.path.join(idx_dir, f))
    map_title = "network-map-%s.csv" % export_date
    with open(os.path.join(out_dir, map_title), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, lineterminator="\n")
        w.writeheader()
        for m in rows:
            w.writerow({c: m.get(c, "") for c in COLS})
    table = OrderedDict()
    for title, (L, part, d) in files.items():
        text = dump(d)
        with open(os.path.join(idx_dir, title), "w", encoding="utf-8") as f:
            f.write(text)
        keys = list(d)
        table[title] = OrderedDict([("letter", L), ("part", part), ("companies", len(keys)),
                                    ("rows", sum(len(v) for v in d.values())), ("bytes", nbytes(text)),
                                    ("first_company", keys[0]), ("last_company", keys[-1])])
    hits = Counter(m["Target"] for m in rows if m["Target"])
    targets_table = OrderedDict((label, OrderedDict([("tier", tier), ("key", norm_company_key(label)),
                                                     ("connections", hits.get(label, 0))]))
                                for label, tier in targets.labels())
    stats["index_companies"] = len(index)
    stats["index_files"] = len(files)
    stats["index_rows"] = sum(v["rows"] for v in table.values())
    stats["phone_guard_text_redactions"] = red["text"]
    stats["phone_guard_emails_dropped"] = red["emails"]
    manifest = OrderedDict([
        ("schema", "network-index-manifest v1"),
        ("generator", "build_network_index.py v%s" % VERSION),
        ("export_date", export_date),
        ("map_file", map_title),
        ("size_limit_bytes", limit),
        ("lookup_rule", LOOKUP_RULE),
        ("person_fields", PERSON_FIELDS),
        ("files", table),
        ("targets", targets_table),
        ("counts", stats),
    ])
    with open(os.path.join(out_dir, "network-index-manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    problems = check_output(out_dir)
    if problems:
        raise ValueError("output check failed: " + "; ".join(problems))
    return manifest


def check_output(out_dir):
    """Independent checks on a built folder. Returns a list of problems (empty means good)."""
    problems = []
    try:
        with open(os.path.join(out_dir, "network-index-manifest.json"), encoding="utf-8") as f:
            man = json.load(f, object_pairs_hook=OrderedDict)
    except (OSError, ValueError) as e:
        return ["manifest unreadable: %s" % e]
    limit = man.get("size_limit_bytes") or DEFAULT_LIMIT
    by_letter = defaultdict(list)
    rows = 0
    for title, v in man.get("files", {}).items():
        p = os.path.join(out_dir, "index", title)
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            d = json.loads(text, object_pairs_hook=OrderedDict)
        except (OSError, ValueError) as e:
            problems.append("%s unreadable: %s" % (title, e))
            continue
        keys = list(d)
        if not keys:
            problems.append("%s is empty" % title)
            continue
        if keys != sorted(keys):
            problems.append("%s keys are not sorted" % title)
        if any(letter_of(k) != v["letter"] for k in keys):
            problems.append("%s has a key outside letter %s" % (title, v["letter"]))
        if nbytes(text) > limit and len(keys) > 1:
            problems.append("%s is %d bytes (> %d) with %d companies" % (title, nbytes(text), limit, len(keys)))
        if (keys[0], keys[-1]) != (v["first_company"], v["last_company"]) or nbytes(text) != v["bytes"]:
            problems.append("%s does not match its manifest row" % title)
        if count_phone_like(d):
            problems.append("%s has phone-shaped strings" % title)
        rows += sum(len(x) for x in d.values())
        by_letter[v["letter"]].append(v)
    for L, parts in by_letter.items():
        parts.sort(key=lambda v: v["part"])
        if len(parts) > 1 and any(v["part"] == 0 for v in parts):
            problems.append("letter %s has a part 0 and numbered parts" % L)
        for a, b in zip(parts, parts[1:]):
            if not a["last_company"] < b["first_company"]:
                problems.append("letter %s parts %d and %d overlap" % (L, a["part"], b["part"]))
    map_path = os.path.join(out_dir, man.get("map_file", ""))
    try:
        map_rows = read_map(map_path)
    except OSError as e:
        return problems + ["map unreadable: %s" % e]
    if len(map_rows) != rows:
        problems.append("index has %d rows, map has %d" % (rows, len(map_rows)))
    if sum(count_phone_like(dict(r)) for r in map_rows):
        problems.append("map has phone-shaped strings")
    if count_phone_like(man):
        problems.append("manifest has phone-shaped strings")
    return problems


# ---------------------------------------------------------------- CLI
def summary_lines(manifest, out_dir, top=1):
    lines = []
    files = manifest["files"]
    by_key = {}
    for title in files:
        with open(os.path.join(out_dir, "index", title), encoding="utf-8") as f:
            by_key.update(json.load(f))
    for label, t in manifest["targets"].items():
        people = by_key.get(t["key"], [])
        head = "%-6s %-26s %2d path%s" % (t["tier"], label, len(people), "" if len(people) == 1 else "s")
        if people:
            best = "; ".join("%s, %s (%d)" % (p["n"], p["t"] or "no title", p["s"]) for p in people[:top])
            head += "  best: " + best
        lines.append(head)
    c = manifest["counts"]
    lines.append("%d connections kept of %d rows (%d hidden, %d duplicates); %d at targets; %d index files"
                 % (c["export_rows_kept"], c["connections_rows"], c["hidden_rows_dropped"],
                    c["dupes_removed_by_url"] + c["dupes_removed_by_name_company"], c["mapped_to_target"],
                    c["index_files"]))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a LinkedIn Connections.csv against target companies "
                                             "and write a company index.")
    ap.add_argument("--connections", required=True, help="Connections.csv from the LinkedIn data export")
    ap.add_argument("--targets", required=True, help="targets file (see examples/targets.example.txt)")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--export-date", required=True, help="date of the export, YYYY-MM-DD")
    ap.add_argument("--old-map", help="previous network-map CSV; rows with a non-LinkedIn Channel carry over")
    ap.add_argument("--limit-bytes", type=int, default=DEFAULT_LIMIT, help="max bytes per index file")
    ap.add_argument("--top", type=int, default=1, help="paths to show per target in the summary")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    try:
        manifest = build(args.connections, args.targets, args.out, args.export_date, args.old_map, args.limit_bytes)
    except (OSError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    for ln in summary_lines(manifest, args.out, args.top):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
