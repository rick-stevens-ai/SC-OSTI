#!/usr/bin/env python3
"""
Stage 1: OSTI API year lookup for v2 PDFs with unknown year.
Queries https://www.osti.gov/api/v1/records/<osti_id> for each, parses publication_date,
writes year + lab back to the inventory DB.

Rate: 4 req/s polite (5 was the standing rule but going to 4 to stay well-clear of throttling).
Resumable: skips IDs already populated.
"""
import json, sqlite3, sys, time, urllib.request, urllib.error
from pathlib import Path

AUDIT = Path("/Volumes/SG-1-8TB/osti/catalog/inventory.sqlite")
API = "https://www.osti.gov/api/v1/records/"
TIMEOUT = 30
DELAY = 0.3      # ~2 req/s effective (verified clean in 30-id smoke at 0.5s)
MAX_RETRY = 4    # exp backoff 2,4,8,16s on 429

conn = sqlite3.connect(AUDIT)

# Add columns for API-resolved year/lab (idempotent)
try:
    conn.execute("ALTER TABLE files ADD COLUMN year_from_api INTEGER")
except sqlite3.OperationalError:
    pass
try:
    conn.execute("ALTER TABLE files ADD COLUMN lab_from_api TEXT")
except sqlite3.OperationalError:
    pass
try:
    conn.execute("ALTER TABLE files ADD COLUMN api_status TEXT")
except sqlite3.OperationalError:
    pass
conn.commit()

# Get distinct osti_ids needing API lookup (year IS NULL, not already API-resolved)
unknown_ids = [row[0] for row in conn.execute("""
    SELECT DISTINCT osti_id FROM files
    WHERE year IS NULL AND (api_status IS NULL OR api_status = 'error' OR api_status = 'timeout')
""")]
print(f"OSTI IDs needing API lookup: {len(unknown_ids)}", flush=True)
if not unknown_ids:
    print("Nothing to do.")
    sys.exit(0)

UA = "Mozilla/5.0 (Kukla agent / osti-corpus-consolidation; rick.stevens.ai@gmail.com)"

def fetch(oid):
    url = f"{API}{oid}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(MAX_RETRY):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            rec = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)
            if not rec:
                return (oid, None, None, "empty")
            pub = rec.get("publication_date") or rec.get("publicationDate") or ""
            year = None
            if pub:
                if "-" in pub and len(pub) >= 4:
                    try: year = int(pub.split("-")[0])
                    except: pass
                elif "/" in pub and len(pub) >= 10:
                    try: year = int(pub.split("/")[-1][:4])
                    except: pass
                elif len(pub) == 4 and pub.isdigit():
                    year = int(pub)
            orgs = rec.get("research_orgs") or rec.get("researchOrgs") or []
            lab = None
            if orgs and isinstance(orgs, list):
                first = orgs[0]
                if isinstance(first, dict):
                    lab = first.get("name") or first.get("orgName")
                elif isinstance(first, str):
                    lab = first
            return (oid, year, lab, "ok")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < MAX_RETRY - 1:
                back = 2 ** (attempt + 1)
                time.sleep(back)
                continue
            return (oid, None, None, f"http_{e.code}")
        except urllib.error.URLError:
            if attempt < MAX_RETRY - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            return (oid, None, None, "timeout")
        except Exception as e:
            return (oid, None, None, f"err_{type(e).__name__}")
    return (oid, None, None, "exhausted")

def write_result(oid, year, lab, status):
    conn.execute("""
        UPDATE files SET year_from_api=?, lab_from_api=?, api_status=?,
            year = COALESCE(year, ?), bucket = CASE WHEN year IS NULL AND ? IS NOT NULL THEN 'year_from_api' ELSE bucket END
        WHERE osti_id=?
    """, (year, lab, status, year, year, oid))

t0 = time.time()
done = 0
ok = 0
err = 0
year_found = 0
for oid in unknown_ids:
    res = fetch(oid)
    write_result(*res)
    done += 1
    status = res[3]
    if status == "ok":
        ok += 1
        if res[1]:
            year_found += 1
    else:
        err += 1
    if done % 50 == 0:
        conn.commit()
        elapsed = time.time() - t0
        rate = done / elapsed
        eta = (len(unknown_ids) - done) / rate if rate > 0 else 0
        print(f"  done={done}/{len(unknown_ids)}  ok={ok}  year_found={year_found}  err={err}  rate={rate:.1f}/s  eta={eta/60:.1f}min", flush=True)
    time.sleep(DELAY)
conn.commit()

elapsed = time.time() - t0
print(f"\nFINAL: done={done} ok={ok} year_found={year_found} err={err} in {elapsed/60:.1f}min")

print("\n=== After API lookup, year_unknown remaining ===")
unk = conn.execute("SELECT COUNT(DISTINCT osti_id) FROM files WHERE year IS NULL").fetchone()[0]
print(f"  {unk} OSTI IDs still year_unknown")

print("\n=== Updated year distribution ===")
for year, cnt in conn.execute("""
    SELECT year, COUNT(DISTINCT osti_id) FROM files GROUP BY year ORDER BY year
"""):
    ylabel = str(year) if year else "UNKNOWN"
    print(f"  {ylabel:>8s} {cnt:>7d}")
conn.close()
