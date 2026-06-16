#!/usr/bin/env python3
"""Citation-database email triangulation validator.

Companion to scripts/run_email_validation_smtp.py (the SMTP battery).
Where the SMTP battery answers "does this mailbox exist?", this script
answers "does this (osti_id, email, name_hint) binding agree with what
authoritative third-party citation databases say about the same paper?"

Four methods, all free, no API key, no LLM calls:
  M1 OpenAlex   /works/doi:<doi>     -> authorships -> name+affil match
  M2 Crossref   /works/<doi>         -> authors -> name+affil match
  M3 OSTI       /api/v1/records/<id> -> authors[] bracketed string
                                        -> name + bracket-affil + ORCID
  M4 DOI page   doi.org/<doi>        -> scrape HTML for verbatim email

Verdict ladder (priority order, strongest first):
  CONFIRMED  email verbatim on DOI landing page
  STRONG     name+affil agreement from ALL 3 sources
  LIKELY     name+affil agreement from 2 sources
  PROBABLE   name+affil agreement from 1 source
  WEAK       name match only, no affil agreement
  UNVERIFIED no positive signal

Web-search engines (Bing/Google/DDG) were tried and KILLED — see
references/email-validation-citation-databases-2026-06-09.md for the
failure-mode breakdown. Don't add them back without a paid Search API.

CUSTOMIZE for a non-OSTI corpus:
  1. Replace m3_osti_api with the equivalent for your registry
     (or drop it entirely — M1+M2 alone reach LIKELY for most records).
  2. Extend DOMAIN_HINTS with the institutional domains your sample
     stresses; the prefix heuristic below handles the long tail.
  3. Adjust the sample loader at the bottom for your input shape.
"""

from __future__ import annotations
import json, re, socket, ssl, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = "Mozilla/5.0 (KuklaAgent/1.0; +kukla@kd9nwa.org) email-validation-research"
TIMEOUT = 20
HTTP_CTX = ssl.create_default_context()

# ----- domain -> institution name hints ----------------------------------
DOMAIN_HINTS = {
    "anl.gov": ["argonne"],
    "ornl.gov": ["oak ridge"],
    "pnnl.gov": ["pacific northwest"],
    "lbl.gov": ["lawrence berkeley", "berkeley lab", "lbnl"],
    "berkeley.edu": ["berkeley", "uc berkeley"],
    "slac.stanford.edu": ["slac", "stanford"],
    "stanford.edu": ["stanford"],
    "fnal.gov": ["fermilab", "fermi national"],
    "bnl.gov": ["brookhaven"],
    "lanl.gov": ["los alamos"],
    "llnl.gov": ["livermore", "llnl"],
    "nrel.gov": ["nrel", "renewable energy laboratory"],
    "sandia.gov": ["sandia"],
    "inl.gov": ["idaho national"],
    "nist.gov": ["nist", "national institute of standards"],
    "jlab.org": ["jefferson lab"],
    "ameslab.gov": ["ames"],
    "pppl.gov": ["princeton plasma"],
    "cern.ch": ["cern"],
}

# Abbreviation expansions for domain prefixes whose institution name
# doesn't contain the prefix as a substring.
ABBREV_EXPANSIONS = {
    "utk": "tennessee", "uic": "illinois at chicago", "rpi": "rensselaer",
    "vt": "virginia tech", "ucalgary": "calgary", "anu": "australian national",
    "ustb": "university of science and technology beijing",
    "cumt": "china university of mining", "buaa": "beihang",
    "hhu": "hohai", "knu": "kyungpook",
    "cup": "china university of petroleum",
    "neu": "northeastern", "uky": "kentucky",
}

# Generic domain components to skip when deriving institution name from
# domain prefix (e.g. 'physics.rutgers.edu' -> 'rutgers', not 'physics').
GENERIC_DOMAIN_PARTS = {
    "edu", "gov", "org", "com", "net", "ac", "cn", "uk", "eu", "us", "de",
    "fr", "it", "ca", "au", "jp", "kr", "ch", "es", "in", "il", "nz",
    "mail", "physics", "chem", "cs", "ece", "cec", "sas", "skl", "sis",
    "math", "phys",
}


def affiliation_matches_domain(domain: str, affil_strings: list) -> bool:
    """Three-tier cascade: curated hints, domain-prefix heuristic, abbrevs."""
    if not affil_strings:
        return False
    joined = " ".join(affil_strings).lower()
    for h in DOMAIN_HINTS.get(domain, []):
        if h in joined:
            return True
    parts = [p for p in domain.lower().split(".")
             if p not in GENERIC_DOMAIN_PARTS and len(p) >= 3]
    for p in parts:
        if p in joined:
            return True
        exp = ABBREV_EXPANSIONS.get(p)
        if exp and exp in joined:
            return True
    return False


# ----- shared HTTP helpers -----------------------------------------------

