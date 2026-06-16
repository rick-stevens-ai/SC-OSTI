# EZproxy + Playwright headless: institutional unlock for Cloudflare-walled publishers

**Discovered 2026-06-10, OSTI corpus refresh, Phase E EZproxy push.**

## Problem

Major commercial publishers — PNAS, ACS, Wiley (incl. AGU/Geophys journals on the
Wiley platform), Elsevier (Sciencedirect), IOP, AIP, Nature subscription titles —
hard-403 every script-side request regardless of:

- IP (cels, m1, cherryrd, prokko UChicago CS subnet 128.135.123.x all blocked)
- User-Agent (Chrome / Firefox / curl — all rejected)
- Cookie jar / persistent session via curl
- TLS / HTTP2 tuning (per the Springer cookies-stub finding, even good fingerprinting only takes you so far)

On the OSTI corpus refresh, this bucket dominated the residual `http_403` count:
**35,640 walled targets** (ACS 12.5K + APS 7.4K + Wiley 5.2K + AIP 3K + IOP 2.9K
+ PNAS 1.5K + Nature 1K + other 3K).

UChicago campus IP (prokko) is on the same allowlist hole as cels — these
publishers IP-license to the **library subnet only**, and that subnet is fronted
by EZproxy with CNetID + DUO auth. The CS subnet (where prokko sits) is not
licensed.

The only path that unlocks the wall is interactive EZproxy login + cookie capture
+ headless browser automation with the captured session.

## When to use this pattern

- Any bulk recovery task where the residual is dominated by Cloudflare-walled
  commercial publishers (PNAS, ACS, Wiley, Elsevier, AIP, IOP, Nature, RSC,
  Springer non-OA, Taylor & Francis).
- You have a UChicago CNetID and access to a Mac that can run a visible browser
  (prokko in our setup — needs physical screen access OR VNC/Screen Sharing).
- Target volume justifies the ~3h dev cost (~1K+ targets minimum).

Skip this pattern if:

- Pool is dominated by OA-publisher prefixes (Frontiers, PLoS, eLife, Nature OA,
  bioRxiv, Copernicus) — use `scripts/freeoa_fetcher.py` instead, no auth needed.
- Pool is dominated by physics preprints (APS/IOP/AIP DOIs) — use
  `scripts/arxiv_title.py` first, 50% recovery without any auth.
- Less than ~500 walled targets — manual browser session and the Save-As button
  is faster than the build.

## The two-stage pipeline

### Stage 1: Interactive login bootstrap (one-time per ~8h)

`scripts/ezproxy_login.py` opens a visible Chromium via Playwright, navigates to
a known proxied URL (e.g. `https://www-pnas-org.proxy.uchicago.edu/doi/pdf/...`).
EZproxy intercepts, redirects to the CNetID login page. Operator signs in,
completes DUO push, lands on the actual publisher PDF/article. Operator returns
to terminal, presses ENTER. The script calls
`browser_context.storage_state(path="~/.ezproxy_state.json")` and exits.

Storage state captures all cookies, localStorage, and sessionStorage scoped to
the EZproxy domain and all proxied publisher subdomains. Lives ~8h before
EZproxy session expires.

### Stage 2: Headless worker (runs against the cookie jar)

`scripts/ezproxy_worker.py` launches Chromium headless with
`browser.new_context(storage_state="~/.ezproxy_state.json")`. Iterates a TSV of
targets (`osti_id\tdoi\tpublisher\tyear\tlab`), navigates each DOI's canonical PDF
URL through the EZproxy rewrite pattern, captures the response body if it's a
PDF, falls back to scraping `citation_pdf_url` from the HTML landing page if the
direct hit returned HTML.

## EZproxy URL rewrite pattern

EZproxy rewrites publisher hostnames by replacing dots with dashes and appending
`.proxy.uchicago.edu`:

| Publisher | Original | Proxied |
|---|---|---|
| PNAS | `www.pnas.org` | `www-pnas-org.proxy.uchicago.edu` |
| ACS | `pubs.acs.org` | `pubs-acs-org.proxy.uchicago.edu` |
| Wiley | `onlinelibrary.wiley.com` | `onlinelibrary-wiley-com.proxy.uchicago.edu` |
| IOP | `iopscience.iop.org` | `iopscience-iop-org.proxy.uchicago.edu` |
| AIP | `pubs.aip.org` | `pubs-aip-org.proxy.uchicago.edu` |
| APS | `journals.aps.org` | `journals-aps-org.proxy.uchicago.edu` |
| Nature | `www.nature.com` | `www-nature-com.proxy.uchicago.edu` |
| Elsevier | `www.sciencedirect.com` | `www-sciencedirect-com.proxy.uchicago.edu` |

Per-publisher PDF URL templates (chained onto the proxied host):

- PNAS: `/doi/pdf/{doi}` — direct PDF.
- ACS: `/doi/pdf/{doi}` — direct PDF.
- Wiley: `/doi/pdfdirect/{doi}` — direct PDF (`/pdf/` works too but slower).
- IOP: `/article/{doi}/pdf` — direct PDF.
- AIP: `/aip/{journal}/article-pdf/doi/{doi}` — needs journal code, often easier
  to scrape from landing page.
- APS: `/abstract/{doi}` → follow to `/pdf/{doi}` (2-hop required, APS doesn't
  serve PDF directly on first hit).
