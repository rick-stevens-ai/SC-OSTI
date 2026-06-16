#!/usr/bin/env python3
"""
Free-OA-publisher templated fetcher.

For papers whose DOI matches a known free-OA publisher pattern, construct the
canonical PDF URL directly from the DOI string and fetch — no API lookup needed,
no Unpaywall round-trip. Empirically 45% blended hit rate on Phase C residuals
(427/949 on 2026-06-10 run).

Run BEFORE the Unpaywall stage in any recovery cascade — Unpaywall often points
at publisher landing pages that 403 or redirect to login, whereas the direct
canonical-PDF URL skips the landing page entirely.

Supported DOI prefixes (per-template hit rate in parens):
  - 10.3389/  (Frontiers, ~90%)
  - 10.1038/  (Nature Comms / Sci Reports / Nature OA articles, ~60%)
  - 10.7554/eLife.  (~95%)
  - 10.1371/journal.  (PLoS, ~80%)
  - 10.1186/  (BMC via Springer, varies)
  - 10.1101/  (bioRxiv/medRxiv)
  - 10.5194/  (Copernicus journals — needs <journal>-<vol>-<page>-<year> shape)

NOT supported (use other strategies):
  - 10.1073/  (PNAS — Cloudflare-walled even on UChicago campus IP)
  - 10.1088/, 10.1063/, 10.3847/  (IOP/AIP/AAS — Radware CAPTCHA wall)
  - 10.1103/  (APS — Cloudflare 5511-byte challenge)
  - 10.1016/, 10.1002/, 10.1021/  (Sciencedirect/Wiley/ACS — paywall)

Reads target rows from a SQLite state DB with schema:
  recovery(osti_id TEXT, doi TEXT, year INTEGER, lab TEXT, fetch_status TEXT, ...)

Writes successful PDFs to <out-root>/<year>/<osti_id>.pdf and updates the row's
fetch_status to 'ok' with bytes and path populated.
"""
import sqlite3, os, time, subprocess, tempfile, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

DB_DEFAULT = "/rbstor/stevens/unpaywall_overnight.db"
OUT_ROOT_DEFAULT = Path("/rbstor/stevens/osti_fulltext_unpay")
WORKERS = 6
TIMEOUT = 30
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def validate_pdf(buf):
    if not buf or len(buf) < 4096:
        return False, f"too_small_{len(buf) if buf else 0}"
    if buf[:4] != b"%PDF":
        return False, f"not_pdf_{buf[:8].hex()}"
    return True, "ok"


def curl_get(url, timeout=TIMEOUT, accept="*/*"):
    with tempfile.NamedTemporaryFile(delete=False) as f: out = f.name
    with tempfile.NamedTemporaryFile(delete=False) as f: ck = f.name
    try:
        cmd = ["curl", "-sL", "--max-time", str(timeout),
               "-A", UA, "-H", f"Accept: {accept}",
               "--compressed", "-c", ck, "-b", ck,
               "-o", out, "-w", "%{http_code}", url]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        try: buf = open(out, "rb").read()
        except: buf = b""
        return proc.stdout.strip() or "0", buf
    finally:
        for f in (out, ck):
            try: os.unlink(f)
            except: pass


