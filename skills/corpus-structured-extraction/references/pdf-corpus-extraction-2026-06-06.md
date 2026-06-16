# PDF corpus fulltext extraction with empty-PDF recovery

When the corpus is a local PDF mirror (OSTI, arXiv dump, institutional
repo snapshot), you need a preprocessing layer that turns PDFs into text
the LLM judges can read. This is mechanical (allowed, see SKILL.md HARD
RULE) but has three real failure modes that bite at scale:

1. A non-trivial fraction of PDFs are 0-byte placeholders (OSTI: 12% =
   8,160 of 67,590 in 2026-06-06 snapshot).
2. Extraction throughput varies 5× depending on worker count and
   pdf-library choice.
3. Recovery via re-download requires following publisher PURL redirects,
   which Python's default urlopen does inconsistently.

This reference captures the working pattern.

## Stack

- **pymupdf** (`pip install pymupdf`, imports as `fitz`) — fastest pure-Python
  PDF extractor. ~14-27 PDFs/sec single-threaded depending on size, scales
  near-linearly to 12 workers on M1.
- **concurrent.futures.ThreadPoolExecutor** — pymupdf is GIL-friendly enough
  for I/O-bound PDF reads. Don't bother with multiprocessing.
- **Idempotent + restartable** — skip if output `.txt` exists, scan output
  dir at startup to build skip set.

## Layout assumption (OSTI shape)

OSTI's local mirror has a double-year nesting quirk:
```
/Volumes/Cherry6TB/osti_fulltext/<year>/<year>/<osti_id>.pdf
```
Build an `all_ids.tsv` (year\tosti_id\tpath) once via `find` and reuse — don't
re-walk the volume for every script.

### Pitfall: `ls /Volumes/Cherry6TB/` hangs — always use direct paths

The Cherry6TB HFS volume's root catalog scan can hang indefinitely (verified
2026-06-07: `ls /Volumes/Cherry6TB/` and `find /Volumes/Cherry6TB -maxdepth
1` both timed out at 60s, but `stat /Volumes/Cherry6TB/osti_fulltext`
returned in <1s). The volume is mounted and healthy — it's the catalog
enumeration that stalls under M1 memory pressure or cold-cache conditions.

**Don't probe the volume by listing the root.** When checking whether a
known subdirectory exists, `stat <known-absolute-path>` directly. When
discovering what's there for the first time, ask via the existing
`all_ids.tsv` index or `kukla-mail` Ollie for the canonical path rather
than walking the catalog.

If you genuinely need to enumerate, use `timeout 30 find /Volumes/Cherry6TB
-maxdepth 1 -type d 2>&1` and accept that empty output ≠ empty volume
(the timeout-killed `find` writes nothing). Re-run during low-pressure
periods or after a `purge` if the catalog cache has been evicted.

### Pitfall: `timeout N <cmd>` not `<cmd> &; sleep N; kill %1`

Hermes' shell sandbox blocks backgrounded commands (`&`, `nohup &`) with
"Foreground command uses '&' backgrounding". This bites every time you
want a bounded-time probe of a slow operation (volume catalog scans,
network reaches, hung mounts). Use the `timeout` coreutil prefix instead:

```bash
# WRONG — will be rejected by the sandbox:
ls -la /Volumes/Cherry6TB/ &
sleep 8
kill %1

# RIGHT:
timeout 8 ls -la /Volumes/Cherry6TB/ 2>&1 | head -20
```

The exit code from `timeout` distinguishes natural completion (0/N), kill
on timeout (124), and command-not-found (127). For probes that need a
real background process (server startup, long-lived watchers), use
`terminal(background=true)` per the broader Hermes pattern, not shell
backgrounding.

## Extraction script template

```python
#!/opt/homebrew/bin/python3.13
"""extract_fulltext.py — pymupdf parallel extractor, idempotent."""
import fitz, os, sys, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

INDEX = Path("all_ids.tsv")      # year\tosti_id\tpath
OUT_DIR = Path("fulltext")
META_OUT = Path("fulltext_meta.jsonl")
WORKERS = 12

OUT_DIR.mkdir(exist_ok=True)
done = {p.stem for p in OUT_DIR.glob("*.txt")}
print(f"[skip] {len(done)} already extracted", file=sys.stderr)

def extract_one(line):
    year, osti_id, path = line.rstrip("\n").split("\t")
    if osti_id in done:
        return None
    pdf_path = Path(path)
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        return {"osti_id": osti_id, "status": "EMPTY", "error": "0-byte file"}
    try:
        doc = fitz.open(pdf_path)
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
        if len(text.strip()) < 100:
            return {"osti_id": osti_id, "status": "TOO_SHORT", "chars": len(text)}
        (OUT_DIR / f"{osti_id}.txt").write_text(text)
        return {"osti_id": osti_id, "status": "OK", "chars": len(text), "pages": doc.page_count}
    except Exception as e:
        return {"osti_id": osti_id, "status": "ERROR", "error": f"{type(e).__name__}: {e}"}

lines = [l for l in INDEX.read_text().splitlines() if l]
t0 = time.time()
with ThreadPoolExecutor(max_workers=WORKERS) as ex, META_OUT.open("a") as meta:
    futs = {ex.submit(extract_one, l): l for l in lines}
    n_ok = n_err = 0
    for i, f in enumerate(as_completed(futs)):
        r = f.result()
        if r is None: continue
        meta.write(json.dumps(r) + "\n"); meta.flush()
        if r["status"] == "OK": n_ok += 1
        else: n_err += 1
        if i % 500 == 0:
            rate = (i+1)/(time.time()-t0)
            print(f"[{i+1}/{len(lines)}] ok={n_ok} err={n_err} {rate:.1f}/s", file=sys.stderr)
```