- Nature: `/articles/{accession}.pdf` where `accession = doi.split("/", 1)[1]`.
- Elsevier: needs PII not DOI, so direct template doesn't work — scrape from
  landing page.

## Prokko-specific setup

Prokko = `<tailnet-host>` (Tailscale), public `128.135.123.251` (UChicago CS).

```bash
# Install Playwright + Chromium (one-time)
ssh prokko 'pip3 install --user playwright'
ssh prokko '~/Library/Python/3.9/bin/playwright install chromium'

# Ship scripts + targets
scp ezproxy_login.py ezproxy_worker.py ezproxy_targets.tsv prokko:~/
```

The `playwright` binary lands in `~/Library/Python/3.9/bin/` which isn't on
PATH — invoke via full path. macOS Monterey, Python 3.9.6 (system) is fine for
Playwright 1.60.

## Hard rules for the login script

1. **Run on the physical screen**, not over ssh. DUO push notifications target
   the registered device, but the browser must be visible to complete the
   challenge. If you have to do it remotely, use Screen Sharing / VNC to the
   Mac, not ssh -X (DUO TOTP code entry is harder over forwarded X).
2. **Do NOT headless the login script**. EZproxy uses a JS-driven SAML form that
   needs a real browser context. Even `headless=True` with proper UA breaks.
3. **Validate the state file is valid before storage_state() return** by
   asserting the final URL is past the login (e.g. matches publisher host, not
   `login.proxy.uchicago.edu`). If user pressed ENTER before login completed,
   storage_state captures partial cookies that fail in the worker.

## Hard rules for the worker

1. **Rate-limit aggressively**. EZproxy logs every request with the user's
   CNetID; bulk scraping triggers UChicago Library abuse alerts. Use a **3s
   sleep between requests, single-threaded**. The worker is throughput-limited
   by politeness, not by IO. Plan ~20K requests / 24h max per session.
2. **Storage state expires ~8h**. Build the worker to detect expiry (final URL
   matches `login.proxy.uchicago.edu`) and exit cleanly so operator can re-bootstrap.
3. **Watch for downloads, not navigations.** Some publishers serve the PDF as
   a `Content-Disposition: attachment` download instead of inline. The worker
   uses `accept_downloads=True` on the context and `page.expect_download()` for
   pages that trigger that path. Detect by `Content-Disposition` header on the
   navigation response.
4. **Validate every PDF by three witnesses** (magic byte, size ≥ 4096, text
   extractable) — same rule as the rest of the OSTI fetch stack. EZproxy can
   return interstitial HTML pages with HTTP 200 if the session is mid-renew.

## Validation script anti-patterns

These look right but don't work:

- **`page.pdf()`** generates a screenshot-style PDF of the rendered page, NOT
  the publisher's actual PDF. Useless. Always go through `page.goto()` and read
  `response.body()` for PDF content.
- **`page.evaluate("() => fetch(pdfUrl).then(r => r.arrayBuffer())")`** runs in
  the page's JS context with the page's CORS rules; many publisher PDFs are
  cross-origin and fetch fails with CORS error. Use Playwright's `page.goto()`
  which carries the cookies natively.
- **Trying to reuse the storage state across publishers in a single session**
  works for cookies but localStorage is per-domain. Use one Playwright context
  per session run; don't try to multiplex contexts to save startup overhead.

## Empirical results

Not yet run end-to-end as of 2026-06-10 evening — login is gated on operator
interactive session next morning. Smoke-tested the URL templates against
publicly-accessible OA pages (Nature OA, Frontiers) which validate the
Playwright launch path. Update this section with real recovery rates after
first full session.

**Expected** (best case, all walled publishers respond to authed UChicago session):

- PNAS: ~70% recovery (some 2025 papers may be embargoed)
- ACS: ~85% recovery (UChicago has full ACS license)
- Wiley: ~80% recovery (some specialty journals not in UChicago bundle)
- Elsevier: ~75% recovery
- IOP: ~80% (assuming the SAML cookie also covers the Radware CAPTCHA)
- AIP: ~75%
- APS: ~85%
- Nature subscription: ~70%

**Total projected recovery**: 25-28K PDFs from the 35K walled bucket = **70-80%
recovery rate on the most-recoverable residual bucket** in the corpus.

## Generalization beyond OSTI

This pattern works for any bulk-content task where the wall is institutional
licensing:

- Bulk-downloading a publisher journal archive your library subscribes to.
- Scraping clinical trial registries that require institutional access.
- Pulling course materials from JSTOR / Project MUSE / SAGE Journals.

The two-stage shape (interactive bootstrap + headless worker against saved
state) is the right primitive for any "user has an account, agent needs to
operate as them" task that doesn't have a real API. The same `storage_state`
pattern works for arbitrary websites — not Playwright-specific to EZproxy.

## Cookie jar storage

`~/.ezproxy_state.json` should be chmod 600. It's a session cookie, not a
persistent credential, but treat as sensitive — UChicago CNetID sessions can
be abused if leaked. Don't commit to git, don't sync to Dropbox, don't ship
between hosts. Each host that needs the worker runs its own `ezproxy_login.py`.

If the operator is on a Mac with Touch ID, the Chromium profile can use Apple
Keychain for the CNetID password autofill, making subsequent re-logins faster
(still need DUO push every time).