def candidate_urls(doi):
    """Return ordered list of (url, template_name) candidates to try for this DOI."""
    cands = []
    # Frontiers
    if doi.startswith("10.3389/"):
        cands.append((f"https://www.frontiersin.org/articles/{doi}/pdf", "frontiers"))
    # PLoS — needs journal subdomain map
    if doi.startswith("10.1371/journal."):
        tail = doi.split("/", 1)[1]
        journal_code = tail.split(".")[1] if "." in tail else "pone"
        jmap = {"pone": "plosone", "pbio": "plosbiology", "pcbi": "ploscompbiol",
                "pgen": "plosgenetics", "ppat": "plospathogens", "pmed": "plosmedicine",
                "pntd": "plosntds", "pchem": "ploschemistry"}
        jsub = jmap.get(journal_code, "plosone")
        cands.append((f"https://journals.plos.org/{jsub}/article/file?id={doi}&type=printable", "plos"))
    # eLife
    if doi.startswith("10.7554/eLife."):
        elife_id = doi.split(".")[-1]
        cands.append((f"https://elifesciences.org/articles/{elife_id}.pdf", "elife"))
    # PNAS — included so it's tried but expected to fail
    if doi.startswith("10.1073/"):
        cands.append((f"https://www.pnas.org/doi/pdf/{doi}", "pnas"))
    # Nature OA family (Nat Comm, Sci Rep, etc.)
    if doi.startswith("10.1038/"):
        accession = doi.split("/", 1)[1]
        cands.append((f"https://www.nature.com/articles/{accession}.pdf", "nature_oa"))
    # BMC via Springer
    if doi.startswith("10.1186/"):
        cands.append((f"https://link.springer.com/content/pdf/{doi}.pdf", "bmc_springer"))
    # bioRxiv / medRxiv
    if doi.startswith("10.1101/"):
        rxiv = doi.split("/", 1)[1]
        cands.append((f"https://www.biorxiv.org/content/10.1101/{rxiv}.full.pdf", "biorxiv"))
        cands.append((f"https://www.medrxiv.org/content/10.1101/{rxiv}.full.pdf", "medrxiv"))
    # Copernicus
    if doi.startswith("10.5194/"):
        tail = doi.split("/", 1)[1]
        parts = tail.split("-")
        if len(parts) >= 4:
            journal, vol, page, year = parts[0], parts[1], parts[2], parts[-1]
            cands.append((f"https://{journal}.copernicus.org/articles/{vol}/{page}/{year}/{tail}.pdf",
                         "copernicus"))
    return cands


def fetch_one(row, out_root):
    osti_id, doi, year, lab = row
    cands = candidate_urls(doi)
    if not cands:
        return osti_id, "freeoa_no_template", 0, None
    for url, tag in cands:
        code, buf = curl_get(url, accept="application/pdf,*/*")
        if code != "200":
            continue
        ok, _ = validate_pdf(buf)
        if not ok:
            continue
        year_dir = out_root / (str(year) if year else "unknown")
        year_dir.mkdir(parents=True, exist_ok=True)
        path = year_dir / f"{osti_id}.pdf"
        path.write_bytes(buf)
        return osti_id, "ok", len(buf), str(path)
    return osti_id, "freeoa_all_failed", 0, None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--out-root", default=str(OUT_ROOT_DEFAULT))
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--bucket-pattern", default=
                    "(fetch_status LIKE 'http_4%' OR fetch_status LIKE 'too_small_%' "
                    "OR fetch_status LIKE 'not_pdf_magic_text/html%')")
    args = ap.parse_args()
    out_root = Path(args.out_root)
    con = sqlite3.connect(args.db, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    where = f"""
    {args.bucket_pattern}
    AND doi IS NOT NULL AND doi != ''
    AND (doi LIKE '10.3389/%' OR doi LIKE '10.1371/%' OR doi LIKE '10.7554/%'
         OR doi LIKE '10.1073/%' OR doi LIKE '10.1038/%' OR doi LIKE '10.1186/%'
         OR doi LIKE '10.1101/%' OR doi LIKE '10.5194/%' OR doi LIKE '10.21105/%')
    """
    cur = con.execute(f"SELECT osti_id, doi, year, lab FROM recovery WHERE {where}")
    targets = cur.fetchall()
    con.close()
    total = len(targets)
    print(f"[freeoa] {total} targets", flush=True)
    counts = {"ok": 0, "fail": 0}
    started = time.time()
    lock = threading.Lock()

    def commit(r):
        oid, status, sz, path = r
        with lock:
            con2 = sqlite3.connect(args.db, timeout=60)
            if status == "ok":
                con2.execute(
                    "UPDATE recovery SET fetch_status=?, bytes=?, path=?, ts=? WHERE osti_id=?",
                    (status, sz, path, int(time.time()), oid))
                counts["ok"] += 1
            else:
                counts["fail"] += 1
            con2.commit()
            con2.close()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, t, out_root): t[0] for t in targets}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                commit(fut.result())
            except Exception as e:
                print(f"  exc: {e}", flush=True)
            if done % 25 == 0:
                rate = done / (time.time() - started)
                eta = (total - done) / rate / 60 if rate > 0 else 0
                print(f"  done={done}/{total} ok={counts['ok']} fail={counts['fail']} "
                      f"rate={rate:.1f}/s eta={eta:.0f}min", flush=True)
    print(f"FINAL ok={counts['ok']} fail={counts['fail']} total={total}", flush=True)


if __name__ == "__main__":
    main()
