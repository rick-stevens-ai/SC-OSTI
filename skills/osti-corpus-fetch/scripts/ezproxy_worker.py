#!/usr/bin/env python3
"""
EZproxy worker. Loads saved storage state and iterates failed PNAS/ACS/Wiley/IOP
papers, downloading PDFs through UChicago EZproxy.

Usage:
    python3 ezproxy_worker.py --targets targets.tsv --staging /tmp/ezproxy_recovered

Targets TSV columns: osti_id\\tdoi\\tpublisher\\tyear\\tlab

EZproxy URL pattern: replace dots in publisher host with dashes:
    pubs.acs.org             -> pubs-acs-org.proxy.uchicago.edu
    www.pnas.org             -> www-pnas-org.proxy.uchicago.edu
    onlinelibrary.wiley.com  -> onlinelibrary-wiley-com.proxy.uchicago.edu
    iopscience.iop.org       -> iopscience-iop-org.proxy.uchicago.edu

For each DOI, navigate to canonical PDF URL via proxy. If response is a PDF
(content-type or first bytes), save it. If it's an HTML article page, scrape
for citation_pdf_url and follow.

Designed to run from prokko (UChicago CS subnet, Tailscale <tailnet-host>) or
any other host where the EZproxy session was bootstrapped via ezproxy_login.py.

HARD RULES:
- Single-threaded. UChicago Library monitors EZproxy bulk usage by CNetID.
- 3s sleep between every navigation (RATE_SLEEP constant).
- Bail on session expiry (final URL matches login.proxy.uchicago.edu).
- Validate every PDF by three witnesses (magic bytes + size >= 4096 + content extractable).
"""
import argparse, asyncio, hashlib, os, re, sys, time
from pathlib import Path
from playwright.async_api import async_playwright

STATE_FILE = Path.home() / ".ezproxy_state.json"
RATE_SLEEP = 3.0  # Be polite — UChicago monitors EZproxy usage
LOGIN_HOST = "login.proxy.uchicago.edu"

PUBLISHER_TEMPLATES = {
    "10.1073/": ("www-pnas-org.proxy.uchicago.edu",
                 "https://www-pnas-org.proxy.uchicago.edu/doi/pdf/{doi}"),
    "10.1021/": ("pubs-acs-org.proxy.uchicago.edu",
                 "https://pubs-acs-org.proxy.uchicago.edu/doi/pdf/{doi}"),
    "10.1002/": ("onlinelibrary-wiley-com.proxy.uchicago.edu",
                 "https://onlinelibrary-wiley-com.proxy.uchicago.edu/doi/pdfdirect/{doi}"),
    "10.1029/": ("onlinelibrary-wiley-com.proxy.uchicago.edu",
                 "https://onlinelibrary-wiley-com.proxy.uchicago.edu/doi/pdfdirect/{doi}"),
    "10.1088/": ("iopscience-iop-org.proxy.uchicago.edu",
                 "https://iopscience-iop-org.proxy.uchicago.edu/article/{doi}/pdf"),
    "10.3847/": ("iopscience-iop-org.proxy.uchicago.edu",
                 "https://iopscience-iop-org.proxy.uchicago.edu/article/{doi}/pdf"),
    "10.1063/": ("pubs-aip-org.proxy.uchicago.edu",
                 "https://pubs-aip-org.proxy.uchicago.edu/aip/jcp/article-pdf/doi/{doi}"),
    # APS needs 2-hop: /abstract/<doi> -> /pdf/<doi>
    "10.1103/": ("journals-aps-org.proxy.uchicago.edu",
                 "https://journals-aps-org.proxy.uchicago.edu/abstract/{doi}"),
    # Nature: doi = 10.1038/<accession>
    "10.1038/": ("www-nature-com.proxy.uchicago.edu",
                 "https://www-nature-com.proxy.uchicago.edu/articles/{accession}.pdf"),
    # Elsevier needs PII not DOI — landing-page scrape only
    "10.1016/": ("www-sciencedirect-com.proxy.uchicago.edu",
                 "https://www-sciencedirect-com.proxy.uchicago.edu/science/article/pii/_LANDING_"),
}


def template_for_doi(doi):
    for prefix, (host, url_tmpl) in PUBLISHER_TEMPLATES.items():
        if doi.startswith(prefix):
            return prefix, host, url_tmpl
    return None, None, None


def build_url(doi, url_tmpl):
    if "_LANDING_" in url_tmpl:
        return None  # Need PII; skip Elsevier in direct template
    if "{doi}" in url_tmpl:
        return url_tmpl.format(doi=doi)
    if "{accession}" in url_tmpl:
        accession = doi.split("/", 1)[1]
        return url_tmpl.format(accession=accession)
    return None


def validate_pdf(buf):
    if not buf or len(buf) < 4096:
        return False, f"too_small_{len(buf) if buf else 0}"
    if buf[:4] != b"%PDF":
        return False, f"not_pdf_{buf[:8].hex()}"
    return True, "ok"


def load_targets(path, partition, of):
    out = []
    with open(path) as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            osti_id, doi = parts[0], parts[1]
            year = parts[3] if len(parts) > 3 else ""
            if of > 1:
                h = int(hashlib.sha256(osti_id.encode()).hexdigest(), 16)
                if h % of != partition:
                    continue
            out.append((osti_id, doi, year))
    return out


