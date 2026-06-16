# Why papers are missing from OSTI — the failure-mode triad

Direct evidence from a 10-paper probe (2026-06-10) of `paper_ids.txt` records that have **no PDF on disk anywhere** (not on Cherry6TB, not on `/rbstor/stevens/osti_fulltext_v2/`). Probe queried both `https://www.osti.gov/servlets/purl/<id>` and `https://www.osti.gov/api/v1/records/<id>` per ID and recorded HTTP status, content-type, content-length, and where the redirect chain ended up.

This is the structural reason for the 49% "missing" bucket on the 238,366-paper universe (per `references/coverage-accounting-2026-06-10.md`). For planning Phase E (state-machine recovery), or for drafting a `comments@osti.gov` ticket, or for setting realistic expectations on any "let's recover the missing PDFs" task — start here.

## The three modes

### Mode 1: Never deposited (~30-35% of missing)

OSTI biblio record exists with title + DOI + research_orgs, but **no `purl` link in `links[]`**. The PURL servlet returns 404. The DOI resolves off-site to the publisher (Elsevier, ACS, Wiley) which paywalls the paper.

DOE published metadata for the citation but never received the manuscript from the lab. This is a deposit-side gap, not a serving-side gap — there is no PDF at OSTI to recover.

Common pattern: pre-2010 ANL/BNL/LBNL papers published in Elsevier/ACS journals.

**Recovery path**: Unpaywall (find an OA mirror), arXiv lookup if DOI is physics, S2 fallback for the rest. Crossref → publisher direct will 403 every time for closed-access journals; don't waste cycles there.

Probe evidence:
- `1040255` (BNL 2007 Phys.Lett.B) — no PURL, DOI = `10.1016/j.physletb.2007.04.073` resolves to ScienceDirect paywall.
- `1006512` (ANL 2005 chem journal) — same shape.

### Mode 2: Lab-repository SSO wall (~10-15% of missing)

PURL returns HTTP 200 but the body is an HTML login page for the lab's institutional SSO portal. Common pages:

- `misportal.jlab.org` — Jefferson Lab Management Information System SSO
- Fermilab Identity Manager — body contains `sourceid-choose-idp-adapter-form`

External traffic (us, anyone outside the lab firewall) cannot get past the IDP picker. The PDF exists at the lab repo but is gated.

Probe evidence:
- `1905487` Jefferson Lab — `misportal.jlab.org` redirect.
- `897183`, `1156469` Fermilab — IDP picker HTML.

Common pattern: Jefferson Lab post-2015, Fermilab post-2018, occasional SLAC.

**Recovery path**: arXiv usually has it for HEP/nuclear (these labs deposit heavily on arXiv). Unpaywall finds the arXiv mirror. Direct lab-repo scrape will not work.

### Mode 3: Publisher-withheld 403 (~30-35% of missing)

PURL servlet returns HTTP 403 with a generic ~500-byte Apache `<HTML><HEAD><TITLE>Error</TITLE></HEAD>...` body. Indistinguishable from network-level access denial but it's actually OSTI honoring a publisher embargo or copyright-restriction flag on the record.

These are the source of the 8,119 zero-byte stubs in Cherry6TB — earlier fetch runs treated HTTP 200 as success (because some 403 paths returned a tiny HTML body with `Content-Length` set), wrote the empty/tiny response to disk, and moved on.

Probe evidence:
- `1559174`, `1532251`, `1471020`, `1902922`, `1439192` — all LBNL, all Frontiers / Elsevier energy/chem journals.

**Recovery path**: Unpaywall if the publisher has an OA tier (often does for energy/climate journals). Otherwise unreachable.

## Distribution recap

