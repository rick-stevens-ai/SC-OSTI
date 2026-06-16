#!/usr/bin/env python3
"""End-to-end findability pipeline template for a corpus of cards/reports.

Stages:
  1. Parse card text → extract structural signals (DOIs, URLs, accessions)
  2. DOI cleanup + doi.org resolution (multi-candidate, let resolver decide)
  3. Registry validation for each accession + DataCite check on DOIs
  4. Per-card verdict: FOUND_DEPOSIT / FOUND_DOI_ONLY / BROKEN_SIGNALS / NO_SIGNALS

Pure stdlib. Python 3.10+ for `str | None` syntax (on this M1 mac use
/opt/homebrew/bin/python3.13).

Usage:
    python3.13 cards_findability_pipeline.py <input_dir> <output_jsonl> [n_workers=4]

Worked baseline (OSTI data cards sample-200, 2026-06-05):
    33s wall for 200 cards at 6 workers × 8-way signal parallelism
    12% FOUND_DEPOSIT, 51% FOUND_DOI_ONLY, 33% NO_SIGNALS, 4% BROKEN
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


UA = "cards-findability/0.1"

# ---------- Extraction patterns ----------
DOI_RX = re.compile(r"\b(10\.\d{4,9}/[\w.\-/:()#]+?)(?=[\s,;\"\'\)\]]|$)", re.I)
URL_RX = re.compile(r'https?://[\w\-._~:/?#\[\]@!$&\'()*+,;=%]+', re.I)

ACCESSION_PATTERNS = {
    "GenBank": re.compile(r"\b([A-Z]{1,2}\d{5,8}|[A-Z]{4,6}\d{8,10})\b"),
    "GEO":     re.compile(r"\b(GSE\d{2,7}|GDS\d{2,6}|GSM\d{2,8})\b"),
    "SRA":     re.compile(r"\b(SR[APRSXZ]\d{4,8}|PRJ[ENPD][A-Z]?\d{4,8})\b"),
    "PRIDE":   re.compile(r"\b(PXD\d{6,8})\b"),
    # PDB ID needs context anchor — bare [1-9][A-Z0-9]{3} matches dates+DOI fragments
    "PDB":     re.compile(r"(?:PDB[:\s]+|pdb\s+id\s+|RCSB\s+)([1-9][A-Z0-9]{3})\b", re.I),
    "Zenodo":  re.compile(r"zenodo\.org/(?:record|doi/10\.5281/zenodo\.)/?(\d{5,10})", re.I),
    "Figshare": re.compile(r"figshare\.com/[\w/]*?(\d{6,})", re.I),
}

DATA_HOST_DOMAINS = (
    "zenodo.org", "figshare.com", "datadryad.org", "dryad", "osf.io",
    "github.com", "gitlab.com", "ncbi.nlm.nih.gov", "ebi.ac.uk",
    "pdb.org", "rcsb.org", "uniprot.org", "datacommons", "kaggle.com",
    "huggingface.co", "data.gov", "doi.pangaea.de", "ess-dive.lbl.gov",
    "materialsproject.org", "nomad-lab.eu", "globus.org",
)


# ---------- Stage 2b: smart DOI cleanup ----------
def doi_candidates(doi: str) -> list[str]:
    """Generate cleanup variants of a possibly-mangled DOI."""
    seen = []

    def add(d):
        if d and d not in seen and re.match(r"^10\.\d{4,9}/", d):
            seen.append(d)

    add(doi)
    add(doi.rstrip(".,;:)]}\\\""))
    add(re.sub(r"(?<=\d)([A-Za-z][\w.\-/]*?)$", "", doi))
    add(re.sub(r"(?<=\d)([A-Z][a-z][\w]*)$", "", doi))
    m = re.match(r"^(10\.\d+/[\w.\-]+\d{4,})(\d{1,3})$", doi)
    if m:
        add(m.group(1))
    m2 = re.match(r"^(10\.\d+/[\w.\-]+)(\d{1,2})$", doi)
    if m2 and m2.group(1) != doi:
        add(m2.group(1))
    for trim in (1, 2, 3):
        m3 = re.match(rf"^(10\.\d+/[\w.\-]+?)(.{{{trim}}})$", doi)
        if m3 and m3.group(1) != doi:
            add(m3.group(1))
    return seen


def resolve_doi(doi: str, timeout: int = 10) -> dict:
    """Try cleanup candidates in order; first that returns 2xx/3xx/403 wins."""
    cands = doi_candidates(doi)
    for c in cands:
        url = f"https://doi.org/{c}"
        try:
            req = Request(url, method="HEAD", headers={"User-Agent": UA})
            with urlopen(req, timeout=timeout) as r:
                return {"raw": doi, "matched_candidate": c, "status": r.status,
                        "ok": True, "n_tried": cands.index(c) + 1}
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308, 403):
                return {"raw": doi, "matched_candidate": c, "status": e.code,
                        "ok": True, "n_tried": cands.index(c) + 1,
                        "note": "paywall" if e.code == 403 else None}
            continue
        except Exception:
            continue
    return {"raw": doi, "matched_candidate": None, "ok": False, "n_tried": len(cands)}


# ---------- Stage 4: registry validators ----------
def _get_json(url: str, timeout: int = 15) -> dict | None:
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def check_datacite(doi: str) -> dict:
    data = _get_json(f"https://api.datacite.org/dois/{quote(doi, safe='/')}")
    if not data or "data" not in data:
        return {"registry": "DataCite", "ok": False, "doi": doi}
    attrs = data["data"].get("attributes", {}) or {}
    types = attrs.get("types", {}) or {}
    return {
        "registry": "DataCite", "ok": True, "doi": doi,
        "title": (attrs.get("titles") or [{}])[0].get("title", ""),
        "type_general": types.get("resourceTypeGeneral", ""),
        "publisher": attrs.get("publisher", ""),
    }


def check_zenodo(record_id: str) -> dict:
    m = re.search(r"(\d{4,})", record_id)
    if not m:
        return {"registry": "Zenodo", "ok": False}
    rid = m.group(1)
    data = _get_json(f"https://zenodo.org/api/records/{rid}")
    if not data:
        return {"registry": "Zenodo", "ok": False, "record_id": rid}
    files = data.get("files", []) or []
    metadata = data.get("metadata", {}) or {}
    return {
        "registry": "Zenodo", "ok": True, "record_id": rid,
        "title": metadata.get("title", ""),
        "type": metadata.get("resource_type", {}).get("type", ""),
        "n_files": len(files),
        "total_bytes": sum(f.get("size", 0) for f in files),
    }


def check_pride(accession: str) -> dict:
    data = _get_json(f"https://www.ebi.ac.uk/pride/ws/archive/v3/projects/{accession}")
    if not data:
        return {"registry": "PRIDE", "ok": False, "accession": accession}
    return {"registry": "PRIDE", "ok": True, "accession": accession,
            "title": data.get("title", "")}


def check_geo(accession: str) -> dict:
    try:
        req = Request(f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",
                      headers={"User-Agent": UA})
        with urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="ignore")
        if "could not find" in body.lower():
            return {"registry": "GEO", "ok": False, "accession": accession}
        n_samples_m = re.search(r"Samples\s*\((\d+)\)", body)
        return {"registry": "GEO", "ok": True, "accession": accession,
                "n_samples": int(n_samples_m.group(1)) if n_samples_m else None}
    except Exception:
        return {"registry": "GEO", "ok": False, "accession": accession}


def check_sra(accession: str) -> dict:
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    s = _get_json(f"{base}/esearch.fcgi?db=sra&term={accession}&retmode=json")
    if not s or not s.get("esearchresult", {}).get("idlist"):
        return {"registry": "SRA", "ok": False, "accession": accession}
    return {"registry": "SRA", "ok": True, "accession": accession,
            "uid": s["esearchresult"]["idlist"][0]}


def check_figshare(article_id: str) -> dict:
    m = re.search(r"(\d{6,})", article_id)
    if not m:
        return {"registry": "Figshare", "ok": False}
    aid = m.group(1)
    data = _get_json(f"https://api.figshare.com/v2/articles/{aid}")
    if not data:
        return {"registry": "Figshare", "ok": False, "article_id": aid}
    return {"registry": "Figshare", "ok": True, "article_id": aid,
            "title": data.get("title", ""),
            "n_files": len(data.get("files", []) or []),
            "size": data.get("size", 0)}


# ---------- Stage 1: parse ----------
def parse_card(path: Path) -> dict:
    text = path.read_text(errors="ignore")
    out = {"file": path.name, "card_id": path.stem.split("_")[0]}

    # Pull common YAML fields if present
    for key in ("dataset_name", "dataset_type", "version"):
        m = re.search(rf'^\s*{re.escape(key)}\s*:\s*["\']?([^"\'\n]+)', text, re.M)
        if m and "not specified" not in m.group(1).lower():
            out[key] = m.group(1).strip()

    dois = set()
    for m in DOI_RX.finditer(text):
        d = m.group(1).rstrip(".,;:)\"']")
        if "not" not in d.lower():
            dois.add(d)
    out["dois"] = sorted(dois)

    urls = set()
    for m in URL_RX.finditer(text):
        urls.add(m.group(0).rstrip(".,;:)\"']"))
    out["urls"] = sorted(urls)
    out["data_host_urls"] = [u for u in out["urls"]
                              if any(d in u for d in DATA_HOST_DOMAINS)]

    accessions = {}
    for kind, rx in ACCESSION_PATTERNS.items():
        hits = set(m.group(1) for m in rx.finditer(text))
        if hits:
            accessions[kind] = sorted(hits)
    out["accessions"] = accessions
    return out


def signals_for_validation(card: dict) -> list[tuple[str, str]]:
    sigs = [("DOI", d) for d in card.get("dois", [])]
    for k, vs in card.get("accessions", {}).items():
        for v in vs:
            sigs.append((k, v))
    return sigs


def validate_one(kind: str, value: str) -> dict:
    out = {"kind": kind, "value": value}
    try:
        if kind == "DOI":
            r1 = resolve_doi(value, timeout=10)
            out["doi_resolution"] = r1
            if r1.get("ok") and r1.get("matched_candidate"):
                out["datacite"] = check_datacite(r1["matched_candidate"])
            out["ok"] = bool(r1.get("ok"))
        elif kind == "Zenodo":
            out["zenodo"] = check_zenodo(value); out["ok"] = bool(out["zenodo"].get("ok"))
        elif kind == "PRIDE":
            out["pride"] = check_pride(value); out["ok"] = bool(out["pride"].get("ok"))
        elif kind == "GEO":
            out["geo"] = check_geo(value); out["ok"] = bool(out["geo"].get("ok"))
        elif kind == "SRA":
            out["sra"] = check_sra(value); out["ok"] = bool(out["sra"].get("ok"))
        elif kind == "Figshare":
            out["figshare"] = check_figshare(value); out["ok"] = bool(out["figshare"].get("ok"))
        else:
            out["ok"] = False
    except Exception as e:
        out["ok"] = False; out["err"] = str(e)
    return out


def verdict(validations: list[dict]) -> dict:
    n_sigs = len(validations)
    n_ok = sum(1 for v in validations if v.get("ok"))
    strong = []
    for v in validations:
        if v.get("ok") and v["kind"] in ("Zenodo", "PRIDE", "GEO", "SRA", "Figshare"):
            strong.append(v["kind"])
        elif v.get("ok") and v["kind"] == "DOI":
            dc = v.get("datacite") or {}
            if dc.get("ok") and dc.get("type_general") in ("Dataset", "Software", "Collection"):
                strong.append(f"DataCite:{dc.get('type_general')}")
    if strong:
        status = "FOUND_DEPOSIT"
    elif n_ok > 0:
        status = "FOUND_DOI_ONLY"
    elif n_sigs > 0:
        status = "BROKEN_SIGNALS"
    else:
        status = "NO_SIGNALS"
    return {"status": status, "n_signals": n_sigs, "n_ok": n_ok,
            "n_strong": len(strong), "strong_types": strong}


def process_card(path: Path) -> dict:
    card = parse_card(path)
    sigs = signals_for_validation(card)
    validations = []
    if sigs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(validate_one, k, v) for k, v in sigs]
            for f in as_completed(futs):
                validations.append(f.result())
    card["validations"] = validations
    card["verdict"] = verdict(validations)
    return card


def main():
    if len(sys.argv) < 3:
        print("usage: cards_findability_pipeline.py <input_dir> <output_jsonl> [n_workers=4]",
              file=sys.stderr)
        sys.exit(1)
    in_dir = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    n_workers = int(sys.argv[3]) if len(sys.argv) > 3 else 4

    files = sorted(in_dir.glob("*.txt"))
    print(f"processing {len(files)} cards with {n_workers}× card-parallelism "
          f"× 8× signal-parallelism", file=sys.stderr)
    t0 = time.time()
    done = 0
    with out_path.open("w") as out_f:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(process_card, f): f for f in files}
            for f in as_completed(futs):
                try:
                    rec = f.result()
                except Exception as e:
                    rec = {"file": futs[f].name, "err": str(e)}
                out_f.write(json.dumps(rec) + "\n")
                out_f.flush()
                done += 1
                if done % 10 == 0:
                    dt = time.time() - t0
                    rate = done / dt
                    eta = (len(files) - done) / rate
                    print(f"  {done}/{len(files)}  {rate:.2f}/s  eta={eta:.0f}s",
                          file=sys.stderr)
    dt = time.time() - t0
    print(f"done in {dt:.1f}s ({len(files)/dt:.2f} cards/s)", file=sys.stderr)


if __name__ == "__main__":
    main()