def http_get(url: str, accept: str = "*/*"):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": accept,
                 "Accept-Language": "en-US,en;q=0.9"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=HTTP_CTX) as r:
            body = r.read()
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = body.decode("latin-1", errors="replace")
            return r.status, text, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return e.code, text, dict(e.headers or {})
    except Exception as e:  # noqa: BLE001 — broad except is correct for parallel fetch
        return -1, f"ERR:{type(e).__name__}:{e}", {}


def name_overlap(name_a: str, name_b: str) -> float:
    """Jaccard over normalized name tokens; ignores initials."""
    def tok(n):
        n = re.sub(r"[^a-z\s]", " ", (n or "").lower())
        return {t for t in n.split() if len(t) >= 3}
    a, b = tok(name_a), tok(name_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ----- the four methods --------------------------------------------------

def m1_openalex(doi: str, name_hint: str, email_domain: str) -> dict:
    out = {"found_name": False, "found_affil": False, "affils": [],
           "ok": False, "ms": 0, "note": ""}
    if not doi:
        out["note"] = "no_doi"; return out
    t0 = time.time()
    url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi, safe='')}"
    status, body, _ = http_get(url, accept="application/json")
    out["ms"] = int((time.time() - t0) * 1000)
    if status != 200:
        out["note"] = f"openalex_http_{status}"; return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        out["note"] = "openalex_bad_json"; return out
    matched_affils = []
    for a in data.get("authorships", []) or []:
        disp = (a.get("author") or {}).get("display_name", "")
        if name_hint and name_overlap(disp, name_hint) >= 0.5:
            out["found_name"] = True
            ras = a.get("raw_affiliation_string")
            if ras:
                matched_affils.append(ras)
            for inst in a.get("institutions") or []:
                n = inst.get("display_name")
                if n:
                    matched_affils.append(n)
    out["affils"] = matched_affils
    out["found_affil"] = affiliation_matches_domain(email_domain, matched_affils)
    out["ok"] = bool(out["found_name"])
    return out


def m2_crossref(doi: str, name_hint: str, email_domain: str) -> dict:
    out = {"found_name": False, "found_affil": False, "affils": [],
           "ok": False, "ms": 0, "note": ""}
    if not doi:
        out["note"] = "no_doi"; return out
    t0 = time.time()
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='')}"
    status, body, _ = http_get(url, accept="application/json")
    out["ms"] = int((time.time() - t0) * 1000)
    if status != 200:
        out["note"] = f"crossref_http_{status}"; return out
    try:
        data = json.loads(body).get("message", {})
    except json.JSONDecodeError:
        out["note"] = "crossref_bad_json"; return out
    matched_affils = []
    for a in data.get("author", []) or []:
        full = f"{a.get('given','')} {a.get('family','')}".strip()
        if name_hint and name_overlap(full, name_hint) >= 0.5:
            out["found_name"] = True
            for af in a.get("affiliation", []) or []:
                n = af.get("name")
                if n:
                    matched_affils.append(n)
    out["affils"] = matched_affils
    out["found_affil"] = affiliation_matches_domain(email_domain, matched_affils)
    out["ok"] = bool(out["found_name"])
    return out


def m3_osti_api(osti_id: str, name_hint: str, email_domain: str) -> dict:
    """OSTI JSON API parser. Also extracts ORCID for high-value provenance.

    Author string shape:
      "Lu, Jun [Argonne National Lab. (ANL), Lemont, IL (United States)] (ORCID:...)"
    """
    out = {"found_name": False, "found_affil": False, "affils": [],
           "orcid": None, "ok": False, "ms": 0, "note": "", "status": 0}
    t0 = time.time()
    status, body, _ = http_get(
        f"https://www.osti.gov/api/v1/records/{osti_id}",
        accept="application/json",
    )
    out["ms"] = int((time.time() - t0) * 1000)
    out["status"] = status
    if status != 200:
        out["note"] = f"osti_api_http_{status}"; return out
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        out["note"] = "osti_api_bad_json"; return out
    rec = data[0] if isinstance(data, list) and data else (data if not isinstance(data, list) else {})
    matched_affils = []
    for author_str in rec.get("authors", []) or []:
        m_name = re.match(r"^([^\[]+?)(?:\s*\[|\s*\(ORCID|$)", author_str)
        name = (m_name.group(1).strip().rstrip(",") if m_name else "").strip()
        affils = re.findall(r"\[([^\]]+)\]", author_str)
        if name_hint and name_overlap(name, name_hint) >= 0.5:
            out["found_name"] = True
            m_orcid = re.search(r"ORCID:\s*([\dX]+)", author_str)
            if m_orcid:
                out["orcid"] = m_orcid.group(1)
            matched_affils.extend(affils)
    out["affils"] = matched_affils
    out["found_affil"] = affiliation_matches_domain(email_domain, matched_affils)
    out["ok"] = bool(out["found_name"] and out["found_affil"])
    return out


