#!/usr/bin/env python3
"""
arXiv title+author search worker for OSTI corpus recovery.

Pipeline per target paper:
  1. Fetch OSTI biblio HTML for the paper
  2. Extract citation_title + first citation_author lastname
  3. Search arXiv with ti:"<title>"+AND+au:<lastname>
  4. Fuzzy-validate top hit title against query title (>=70% token overlap)
  5. If validated, download PDF, write to staging dir + meta sidecar

Designed to run from non-ANL IP (M1, cherryrd, prokko) to avoid arXiv 429 wall.
Each instance processes a hash-partitioned slice of the work pool to avoid
double-fetching when running parallel workers across hosts.

Args:
  --targets <TSV>     Tab-separated: osti_id, biblio_url, year, lab
  --partition <N>     This worker's partition (0-indexed)
  --of <M>            Total worker count (partition % of == this worker's slice)
  --staging <DIR>     Output dir for PDFs + meta sidecars

Rate limits: 4s sleep between every request (search, PDF, biblio fetch).
At 3 reqs per target = ~12s/target, ~5/min, ~5500 in ~18 hours single-host.
Run 2-3 hosts in parallel to cut wall time to 6-9 hours.

Empirical hit rate (2026-06-10): 50% on APS/IOP/AIP/AAS/MNRAS-prefix pool.

Output:
  <staging>/<osti_id>.pdf     - downloaded PDF
  <staging>/<osti_id>.meta    - one-line TSV: osti_id, arxiv_id, bytes, title
  <staging>/arxiv_title_p<N>.log  - per-host run log with periodic counters
"""
import argparse, sys, os, time, subprocess, tempfile, re, urllib.parse, hashlib
from pathlib import Path

UA = "OSTI-corpus-recovery/1.0 (mailto:rick.stevens@uchicago.edu)"
SLEEP = 4.0
TIMEOUT = 25

CITATION_TITLE = re.compile(rb'citation_title["\s]+content=["\']([^"\']+)["\']', re.IGNORECASE)
CITATION_AUTHOR = re.compile(rb'citation_author["\s]+content=["\']([^"\']+)["\']', re.IGNORECASE)

STOPWORDS = {"the", "a", "an", "of", "and", "for", "in", "on", "to", "with",
             "at", "by", "from", "as", "is", "are", "be"}


def curl_get(url, timeout=TIMEOUT, accept="*/*"):
    """Single curl GET, returns (http_code_str, response_body_bytes)."""
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


def validate_pdf(buf):
    """Magic-byte + size check. Returns (ok_bool, reason_str)."""
    if not buf or len(buf) < 4096:
        return False, f"too_small_{len(buf) if buf else 0}"
    if buf[:4] != b"%PDF":
        return False, "not_pdf"
    return True, "ok"


def extract_title_author(biblio_url):
    """Fetch OSTI biblio page, extract title + first author lastname. (title, lastname) or (None, None)."""
    code, body = curl_get(biblio_url, timeout=20, accept="text/html,*/*")
    if code != "200" or not body:
        return None, None
    tm = CITATION_TITLE.search(body)
    if not tm:
        return None, None
    title = tm.group(1).decode("utf-8", errors="ignore").strip()
    am = CITATION_AUTHOR.search(body)
    author = None
    if am:
        raw = am.group(1).decode("utf-8", errors="ignore").strip()
        if "," in raw:
            author = raw.split(",")[0].strip()  # "Lastname, F." form
        else:
            parts = raw.split()
            if parts:
                author = parts[-1].strip()  # "First Last" form
    return title, author


def tokens(s):
    """Lowercase word tokens, minus stopwords. Used for title fuzzy match."""
    return set(re.findall(r"[a-z0-9]+", s.lower())) - STOPWORDS


def title_match_ok(query_title, found_title, threshold=0.7):
    """Require >=70% token overlap on query title's tokens. False positive guard."""
    qt, ft = tokens(query_title), tokens(found_title)
    if not qt:
        return False
    return len(qt & ft) / len(qt) >= threshold


