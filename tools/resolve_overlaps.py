#!/usr/bin/env python3
"""
Resolve canonical-pick for each osti_id with 2+ file_instances.

Evidence (in order of strength):
  1. SHA-256 byte-identity: if all instances match, pick any (source priority tiebreak),
     log 'duplicate_byte_identical'.
  2. Size + extracted title: if sizes differ AND we have papers.title from OSTI catalog,
     extract first-page text from each candidate, score similarity to papers.title,
     pick the best-scoring instance, log 'title_extract_match'.
  3. Largest size wins: if no title from catalog OR title extract fails for all,
     pick largest by size (smaller files are usually error pages / paywalls / truncated),
     log 'size_largest'.
  4. Source priority (fallback if all sizes equal): osti_fulltext > osti_fulltext_v2 > osti_fulltext_unpay.

Writes:
  - decisions table: one row per osti_id resolved
  - file_instances: set is_canonical=1, canonical_decision_id=N on chosen instance
  - papers: set canonical_pdf_path, canonical_source, canonical_size, canonical_sha256, has_pdf=1

Idempotent: if a decision for an osti_id already exists for this run-tag, skip it.

Usage:
  resolve_overlaps.py [--limit N] [--run-tag TAG]
"""
import argparse, hashlib, json, sqlite3, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from difflib import SequenceMatcher

CATALOG = "/Volumes/Cherry6TB/osti_corpus/_state/catalog.sqlite"
SOURCE_PRIORITY = {"osti_fulltext": 1, "osti_fulltext_v2": 2, "osti_fulltext_unpay": 3}
PDFTOTEXT = "/opt/homebrew/bin/pdftotext"
TITLE_SAMPLE_BYTES = 4096  # first ~4KB of text usually has title

def sha256_of(path, chunk=1024*1024):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b: break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return None

def extract_first_page_text(path, max_pages=1, timeout=10):
    """Use pdftotext to extract first page, return as string, or None on failure."""
    try:
        r = subprocess.run(
            [PDFTOTEXT, "-l", str(max_pages), "-q", path, "-"],
            capture_output=True, timeout=timeout
        )
        if r.returncode == 0:
            txt = r.stdout.decode("utf-8", errors="replace")
            return txt[:TITLE_SAMPLE_BYTES]
        return None
    except Exception:
        return None

def normalize_title(s):
    if not s: return ""
    return " ".join(s.lower().split())

def title_similarity(extracted_text, catalog_title):
    """Score how well the catalog_title appears within the extracted first-page text."""
    if not extracted_text or not catalog_title:
        return 0.0
    text_norm = normalize_title(extracted_text)
    title_norm = normalize_title(catalog_title)
    if not title_norm:
        return 0.0
    # Two signals: substring presence (strong), and fuzzy ratio across full content
    if title_norm in text_norm:
        return 1.0
    # Try first 60 chars of title (handles truncation / line breaks in PDF)
    title_short = title_norm[:60]
    if title_short and title_short in text_norm:
        return 0.95
    # Fall back to fuzzy match against the chunk of extracted text most likely to contain the title
    head = text_norm[:1000]
    return SequenceMatcher(None, head, title_norm).ratio()

