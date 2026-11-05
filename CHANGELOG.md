# Changelog

## 1.0.0 (2026-11-05)
First public version, rebuilt from my private scripts.
- Python 3 standard library only. Targets moved from YAML to a plain-text file (`examples/targets.example.txt`).
- Every path comes from environment variables or flags; a launchd plist template replaces my own plist.
- Rows that map to a target are filed under the target's name in the index, so "Ex-X, now Lumen Harbor" is found by a lookup for Lumen Harbor. Each person record gains `s` (score) and `r` (raw company, when it differs).
- `refresh.py` extracts only `Connections.csv`, to a fixed name, instead of the whole archive.
- The phone guard moved into the builder, and `refresh.py` runs an independent output check before it publishes.
- New `lookup.py`, which follows the manifest's lookup rule.
- Deterministic output, checked byte for byte against `examples/expected/`. 27 tests.
- Left out of the public version: message recency, job applications, invitations, a profile snapshot and a phone book built from a contacts export.

## 0.x (2026-10, private)
- `build_network_index.py` v3.1 and `refresh.py` v1.1, YAML targets, output published into a synced notes folder for an agent to read.
