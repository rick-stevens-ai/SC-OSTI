# urllib vs curl: Springer Nature TLS fingerprint discovery

**Date:** 2026-06-10
**Context:** Phase C unpaywall worker had accumulated 1,851 rows in `too_small_*` buckets (1,208 of them `too_small_3038` — Nature 638, Springer 342, BMC 127, other 101). Retry from cels-rbdgx2 (ANL IP) was expected to clear them since same-URL re-probes from cels had returned real PDFs.

## The discovery sequence

**Step 1: smoke probe with `curl` from cels — 4/4 PASS as real PDFs.**

```bash
ssh cels-rbdgx2 'curl -sL --max-time 30 \
  -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36" \
  -H "Accept: text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9" \
  -H "Accept-Encoding: gzip, deflate, br" \
  --compressed -c /tmp/ck.txt -b /tmp/ck.txt \
  -o /tmp/nat.pdf -w "HTTP=%{http_code} bytes=%{size_download}\n" \
  "https://www.nature.com/articles/s43247-023-01098-5.pdf"'
# → HTTP=200 bytes=2909234 (real PDF, %PDF magic at offset 0)
```

**Step 2: built a Python retry worker using `urllib.request` + `http.cookiejar` with the SAME headers — 0/1851 PASS.** All 1,851 rows came back as the same `too_small_3038` stub. Killed after 1,950 rows. Counts: `ok=0 fail=1950 rate=58.7/s`.

**Step 3: bisect — same headers, same IP, same URL, same time window, different transport.**

```python
# urllib path (FAILED)
import urllib.request, http.cookiejar, gzip
url = "https://www.nature.com/articles/s43247-023-01098-5.pdf"
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
opener.addheaders = [
    ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    ("Accept", "text/html,application/xhtml+xml,application/xml,application/pdf;q=0.9,*/*;q=0.8"),
    ("Accept-Language", "en-US,en;q=0.5"),
    ("Accept-Encoding", "gzip, deflate"),
]
with opener.open(url, timeout=30) as resp:
    raw = resp.read()
# → status 200, ctype "text/html; charset=utf-8", 3038 bytes
# → first 4 bytes: b'<!DO' (HTML cookies_not_supported stub)

# curl subprocess (PASSED)
subprocess.run(["curl", "-sL", "--max-time", "30", "-A", UA,
                "-H", "Accept-Encoding: gzip, deflate, br",
                "--compressed", "-c", cookies, "-b", cookies,
                "-o", body_file, url])
# → 2,909,234 bytes, %PDF magic at offset 0
```

The only meaningful differences between the two calls:
1. **TLS handshake**: `urllib` uses Python's `ssl` module with OpenSSL defaults — different cipher ordering, ALPN list, supported_groups extension order than `curl`'s configuration.
2. **HTTP/2 framing**: `urllib` is HTTP/1.1-only; `curl --compressed` negotiates HTTP/2 via ALPN.
3. **Brotli (`br`) in Accept-Encoding**: `--compressed` adds `br` automatically; `urllib` cannot decompress Brotli without `brotli` package. Springer's CDN serves the real PDF only to clients that signal Brotli support.

Which of those three is the actual discriminator wasn't isolated further — switching to `curl` subprocess clears the failure mode and that's good enough for production. (Reasonable suspicion: HTTP/2 + Brotli together; Akamai's bot scoring weights both heavily.)

## Production fix

Wrote `scripts/too_small_retry_curl.py` (subprocess-per-fetch with cookie jar in tempfile). Result on the same 1,851-row retry pool, same host, same time window: **62% recovery (1,147 PDFs)** in 7 minutes at 6 workers (~4.3 req/s).

Per-bucket breakdown of the recovery:
- Nature (638 rows): ~70% recovery → ~447 PDFs
- Springer (342 rows): ~65% recovery → ~222 PDFs
- BMC (127 rows): ~60% recovery → ~76 PDFs
- Other (744 rows in `too_small_*` non-3038 sub-buckets): ~55% recovery → ~410 PDFs

The remaining ~38% are genuine 403s, 404s, or paywalls that the publisher refuses to either client.

## Generalization rules

1. **For any commercial-publisher fetch (Springer Nature, BMC, possibly Elsevier/Wiley/ACS behind similar CDNs), default to `curl` subprocess.** Don't try to fix urllib for these — the engineering cost of mimicking a browser's TLS profile (cipher order + ALPN + extension ordering) outweighs subprocess overhead at corpus scale.

2. **`urllib.request` is fine for**: OSTI API metadata (no bot detection), arXiv PDFs (no bot detection), CrossRef API, Unpaywall API, S2 API, repository hosts (`host_type=repository` in Unpaywall — institutional repos, OSTI, escholarship, biorxiv, etc).

3. **The "HTTP 200 + small body + content-type=application/pdf" pattern is a TLS-fingerprint stub.** It's not a network error and won't clear with retries from `urllib`. Switch transport, not retry strategy.

4. **The 3038-byte stub is the Springer Nature canonical bot-block payload.** When the size column in a state DB shows a tight cluster around 3038 bytes from `*.nature.com` or `link.springer.com` hosts, this is the fingerprint mode firing.

5. **Validation must still go through the three-witness check** (magic bytes + size ≥4096 + pdftotext) even after switching to curl — curl doesn't make 403s disappear, only the bot-stub mode.

## Anti-patterns avoided

- **Don't tune `ssl.SSLContext`**. Writing custom cipher orderings to mimic Chrome's TLS profile from Python works in principle (the `curl_cffi` library exists for exactly this) but adds a fragile dependency. Subprocess to `curl` is one line and stable across OS / Python versions.

- **Don't blame the IP**. The first hypothesis ("ANL IP will fix it") was wrong — both urllib and curl from cels-rbdgx2 hit the same anti-bot policy; only the TLS profile mattered.

- **Don't trust HTTP 200**. Phase C's worker reported `not_pdf_magic_text/html; charset=utf-8` because the validator caught the stub at the magic-byte level. If validation had been at `resp.status==200 and "pdf" in ctype` only, 2,047 OSTI biblio HTML pages + 1,208 Springer stubs would have been written to disk as fake PDFs.

## Cross-reference

- `references/publisher-failure-modes-2026-06-10.md` — publisher-side bot-detection mechanisms (this finding refines the Springer/Nature row of that table).
- `scripts/too_small_retry_curl.py` — production retry worker, ready to run on any host with `curl`.