def resolve_one_id(conn, osti_id, instances, catalog_title):
    """
    Resolve canonical pick for one osti_id given its instances and (optional) catalog title.
    instances: list of (instance_id, source, path, size)
    catalog_title: string from papers.title, or None
    Returns: (chosen_instance_id, decision_type, method, confidence, rationale, inputs)
    """
    # Filter out zero-byte files
    instances = [(iid, src, p, sz) for (iid, src, p, sz) in instances if sz and sz > 0]
    if not instances:
        return None
    if len(instances) == 1:
        iid, src, p, sz = instances[0]
        return (iid, "single_instance", "single", 1.0,
                f"Single instance from {src}",
                {"size": sz, "source": src})

    # === SHA-256 sweep ===
    hashes = {}
    for iid, src, p, sz in instances:
        h = sha256_of(p)
        hashes[iid] = h

    # Are all non-null hashes identical?
    non_null = [h for h in hashes.values() if h]
    if non_null and len(set(non_null)) == 1:
        # All byte-identical: pick by source priority
        instances_sorted = sorted(instances, key=lambda x: SOURCE_PRIORITY.get(x[1], 99))
        chosen = instances_sorted[0]
        return (chosen[0], "duplicate_byte_identical", "sha256_match", 1.0,
                f"All {len(instances)} instances byte-identical (sha256={non_null[0][:12]}...); picked by source priority",
                {"sha256": non_null[0], "sizes": [x[3] for x in instances]})

    # === Size + title-extract decision ===
    # Update sha256 in DB while we're at it
    for iid, h in hashes.items():
        if h:
            conn.execute("UPDATE file_instances SET sha256=? WHERE instance_id=?", (h, iid))

    # If we have a catalog title, extract & compare
    if catalog_title:
        scores = {}
        for iid, src, p, sz in instances:
            text = extract_first_page_text(p)
            score = title_similarity(text, catalog_title)
            scores[iid] = score
            conn.execute("UPDATE file_instances SET extracted_title=?, title_match_score=? WHERE instance_id=?",
                         ((text[:500] if text else None), score, iid))
        # Pick the highest-scoring instance with score >= 0.5
        ranked = sorted(scores.items(), key=lambda x: (-x[1], SOURCE_PRIORITY.get(
            next(s for (i, s, p, sz) in instances if i == x[0]), 99)))
        best_iid, best_score = ranked[0]
        if best_score >= 0.5:
            chosen = next(x for x in instances if x[0] == best_iid)
            return (best_iid, "title_extract_match", "title_extract_match", best_score,
                    f"Title match score {best_score:.2f} for {chosen[1]} vs OSTI catalog title",
                    {"scores": scores, "sizes": {iid: sz for (iid, _, _, sz) in instances},
                     "sha256s": {iid: h for iid, h in hashes.items()}})
        # Title match weak — fall through to size+priority

    # === Largest-size wins ===
    sizes = {iid: sz for (iid, _, _, sz) in instances}
    max_size = max(sizes.values())
    largest = [iid for iid, sz in sizes.items() if sz == max_size]
    if len(largest) == 1:
        chosen_iid = largest[0]
        chosen = next(x for x in instances if x[0] == chosen_iid)
        return (chosen_iid, "size_largest", "size_largest", 0.8,
                f"Largest size {max_size} from {chosen[1]} (others: {sorted(sizes.values())})",
                {"sizes": sizes, "sha256s": {iid: h for iid, h in hashes.items()}})

    # === Source priority fallback ===
    instances_sorted = sorted([x for x in instances if x[0] in largest],
                              key=lambda x: SOURCE_PRIORITY.get(x[1], 99))
    chosen = instances_sorted[0]
    return (chosen[0], "source_priority", "source_priority", 0.6,
            f"Tied at largest size {max_size}; picked {chosen[1]} by source priority",
            {"sizes": sizes, "sha256s": {iid: h for iid, h in hashes.items()}})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Only resolve N overlap IDs (smoke test)")
    ap.add_argument("--run-tag", type=str, default="resolve_overlaps_v1")
    ap.add_argument("--single-instances", action="store_true",
                    help="Also resolve single-instance IDs (mark them canonical)")
    args = ap.parse_args()

    conn = sqlite3.connect(CATALOG, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")

    # Build worklist
    if args.single_instances:
        ids = [r[0] for r in conn.execute("SELECT DISTINCT osti_id FROM file_instances WHERE osti_id IS NOT NULL ORDER BY osti_id").fetchall()]
        mode = "all"
    else:
        ids = [r[0] for r in conn.execute("""
            SELECT osti_id FROM file_instances
            WHERE osti_id IS NOT NULL
            GROUP BY osti_id
            HAVING COUNT(*) >= 2
            ORDER BY osti_id
        """).fetchall()]
        mode = "overlaps_only"

    if args.limit:
        ids = ids[:args.limit]
    print(f"Mode: {mode}, processing {len(ids):,} osti_ids", flush=True)

    t0 = time.time()
    stats = {"resolved": 0, "single": 0, "byte_identical": 0, "title_match": 0,
             "size_largest": 0, "source_priority": 0, "skipped": 0, "errors": 0}

    for i, osti_id in enumerate(ids):
        if i % 50 == 0 and i > 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (len(ids) - i) / rate / 60
            print(f"  {i:,}/{len(ids):,} rate={rate:.1f}/s eta={eta:.1f}min "
                  f"resolved={stats['resolved']} types={stats}", flush=True)

        instances = conn.execute(
            "SELECT instance_id, source, path, size FROM file_instances WHERE osti_id=?",
            (osti_id,)).fetchall()

        # Check existing decision for this run-tag
        existing = conn.execute(
            "SELECT decision_id FROM decisions WHERE osti_id=? AND inputs_json LIKE ?",
            (osti_id, f'%"run_tag": "{args.run_tag}"%')).fetchone()
        if existing:
            stats["skipped"] += 1
            continue

        title_row = conn.execute("SELECT title FROM papers WHERE osti_id=?", (osti_id,)).fetchone()
        catalog_title = title_row[0] if title_row else None

        try:
            result = resolve_one_id(conn, osti_id, instances, catalog_title)
            if not result:
                stats["errors"] += 1
                continue
            chosen_iid, dtype, method, confidence, rationale, inputs = result
            inputs["run_tag"] = args.run_tag
            chosen = next(x for x in instances if x[0] == chosen_iid)
            rejected = [iid for (iid, _, _, _) in instances if iid != chosen_iid]
            ts = datetime.utcnow().isoformat() + "Z"
            cur = conn.execute("""
                INSERT INTO decisions (ts, osti_id, decision_type, chosen_instance_id,
                                       rejected_instance_ids_json, rationale, method,
                                       confidence, inputs_json)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (ts, osti_id, dtype, chosen_iid, json.dumps(rejected),
                  rationale, method, confidence, json.dumps(inputs, default=str)))
            decision_id = cur.lastrowid
            conn.execute("UPDATE file_instances SET is_canonical=0 WHERE osti_id=?", (osti_id,))
            conn.execute("UPDATE file_instances SET is_canonical=1, canonical_decision_id=? WHERE instance_id=?",
                         (decision_id, chosen_iid))
            chosen_sha = conn.execute("SELECT sha256 FROM file_instances WHERE instance_id=?", (chosen_iid,)).fetchone()[0]
            conn.execute("""UPDATE papers
                            SET canonical_pdf_path=?, canonical_source=?, canonical_size=?,
                                canonical_sha256=?, has_pdf=1
                            WHERE osti_id=?""",
                         (chosen[2], chosen[1], chosen[3], chosen_sha, osti_id))
            # If no papers row existed, insert a stub so we don't lose track
            if conn.total_changes and conn.execute("SELECT 1 FROM papers WHERE osti_id=?", (osti_id,)).fetchone() is None:
                now = datetime.utcnow().isoformat() + "Z"
                conn.execute("""INSERT INTO papers
                                (osti_id, catalog_first_seen_ts, catalog_last_seen_ts,
                                 metadata_source, has_pdf, canonical_pdf_path,
                                 canonical_source, canonical_size, canonical_sha256,
                                 notes)
                                VALUES (?,?,?,?,1,?,?,?,?,?)""",
                             (osti_id, now, now, "stub_from_file_only",
                              chosen[2], chosen[1], chosen[3], chosen_sha,
                              "Stub: file on disk but not yet in OSTI catalog"))
            conn.commit()
            stats["resolved"] += 1
            if dtype == "single_instance": stats["single"] += 1
            elif dtype == "duplicate_byte_identical": stats["byte_identical"] += 1
            elif dtype == "title_extract_match": stats["title_match"] += 1
            elif dtype == "size_largest": stats["size_largest"] += 1
            elif dtype == "source_priority": stats["source_priority"] += 1
        except Exception as e:
            print(f"  ERROR osti_id={osti_id}: {e}", flush=True)
            stats["errors"] += 1
            conn.rollback()

    elapsed = time.time() - t0
    print(f"\n=== Done in {elapsed/60:.1f}min ===")
    for k, v in stats.items():
        print(f"  {k:20s} {v:>7,}")
    conn.close()

if __name__ == "__main__":
    main()