def search_arxiv(title, author):
    """Return (arxiv_id, matched_title) of first validated hit, or (None, None)."""
    title_clean = re.sub(r'[^\w\s\-]', ' ', title)[:200]  # strip punctuation, cap length
    q = f'ti:"{title_clean}"'
    if author and len(author) > 2:
        q += f"+AND+au:{author}"
    qenc = urllib.parse.quote(q, safe=":+")
    url = f"https://export.arxiv.org/api/query?search_query={qenc}&max_results=3"
    code, body = curl_get(url, timeout=20, accept="application/atom+xml")
    if code != "200" or not body:
        return None, None
    entries = re.split(rb"<entry>", body)[1:]
    for entry in entries[:3]:
        id_m = re.search(rb"<id>http://arxiv.org/abs/([^<]+)</id>", entry)
        title_m = re.search(rb"<title>([^<]+)</title>", entry, re.DOTALL)
        if not id_m or not title_m:
            continue
        arxiv_id = id_m.group(1).decode().strip()
        found_title = title_m.group(1).decode("utf-8", errors="ignore").strip()
        found_title = re.sub(r"\s+", " ", found_title)
        if title_match_ok(title, found_title):
            return arxiv_id, found_title
    return None, None


def fetch_pdf(arxiv_id):
    """Strip version suffix to get the latest version PDF."""
    base = re.sub(r"v\d+$", "", arxiv_id)
    url = f"https://arxiv.org/pdf/{base}"
    return curl_get(url, accept="application/pdf,*/*")


def load_targets(targets_file, partition, of):
    """Targets TSV: osti_id\\tbiblio_url\\tyear\\tlab. Hash-partition by osti_id."""
    targets = []
    with open(targets_file) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            osti_id, biblio_url, year, lab = parts[:4]
            h = int(hashlib.sha256(osti_id.encode()).hexdigest(), 16)
            if h % of != partition:
                continue
            targets.append((osti_id, biblio_url, year, lab))
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--partition", type=int, required=True)
    ap.add_argument("--of", type=int, required=True)
    ap.add_argument("--staging", required=True)
    args = ap.parse_args()
    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)
    targets = load_targets(args.targets, args.partition, args.of)
    total = len(targets)
    print(f"[arxiv_title part={args.partition}/{args.of}] {total} targets", flush=True)
    counts = {"ok": 0, "no_meta": 0, "no_match": 0, "fail_pdf": 0, "fail_other": 0}
    started = time.time()
    log_path = staging / f"arxiv_title_p{args.partition}.log"
    log_fp = open(log_path, "w")

    def log(s):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {s}", flush=True)
        log_fp.write(f"[{ts}] {s}\n")
        log_fp.flush()

    for i, (osti_id, biblio_url, year, lab) in enumerate(targets):
        try:
            title, author = extract_title_author(biblio_url)
            time.sleep(SLEEP)
            if not title:
                counts["no_meta"] += 1
                continue
            arxiv_id, matched_title = search_arxiv(title, author)
            time.sleep(SLEEP)
            if not arxiv_id:
                counts["no_match"] += 1
                continue
            code, buf = fetch_pdf(arxiv_id)
            time.sleep(SLEEP)
            if code == "200":
                ok, why = validate_pdf(buf)
                if ok:
                    path = staging / f"{osti_id}.pdf"
                    path.write_bytes(buf)
                    (staging / f"{osti_id}.meta").write_text(
                        f"{osti_id}\t{arxiv_id}\t{len(buf)}\t{title[:120]}\n")
                    counts["ok"] += 1
                else:
                    counts["fail_pdf"] += 1
            else:
                counts["fail_pdf"] += 1
        except Exception as e:
            counts["fail_other"] += 1
            log(f"exc {osti_id}: {str(e)[:80]}")
        if (i + 1) % 20 == 0:
            el = time.time() - started
            rate = (i + 1) / el
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            log(f"done={i+1}/{total} ok={counts['ok']} no_meta={counts['no_meta']} "
                f"no_match={counts['no_match']} fail={counts['fail_pdf']+counts['fail_other']} "
                f"eta={eta:.0f}min")
    log(f"FINAL ok={counts['ok']} no_meta={counts['no_meta']} no_match={counts['no_match']} "
        f"fail={counts['fail_pdf']+counts['fail_other']} total={total}")
    log_fp.close()


if __name__ == "__main__":
    main()