## Throughput data points (M1 mini, 2026-06-06)

- 6 workers: 14 PDF/s
- 12 workers: 27 PDF/s  ← optimal on this machine
- 16 workers: marginal gain, more contention

ETA for 67K papers at 27/s = ~41 min.

## Empty-PDF recovery — 3-tier fallback

When the local PDF is 0 bytes, you have three recovery paths in descending
preference order:

1. **OSTI PURL re-download** — most reliable. URL shape:
   `https://www.osti.gov/biblio/<osti_id>`. This returns HTTP 302 to a
   publisher-hosted PDF. **You MUST follow redirects explicitly** — see
   pitfall below.
2. **arXiv DOI lookup** — for papers with an arXiv DOI in their OSTI
   metadata. Hit `https://arxiv.org/pdf/<arxiv_id>` directly.
3. **Abstract-only fallback** — flag `fulltext_status: ABSTRACT_ONLY` in
   the metadata record, judge with weaker confidence. The judge prompt
   should know to soften its conclusions when only abstract is available.

### Pitfall: urlopen redirect handling is broken for publisher landings — use curl

Python's `urllib.request.urlopen` follows redirects by default BUT the
redirect chain breaks on publisher landing pages (escholarship.org, bnl.gov,
many others) with a hard **HTTP 403 Forbidden** — even with a normal browser
User-Agent. A previous version of this reference shipped a `fetch_pdf()`
helper that built a custom HTTPRedirectHandler chain; **it does not work** on
the worst cases. Confirmed 2026-06-06: 5-paper smoke yielded 1/5 success via
the urllib path; same 5 URLs via `curl -sL -A "Mozilla/5.0…"` → 5/5 PDFs
downloaded cleanly. The difference is in how curl negotiates the
publisher's redirect-cookie / Referer handshake — fighting urllib's handler
to match curl is not worth the effort.

**The reliable recipe is curl-as-subprocess:**

```python
import os, subprocess, tempfile
from typing import Optional, Tuple

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) corpus-recovery/1.0 (mailto:you@example.org)"
)

def fetch(url: str, timeout: int = 45) -> Tuple[Optional[bytes], str, str]:
    """Returns (body_bytes, content_type, final_url) or (None, '', '').

    curl -sL handles publisher redirect chains that urllib chokes on with 403.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tf:
            tmp_path = tf.name
        r = subprocess.run(
            ["curl", "-sL", "-A", USER_AGENT,
             "--max-time", str(timeout),
             "-o", tmp_path,
             "-w", "%{content_type}|%{url_effective}|%{http_code}",
             url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
        parts = (r.stdout or "").strip().split("|", 2)
        ctype = parts[0].lower() if len(parts) > 0 else ""
        final_url = parts[1] if len(parts) > 1 else ""
        http_code = parts[2] if len(parts) > 2 else ""
        if http_code != "200":
            return None, ctype, final_url
        with open(tmp_path, "rb") as f:
            return f.read(), ctype, final_url
    except Exception:
        return None, "", ""
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass
```

Then your recovery loop checks `data[:4] == b"%PDF"` for direct hits and
falls back to parsing the HTML body for a `citation_pdf_url` meta tag or
`<a href="...pdf">` link before giving up.

### Pitfall: image-only / scanned PDFs return blank text — OCR fallback recipe

A meaningful slice of older lab reports (BNL, ORNL, LANL) are **scanned
PDFs with no text layer**. pymupdf's `get_text("text")` returns 30 chars of
whitespace and your judge sees nothing useful. The recipe is to detect the
short-output case and OCR with tesseract:

