"""
Validate resolve_projekt_typ() (wp_sync.py) against a live WP baseline, WITHOUT
writing anything to WordPress.

Usage:
    python3 validate_projekt_typ.py --baseline data/projekt_typ_baseline.json

The baseline is a JSON list of {"post_id", "title", "expro_id", "projekt_typ"}
pulled live from WP (see fetch_projekt_typ_baseline() below, or run the
equivalent wp eval-file query manually). This script never touches WP —
it only compares data/expro_data.json (local scrape cache) through the
current classifier logic against that snapshot.

Exit code 0 = zero unexpected diffs, safe to ship the classifier change.
Exit code 1 = at least one live investment would change on next sync — stop
and either tighten the keyword list or add an explicit override.
"""
import argparse
import json
import sys

from wp_sync import detect_projekt_typ, resolve_projekt_typ, _PROJEKT_TYP_OVERRIDES


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/expro_data.json")
    ap.add_argument("--baseline", required=True, help="JSON snapshot of live WP projekt_typ per expro_id")
    args = ap.parse_args()

    data = json.load(open(args.data, encoding="utf-8"))
    baseline_list = json.load(open(args.baseline, encoding="utf-8"))
    by_expro_live = {str(b["expro_id"]): b["projekt_typ"] for b in baseline_list if b.get("expro_id")}

    checked = 0
    unexpected_diff = []
    would_change_from_old_behavior = []

    for inv in data:
        eid = str(inv["expro_id"])
        live = by_expro_live.get(eid)
        if live is None:
            continue  # not live yet / not in WP — nothing to compare against
        checked += 1

        old_resolved = _PROJEKT_TYP_OVERRIDES.get(eid) or detect_projekt_typ(inv)
        new_resolved = resolve_projekt_typ(eid, inv)

        if new_resolved != old_resolved:
            would_change_from_old_behavior.append((eid, inv.get("name"), old_resolved, new_resolved))

        # The real safety gate: does the NEW code, if it ran right now, write
        # something different from what's already live? (matches the `if value:`
        # write guard in wp_sync.py — empty string never gets written)
        if new_resolved and new_resolved != live:
            unexpected_diff.append((eid, inv.get("name"), new_resolved, live))

    print(f"Checked {checked} live investments (of {len(data)} in local cache).")
    print()
    print(f"Cases where new logic differs from OLD logic (expected — these are exactly")
    print(f"the ones the name-signal is meant to fix): {len(would_change_from_old_behavior)}")
    for eid, name, old, new in would_change_from_old_behavior:
        print(f"  {eid:6} {name!r:45} old={old!r:12} -> new={new!r}")

    print()
    print(f"UNEXPECTED diffs against live WP (new logic would write something that")
    print(f"doesn't match what's currently live — THIS MUST BE ZERO): {len(unexpected_diff)}")
    for eid, name, new, live in unexpected_diff:
        print(f"  {eid:6} {name!r:45} new={new!r:12} live={live!r}")

    if unexpected_diff:
        print("\nFAIL — do not ship. Tighten _NAME_TYP_PATTERNS or add an explicit override.")
        sys.exit(1)
    else:
        print("\nPASS — new resolution matches live WP for all currently-live investments.")
        sys.exit(0)


if __name__ == "__main__":
    main()