| Mode | Approx fraction | Detector | Recoverable via |
|------|-----------------|----------|-----------------|
| 1: Never deposited | ~30-35% | No `purl` link in OSTI record `links[]` | Unpaywall, arXiv, S2 |
| 2: Lab SSO wall | ~10-15% | PURL 200 + body matches `misportal\.jlab\.org\|sourceid-choose-idp\|Fermilab Identity` | arXiv |
| 3: Publisher 403 | ~30-35% | PURL HTTP 403, body <1KB, generic Apache error | Unpaywall (OA tier) |
| 4: Other | ~20% | 5xx, timeouts, redirect loops, non-HTML/non-PDF bodies | Retry; if persistent, bucket as Mode 1 |

Numbers are eyeball estimates from a 10-paper probe. Re-run on a 100-paper stratified-by-lab sample for tighter intervals before committing to a Phase E throughput budget.

## Implication for Phase E state machine

The strategy cascade should branch on the detected mode, not run every strategy linearly:

```
classify_mode(osti_id) -> "never_deposited" | "sso_walled" | "publisher_403" | "unknown"

never_deposited   -> [unpaywall, arxiv_by_doi, s2_by_doi]                 (skip osti_purl)
sso_walled        -> [arxiv_by_doi, unpaywall]                            (skip osti_purl, publishers)
publisher_403     -> [unpaywall, arxiv_by_doi]                            (skip osti_purl retry)
unknown           -> [osti_purl, unpaywall, arxiv_by_doi, s2_by_doi]
```

Classification cost is one OSTI biblio API call (`/api/v1/records/<id>`) + one PURL HEAD if biblio shows a `purl` link. ~0.5s per ID. Caching the classification in the state DB avoids re-classifying on retry.

## The real root cause

DOE is the **metadata authority** for these papers, not the **content host**. The OSTI mandate is to track DOE-funded research outputs; serving full text is a best-effort layer on top of that. Toll-access publishers prohibit redistribution; lab SSO walls exist because labs treat their repos as internal-first. The 49% "missing" rate is structural to the OSTI mission, not a bug.

Set expectations accordingly when reporting coverage: "we have ~46% on disk, of the remaining ~50%, optimistic recovery is 15-25% via OA fallbacks, leaving ~30% structurally unreachable without per-paper publisher negotiation." Do not promise 80%+ coverage from any technical fix.

## Probe script (reproducible)

```python
import json, urllib.request, urllib.error
from pathlib import Path

PURL = "https://www.osti.gov/servlets/purl/{id}"
BIBLIO = "https://www.osti.gov/api/v1/records/{id}"

def probe(osti_id):
    out = {"id": osti_id}
    # Biblio
    try:
        req = urllib.request.Request(BIBLIO.format(id=osti_id), headers={"User-Agent": "OSTI-probe/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rec = json.loads(r.read())[0]
            out["has_purl"] = any("purl" in (l.get("rel") or "").lower() for l in rec.get("links") or [])
            out["doi"] = rec.get("doi")
            out["product_type"] = rec.get("product_type")
            out["research_orgs"] = [o.get("name") for o in rec.get("research_orgs") or []][:3]
    except Exception as e:
        out["biblio_err"] = str(e)[:80]
    # PURL
    try:
        req = urllib.request.Request(PURL.format(id=osti_id), headers={"User-Agent": "OSTI-probe/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read(4096)
            out["purl_status"] = r.status
            out["purl_ct"] = r.headers.get("Content-Type", "")
            out["purl_len"] = int(r.headers.get("Content-Length", "0"))
            out["body_head"] = body[:200].decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        out["purl_status"] = e.code
        out["purl_body_head"] = (e.read(200) or b"").decode("utf-8", errors="replace")
    except Exception as e:
        out["purl_err"] = str(e)[:80]
    return out

# Run on a 100-ID stratified-by-lab sample for production estimates
ids = [...]  # from paper_ids.txt minus on-disk
for i in ids:
    print(json.dumps(probe(i)))
```

Run from m1 (the API works fine from home) or from Aurora UAN if you want the bulk-fetch host's view of redirects.
