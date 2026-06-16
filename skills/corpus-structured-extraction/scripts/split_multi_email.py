#!/usr/bin/env python3.13
"""Split multi-address strings extracted by an LLM into primary + extras.

Common defect when an LLM is asked for a single email field but the document
has multiple corresponding authors:

    "barkakatyb@ornl.gov or lokitzbs@ornl.gov"
    "xiangwang.chn@gmail.com; wuz1@ornl.gov"
    "tcchiang@illinois.edu; chuangtm@gate.sinica.edu.tw"

The model returns the concatenated string instead of picking one or returning
a list. Affects ~1% of records in a corpus of ~100K papers.

Two uses:

1. **Inline in the extractor** — call `split_multi_email()` on each LLM-
   returned email field, store `primary` in the existing slot and `extras`
   in a new `additional_emails` field. Idempotent on single-clean addresses
   (fast path).

2. **One-shot post-processor** — `process_corpus()` rewrites an existing
   `emails.jsonl` to `emails_v2.jsonl` and emits a diff log
   (`split_diff.jsonl`) for audit. Use this when you discover the defect
   after the extraction has already run for hours.

Run `python split_multi_email.py --test` to verify the splitter on the
five canonical edge cases before deploying.
"""
from __future__ import annotations
import json
import re
import sys
from pathlib import Path
from collections import Counter

# RFC-lite for individual-address validation after split.
RFC_LITE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Splitter for multi-email strings: ' or ', ' and ', ';', ',', ' / ', ' & '.
# Conservative — does NOT split on whitespace alone (some valid local-parts
# could legitimately contain unusual chars, and we'd rather miss-split than
# corrupt a real address).
SPLIT_RE = re.compile(r"(?:\s+(?:or|and)\s+|[;,]\s*|\s+/\s+|\s+&\s+)")


def split_multi_email(value):
    """Return (primary, additional_list).

    Behavior:
    - Single clean address       → (addr, [])
    - "a or b" / "a; b" / "a, b" → (a, [b, ...])
    - 3+ addresses               → (first, [second, third, ...])
    - Empty string               → (None, [])
    - None                       → (None, [])
    - Not-an-email string        → (raw, [])  — caller decides

    The strip-set "<>(),:;" removes trailing sentence punctuation that the
    LLM sometimes leaves attached when extracting from prose.
    """
    if not value or not isinstance(value, str):
        return (None if not value else value), []
    raw = value.strip()
    if not raw:
        return None, []
    if RFC_LITE.match(raw):
        return raw, []  # fast path — common case
    parts = SPLIT_RE.split(raw)
    parts = [p.strip().strip("<>(),:;") for p in parts if p.strip()]
    valid = [p for p in parts if RFC_LITE.match(p)]
    if not valid:
        return raw, []
    return valid[0], valid[1:]


# ---- Post-processor for an existing JSONL corpus ----

def fix_record(rec, primary_field="corresponding_email",
               authors_field="authors", author_email_field="email",
               extras_field="additional_emails"):
    """Apply the split fix to one extraction record. Returns (rec, changes)."""
    changes = []
    # Top-level corresponding-email-style field
    primary, extra, was_split = _maybe_split(rec.get(primary_field))
    if was_split:
        changes.append({"field": primary_field,
                        "before": rec.get(primary_field),
                        "primary": primary, "additional": extra})
        rec[primary_field] = primary
        rec.setdefault(extras_field, []).extend(extra)
    # Per-author email field
    for i, a in enumerate(rec.get(authors_field) or []):
        p, e, was_split2 = _maybe_split(a.get(author_email_field))
        if was_split2:
            changes.append({"field": f"{authors_field}[{i}].{author_email_field}",
                            "name": a.get("name"),
                            "before": a.get(author_email_field),
                            "primary": p, "additional": e})
            a[author_email_field] = p
            a[extras_field] = e
    return rec, changes


def _maybe_split(value):
    """split_multi_email + a was_split flag for the post-processor."""
    primary, extra = split_multi_email(value)
    was_split = bool(extra) or (isinstance(value, str)
                                and value.strip()
                                and primary != value.strip()
                                and primary is not None)
    return primary, extra, was_split


def process_corpus(src_path, out_path, diff_path):
    src = Path(src_path); out = Path(out_path); diff = Path(diff_path)
    n_total = n_changed = n_recovered = 0
    by_field = Counter()
    with src.open() as fin, out.open("w") as fout, diff.open("w") as fdiff:
        for line in fin:
            try:
                rec = json.loads(line)
            except Exception:
                fout.write(line)
                continue
            n_total += 1
            rec, changes = fix_record(rec)
            if changes:
                n_changed += 1
                for c in changes:
                    by_field[c["field"].split("[")[0]] += 1
                    n_recovered += len(c["additional"])
                fdiff.write(json.dumps({"changes": changes,
                                        "key": rec.get("osti_id")
                                               or rec.get("doi")
                                               or rec.get("id")}) + "\n")
            fout.write(json.dumps(rec) + "\n")
            if n_total % 10000 == 0:
                print(f"  {n_total:,} processed  changed={n_changed:,}  "
                      f"recovered={n_recovered:,}", file=sys.stderr)
    return {"total": n_total, "changed": n_changed,
            "recovered": n_recovered, "by_field": dict(by_field)}


# ---- Self-test ----

_TEST_CASES = [
    ("barkakatyb@ornl.gov or lokitzbs@ornl.gov",
        ("barkakatyb@ornl.gov", ["lokitzbs@ornl.gov"])),
    ("xiangwang.chn@gmail.com; wuz1@ornl.gov",
        ("xiangwang.chn@gmail.com", ["wuz1@ornl.gov"])),
    ("tcchiang@illinois.edu; chuangtm@gate.sinica.edu.tw",
        ("tcchiang@illinois.edu", ["chuangtm@gate.sinica.edu.tw"])),
    ("a@b.org, c@d.net, e@f.gov", ("a@b.org", ["c@d.net", "e@f.gov"])),
    ("single@example.com", ("single@example.com", [])),
    ("", (None, [])),
    (None, (None, [])),
    ("not_an_email_at_all", ("not_an_email_at_all", [])),
]


def _self_test():
    fails = 0
    for inp, expected in _TEST_CASES:
        got = split_multi_email(inp)
        ok = got == expected
        print(("PASS" if ok else "FAIL"), repr(inp), "->", got,
              "" if ok else f"  expected {expected}")
        if not ok:
            fails += 1
    print(f"\n{len(_TEST_CASES) - fails}/{len(_TEST_CASES)} cases pass")
    return fails


if __name__ == "__main__":
    if "--test" in sys.argv:
        sys.exit(_self_test())
    if len(sys.argv) < 4:
        print("Usage: split_multi_email.py SRC.jsonl OUT.jsonl DIFF.jsonl",
              file=sys.stderr)
        print("       split_multi_email.py --test", file=sys.stderr)
        sys.exit(2)
    stats = process_corpus(sys.argv[1], sys.argv[2], sys.argv[3])
    print(json.dumps(stats, indent=2))