```python
def pdf_bytes_to_text(data: bytes) -> Optional[str]:
    """Extract text from a PDF blob. Falls back to OCR if image-only."""
    if not data or data[:4] != b"%PDF":
        return None
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            parts = [page.get_text("text") for page in doc]
            text = "\n".join(parts)
            if text.strip() and len(text) > 200:
                return text
            # Image-only PDF: OCR via tesseract. Cap at 20 pages — the
            # abstract/intro/methods that drive judgment live in the first
            # few pages, and OCR-ing a 200-page LDRD report is wasteful.
            try:
                import io, pytesseract
                from PIL import Image
                ocr_parts = []
                for i, page in enumerate(doc):
                    if i >= 20:
                        break
                    pix = page.get_pixmap(dpi=200)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    ocr_parts.append(pytesseract.image_to_string(img))
                ocr_text = "\n".join(ocr_parts)
                if ocr_text.strip() and len(ocr_text) > 200:
                    return ocr_text
            except Exception:
                pass
    except Exception:
        return None
    return None
```

Setup (one-time): `brew install tesseract` (macOS) and
`pip install pytesseract Pillow`. The bundled `tesseract` is fine for
English-only scientific text; languages with non-Latin scripts need the
language packs installed separately.

**Throughput tradeoff:** direct-text PDFs extract at sub-second; OCR is
~25-50s per paper for a typical 20-page report at 200dpi. In the OSTI smoke
100-paper run (2026-06-06), 9% needed OCR — they accounted for ~70% of
total wall time. Run recovery with 6-8 workers; OCR is CPU-bound so more
workers help only the network-bound direct-text tier.

### Pitfall: HTML landing pages need a separate parse-for-PDF-link step

Some publishers return a landing page (HTML) at the PURL final URL rather
than the PDF itself. Detect via `Content-Type: text/html` or
`data[:5].lower() in (b"<html", b"<!doc")`, then scan for:

```python
PDF_LINK_RES = [
    re.compile(rb'href=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE),
    re.compile(rb'<meta\s+name=["\']citation_pdf_url["\']\s+content=["\']([^"\']+)["\']', re.IGNORECASE),
]
```

Most academic publishers emit `citation_pdf_url` meta tags (a Google
Scholar convention). Always check that one first before scraping `<a>`
tags — it's exact, not heuristic.

### Pitfall: don't run recovery in parallel against the same publisher

Publishers rate-limit by IP. Use 2-4 workers max, jitter requests with
`time.sleep(random.uniform(1, 3))` between calls, and log the publisher
domain in the recovery JSONL so you can audit blast radius.

### Smoke before scale (recovery)

Run recovery on 100 random empty PDFs first, NOT 5. The 5-paper smoke is
too small to reveal redirect-handling bugs reliably — first OSTI smoke
(2026-06-06) returned 1/5 success and the diagnosis required `curl -I` on
each failed URL to see the redirect chain. With 100, you get a real
recovery-rate estimate and a clean publisher distribution showing which
ones are giving you trouble.

**Realistic recovery rate with the full curl + OCR + HTML-landing recipe
above: 95-99%.** OSTI 100-paper smoke (2026-06-06) hit 96% — 95 via direct
OSTI_PURL (median 2.5s), 1 via OSTI_PURL landing-page parse, 9 via OCR
fallback (median ~30s), 1 hard failure. The previous "50-80%" guess in
this reference was wrong — that was the urlopen-only number, before the
curl swap and OCR fallback landed. If your smoke shows <90%, something in
the recipe is degraded — check curl is present, tesseract is installed,
and the User-Agent string isn't being filtered.

## Empty-PDF year distribution (OSTI, 2026-06-06)

For reference / what to expect on similar mirrors:
```
2016:  650    2021: 1090
2017:  862    2022:  781
2018:  900    2023:  722
2019: 1187    2024:  644
2020: 1149    2025:  175
```
Older papers more likely to be 0-byte placeholders (link rot, removed from
publisher). Newer papers (current year) often empty because OSTI hadn't
finished ingesting at snapshot time — retry these in a few weeks.

## When to skip extraction entirely

If the corpus has a metadata API and the LLM judgment can work from
title+abstract+keywords (see SKILL.md "metadata-only screening variant"),
**skip the PDFs**. For the OSTI 67K case, the initial v1 judge ran abstract-
only and produced usable verdicts; fulltext extraction is the upgrade for
**Stage 2 deep extraction** in the cascade, not the v1 triage. Don't extract
PDFs you won't read.

## Failure mode index for future smoke runs

- Output dir not creatable → check permissions, the volume might be read-only
- `fitz.open()` segfault → corrupt PDF, log and skip. pymupdf 1.26+ usually
  raises a Python exception instead, but very old PDFs (pre-1.4) can crash.
- All-images PDF (scanned, no text layer) → `text` will be empty/whitespace.
  Flag as `TOO_SHORT` and either (a) skip or (b) route to OCR pipeline
  (tesseract, marker-pdf). Decision is corpus-dependent.
- Encrypted PDF → `fitz.open()` raises `RuntimeError: cannot authenticate`.
  Try `doc.authenticate("")` (empty password unlocks most public-data PDFs).