def has_email(haystack: str, email: str) -> bool:
    """Detect email in text, tolerating obfuscations ([at], DOT, etc.)."""
    if not haystack:
        return False
    e = email.lower()
    if e in haystack.lower():
        return True
    local, domain = e.split("@", 1)
    dot_alt = r"(?:\.| dot |\(dot\))"
    domain_pat = re.escape(domain).replace(r"\.", dot_alt)
    pat = re.compile(
        rf"{re.escape(local)}\s*(?:@|\(at\)|\[at\]|\s+at\s+)\s*{domain_pat}",
        re.IGNORECASE,
    )
    return bool(pat.search(haystack))


def m4_doi_landing(doi: str, email: str) -> dict:
    out = {"found": False, "ok": False, "ms": 0, "note": "",
           "status": 0, "final_url": ""}
    if not doi:
        out["note"] = "no_doi"; return out
    t0 = time.time()
    req = urllib.request.Request(
        f"https://doi.org/{doi}",
        headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.5"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=HTTP_CTX) as r:
            out["status"] = r.status
            out["final_url"] = r.geturl()
            body = r.read(2_000_000).decode("utf-8", errors="replace")
            out["found"] = has_email(body, email)
            out["ok"] = out["found"]
    except urllib.error.HTTPError as e:
        out["status"] = e.code; out["note"] = f"doi_http_{e.code}"
    except Exception as e:  # noqa: BLE001
        out["status"] = -1; out["note"] = f"doi_err_{type(e).__name__}"
    out["ms"] = int((time.time() - t0) * 1000)
    return out


# ----- per-record orchestrator + verdict -----------------------------------

def run_one(rec: dict, paper_lookup: dict) -> dict:
    osti_id = rec["osti_id"]
    email = rec["email"]
    name_hint = rec.get("name_hint", "") or ""
    domain = email.split("@", 1)[1].lower() if "@" in email else ""
    doi = (paper_lookup.get(osti_id) or {}).get("doi") or ""

    result = {"osti_id": osti_id, "email": email, "name_hint": name_hint, "doi": doi}
    result["m1_openalex"] = m1_openalex(doi, name_hint, domain)
    result["m2_crossref"] = m2_crossref(doi, name_hint, domain)
    result["m3_osti"] = m3_osti_api(osti_id, name_hint, domain)
    result["m4_doi"] = m4_doi_landing(doi, email)

    direct = result["m4_doi"]["found"]
    affil_sources = sum(
        1 for r in (result["m1_openalex"], result["m2_crossref"], result["m3_osti"])
        if r.get("found_name") and r.get("found_affil")
    )
    name_sources = sum(
        1 for r in (result["m1_openalex"], result["m2_crossref"], result["m3_osti"])
        if r.get("found_name")
    )
    if direct:
        result["verdict"] = "CONFIRMED"
    elif affil_sources >= 3:
        result["verdict"] = "STRONG"
    elif affil_sources >= 2:
        result["verdict"] = "LIKELY"
    elif affil_sources == 1:
        result["verdict"] = "PROBABLE"
    elif name_sources >= 1:
        result["verdict"] = "WEAK"
    else:
        result["verdict"] = "UNVERIFIED"
    return result


# ----- driver --------------------------------------------------------------
# CUSTOMIZE these for your corpus: SAMPLE input path, OUT output path,
# and the paper_lookup loader.

if __name__ == "__main__":
    import sqlite3
    HERE = Path(__file__).resolve().parent
    SAMPLE = HERE / "sample.jsonl"
    OUT = HERE / "results_web.jsonl"

    # Load DOIs (CUSTOMIZE: change to your metadata source)
    conn = sqlite3.connect("/Users/stevens/Dropbox/XFER/osti-contacts/contacts.db")
    paper_lookup = {row[0]: {"doi": row[2]}
                    for row in conn.execute("SELECT osti_id, title, doi, journal FROM paper")}
    conn.close()
    print(f"loaded {len(paper_lookup):,} papers", flush=True)

    records = [json.loads(l) for l in open(SAMPLE)]
    print(f"sample size: {len(records)}", flush=True)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(run_one, r, paper_lookup): r for r in records}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                rec = futs[fut]
                res = {"osti_id": rec["osti_id"], "email": rec["email"],
                       "verdict": "ERROR", "error": str(e)}
            results.append(res)
            if i % 10 == 0:
                print(f"  {i:3d}/{len(records)} done  elapsed={time.time()-t0:.0f}s",
                      flush=True)

    OUT.write_text("\n".join(json.dumps(r) for r in results) + "\n")
    print(f"wrote {OUT}", flush=True)

    from collections import Counter
    v = Counter(r.get("verdict", "?") for r in results)
    print("\n=== verdict breakdown ===")
    for k in ("CONFIRMED", "STRONG", "LIKELY", "PROBABLE", "WEAK", "UNVERIFIED", "ERROR"):
        print(f"  {k:11s}: {v.get(k,0):3d}")
    print("\n=== per-method positive rate ===")
    for m in ("m1_openalex", "m2_crossref", "m3_osti", "m4_doi"):
        hits = sum(1 for r in results if r.get(m, {}).get("ok"))
        print(f"  {m:13s}: {hits:3d}/{len(results)}")
    print(f"\nelapsed: {time.time()-t0:.1f}s")