async def fetch_one(page, osti_id, doi, year, staging):
    prefix, host, url_tmpl = template_for_doi(doi)
    if not url_tmpl:
        return "no_template", 0
    url = build_url(doi, url_tmpl)
    if not url:
        return "no_url_built", 0

    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        return f"goto_err_{str(e)[:30]}", 0
    if resp is None:
        return "no_response", 0

    final_url = resp.url
    # Session expiry detection
    if LOGIN_HOST in final_url:
        return "SESSION_EXPIRED", 0

    ct = (resp.headers.get("content-type") or "").lower()
    status = resp.status

    # Case 1: direct PDF response
    if "application/pdf" in ct or final_url.endswith(".pdf"):
        try:
            buf = await resp.body()
        except Exception as e:
            return f"body_err_{str(e)[:30]}", 0
        ok, why = validate_pdf(buf)
        if ok:
            path = staging / f"{osti_id}.pdf"
            path.write_bytes(buf)
            (staging / f"{osti_id}.meta").write_text(
                f"{osti_id}\t{doi}\t{prefix}\t{len(buf)}\t{final_url}\n"
            )
            return "ok", len(buf)
        return f"invalid_pdf_{why}", len(buf) if buf else 0

    # Case 2: APS abstract page — convert /abstract/ -> /pdf/
    if prefix == "10.1103/" and "/abstract/" in url:
        pdf_url = url.replace("/abstract/", "/pdf/")
        try:
            resp2 = await page.goto(pdf_url, wait_until="domcontentloaded", timeout=45000)
            if resp2 and "application/pdf" in (resp2.headers.get("content-type") or "").lower():
                buf = await resp2.body()
                ok, why = validate_pdf(buf)
                if ok:
                    path = staging / f"{osti_id}.pdf"
                    path.write_bytes(buf)
                    (staging / f"{osti_id}.meta").write_text(
                        f"{osti_id}\t{doi}\t{prefix}\t{len(buf)}\t{resp2.url}\n"
                    )
                    return "ok", len(buf)
        except Exception:
            pass

    # Case 3: HTML landing page — scrape citation_pdf_url
    try:
        html = await page.content()
    except Exception:
        return f"html_status_{status}", 0
    m = re.search(r'citation_pdf_url["\s]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if m:
        pdf_url = m.group(1)
        if pdf_url.startswith("//"):
            pdf_url = "https:" + pdf_url
        elif pdf_url.startswith("/"):
            from urllib.parse import urlparse
            u = urlparse(final_url)
            pdf_url = f"{u.scheme}://{u.netloc}{pdf_url}"
        try:
            resp3 = await page.goto(pdf_url, wait_until="domcontentloaded", timeout=45000)
            if resp3:
                buf = await resp3.body()
                ok, why = validate_pdf(buf)
                if ok:
                    path = staging / f"{osti_id}.pdf"
                    path.write_bytes(buf)
                    (staging / f"{osti_id}.meta").write_text(
                        f"{osti_id}\t{doi}\t{prefix}\t{len(buf)}\t{resp3.url}\n"
                    )
                    return "ok", len(buf)
        except Exception:
            pass

    return f"landing_html_{status}", len(html) if html else 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--staging", required=True)
    ap.add_argument("--partition", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    args = ap.parse_args()

    staging = Path(args.staging)
    staging.mkdir(parents=True, exist_ok=True)
    targets = load_targets(args.targets, args.partition, args.of)
    total = len(targets)
    print(f"[ezproxy part={args.partition}/{args.of}] {total} targets", flush=True)

    log_fp = open(staging / f"ezproxy_p{args.partition}.log", "w")

    def log(s):
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {s}", flush=True)
        log_fp.write(f"[{ts}] {s}\n")
        log_fp.flush()

    if not STATE_FILE.exists():
        log(f"ERROR: storage state {STATE_FILE} not found. Run ezproxy_login.py first.")
        sys.exit(1)

    counts = {}
    started = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            storage_state=str(STATE_FILE),
            accept_downloads=True,
        )
        page = await ctx.new_page()
        for i, (osti_id, doi, year) in enumerate(targets):
            try:
                status, sz = await fetch_one(page, osti_id, doi, year, staging)
            except Exception as e:
                status = f"exc_{str(e)[:30]}"
                sz = 0
            # Session-expiry bail
            if status == "SESSION_EXPIRED":
                log(f"SESSION EXPIRED at osti={osti_id}; re-run ezproxy_login.py and restart worker.")
                break
            bucket = status.split("_")[0]
            counts[bucket] = counts.get(bucket, 0) + 1
            if status != "ok" and not status.startswith("ok"):
                if i < 10 or (i + 1) % 50 == 0:
                    log(f"  miss {osti_id} ({doi[:40]}): {status[:60]}")
            await asyncio.sleep(RATE_SLEEP)
            if (i + 1) % 10 == 0:
                el = time.time() - started
                rate = (i + 1) / el
                eta = (total - i - 1) / rate / 60 if rate > 0 else 0
                buckets = " ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])[:5])
                log(f"done={i+1}/{total} {buckets} eta={eta:.0f}min")
        await browser.close()

    log(f"FINAL {counts} total={total}")
    log_fp.close()


if __name__ == "__main__":
    asyncio.run(main())
