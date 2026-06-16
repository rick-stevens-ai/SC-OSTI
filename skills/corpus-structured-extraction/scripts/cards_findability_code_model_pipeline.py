#!/usr/bin/env python3
"""Findability pipeline for MODEL and AGENT cards (sibling of cards_findability_pipeline.py).

The data-card pipeline asks "can I find the dataset?" Strong signals are
deposit registries (Zenodo, GEO, PRIDE, SRA, Figshare, DataCite-typed Dataset).

This pipeline asks "can I find/download the code or weights and actually run this?"
Strong signals are CODE registries (GitHub, GitLab, HuggingFace Hub) plus arXiv
as a paper-findable fallback.

Usage:
    python3.13 cards_findability_code_model_pipeline.py <model|agent> <input_dir> <output_jsonl> [n_workers=6]

Worked baseline (OSTI cards, 2026-06-05):
    Model cards: 200 in 36.5s, 13.5% FOUND_RUNNABLE, 70% any signal, 17.5% no signals
    Agent cards: 86 in 8.6s, 18.6% FOUND_RUNNABLE, 48% any signal, 38% no signals

Pure stdlib. Python 3.10+ for `str | None`. On the M1 mac use /opt/homebrew/bin/python3.13.

Verdict states (note the DIFFERENT set from the data-card pipeline):
  FOUND_RUNNABLE    — ≥1 resolvable GitHub / GitLab / HuggingFace repo
  FOUND_PAPER_ONLY  — arXiv or DataCite Software/Dataset, but no live code repo
  FOUND_DOI_ONLY    — a DOI resolves but registry didn't classify as code/data
  WEAK_MATCH_ONLY   — only an HF-search name-match (likely false positive)
  BROKEN_SIGNALS    — has identifiers but none resolve
  NO_SIGNALS        — no machine-extractable identifiers

Two important design choices baked in (see references/card-resolution-pattern):

  1. HF NAME-SEARCH IS A FALLBACK, NOT A STRUCTURAL SIGNAL. When a card has no HF
     URL, we search HF Hub for the model_name. But generic multi-word science names
     ("Kernel Ridge Regression for Anderson Impurity Model Green Function
     Prediction") either return zero hits OR an unrelated false-positive
     ("F-DetectorModel" for "fDETECT"). The verdict math EXCLUDES HF-search from
     the n_signals count so a fallback miss does NOT mark the card BROKEN_SIGNALS.
     Search hits are flagged weak_match=True and bucketed WEAK_MATCH_ONLY.

  2. ARXIV COUNTS AS A STRONG SIGNAL for FOUND_PAPER_ONLY. An arXiv hit doesn't
     give you code, but it confirms the paper exists and is reachable — a real
     data point for findability separate from "DOI happens to resolve."
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


UA = "cards-findability-code/0.1"


# ---------- Common helpers ----------
def _get_json(url: str, timeout: int = 15, headers: dict | None = None) -> dict | None:
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    try:
        req = Request(url, headers=hdrs)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


PLACEHOLDER_RX = re.compile(
    r"\b(not\s+specified|unknown|null|n/?a|not\s+applicable|\[not[^\]]*\])\b", re.I
)


def is_placeholder(s: str) -> bool:
    if not s:
        return True
    s = s.strip().strip("\"'")
    if len(s) < 2:
        return True
    return bool(PLACEHOLDER_RX.fullmatch(s)) or bool(PLACEHOLDER_RX.match(s))


# ---------- Code/preprint validators ----------
def check_hf_model(repo_id: str) -> dict:
    m = re.search(r"huggingface\.co/([\w\-.]+/[\w\-.]+)", repo_id)
    if m:
        repo_id = m.group(1).rstrip("/")
    repo_id = repo_id.strip("/")
    if "/" not in repo_id:
        return {"registry": "HuggingFace", "ok": False, "err": "expected org/name"}
    data = _get_json(f"https://huggingface.co/api/models/{quote(repo_id)}")
    if not data:
        return {"registry": "HuggingFace", "ok": False, "repo_id": repo_id}
    return {
        "registry": "HuggingFace", "ok": True, "repo_id": repo_id,
        "model_id": data.get("modelId", ""),
        "pipeline_tag": data.get("pipeline_tag", ""),
        "downloads": data.get("downloads", 0),
        "likes": data.get("likes", 0),
        "gated": data.get("gated", False),
        "n_siblings": len(data.get("siblings", [])),
    }


def search_hf_by_name(model_name: str, limit: int = 3) -> dict:
    """Fallback when the card has no HF URL but does have a model_name.
    Returns top hits but caller should treat as weak_match, not ok."""
    q = quote(model_name)
    data = _get_json(f"https://huggingface.co/api/models?search={q}&limit={limit}")
    if not data or not isinstance(data, list) or not data:
        return {"registry": "HuggingFace-search", "ok": False, "query": model_name, "n_hits": 0}
    return {
        "registry": "HuggingFace-search", "ok": True, "query": model_name,
        "n_hits": len(data),
        "top_hits": [{"id": d.get("id"), "downloads": d.get("downloads", 0),
                      "likes": d.get("likes", 0)} for d in data[:3]],
    }


def check_github(url_or_slug: str) -> dict:
    m = re.search(r"github\.com[:/]([\w.\-]+/[\w.\-]+?)(?:[/.]|$)", url_or_slug)
    slug = m.group(1) if m else url_or_slug.strip("/").rstrip(".git")
    slug = slug.rstrip(".git")
    if "/" not in slug or slug.count("/") != 1:
        return {"registry": "GitHub", "ok": False, "err": "expected org/repo", "input": url_or_slug}
    data = _get_json(f"https://api.github.com/repos/{slug}")
    if not data:
        return {"registry": "GitHub", "ok": False, "slug": slug}
    if data.get("message"):
        return {"registry": "GitHub", "ok": False, "slug": slug, "api_msg": data.get("message", "")[:80]}
    return {
        "registry": "GitHub", "ok": True, "slug": data.get("full_name", slug),
        "description": (data.get("description") or "")[:120],
        "language": data.get("language", ""),
        "stars": data.get("stargazers_count", 0),
        "forks": data.get("forks_count", 0),
        "license": (data.get("license") or {}).get("spdx_id", "") if isinstance(data.get("license"), dict) else "",
        "pushed_at": data.get("pushed_at", ""),
        "archived": data.get("archived", False),
    }


def check_gitlab(url_or_slug: str) -> dict:
    m = re.search(r"gitlab\.com/([\w./\-]+?)(?:[/.]|$)", url_or_slug)
    if not m:
        return {"registry": "GitLab", "ok": False, "err": "no slug parsed"}
    slug = m.group(1).rstrip(".git")
    data = _get_json(f"https://gitlab.com/api/v4/projects/{quote(slug, safe='')}")
    if not data:
        return {"registry": "GitLab", "ok": False, "slug": slug}
    return {
        "registry": "GitLab", "ok": True, "slug": slug,
        "name": data.get("name", ""),
        "stars": data.get("star_count", 0),
        "visibility": data.get("visibility", ""),
    }


def check_arxiv(arxiv_id: str) -> dict:
    aid = re.sub(r"^arxiv:\s*", "", arxiv_id, flags=re.I).strip()
    aid = re.sub(r"v\d+$", "", aid)
    if not re.match(r"^(\d{4}\.\d{4,5}|[a-z-]+/\d{7})$", aid):
        return {"registry": "arXiv", "ok": False, "err": "bad arxiv id format", "input": arxiv_id}
    url = f"https://export.arxiv.org/api/query?id_list={aid}"
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="ignore")
    except Exception:
        return {"registry": "arXiv", "ok": False, "arxiv_id": aid}
    if "<entry>" not in body:
        return {"registry": "arXiv", "ok": False, "arxiv_id": aid}
    title_m = re.search(r"<title>([\s\S]*?)</title>", body[body.find("<entry>"):])
    return {
        "registry": "arXiv", "ok": True, "arxiv_id": aid,
        "title": (title_m.group(1).strip() if title_m else "")[:140],
        "url": f"https://arxiv.org/abs/{aid}",
    }


def check_datacite(doi: str) -> dict:
    data = _get_json(f"https://api.datacite.org/dois/{quote(doi, safe='/')}")
    if not data or "data" not in data:
        return {"registry": "DataCite", "ok": False, "doi": doi}
    attrs = data["data"].get("attributes", {}) or {}
    types = attrs.get("types", {}) or {}
    return {
        "registry": "DataCite", "ok": True, "doi": doi,
        "type_general": types.get("resourceTypeGeneral", ""),
        "publisher": attrs.get("publisher", ""),
    }


# ---------- DOI cleanup ----------
def doi_candidates(doi: str) -> list[str]:
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
    cands = doi_candidates(doi)
    for c in cands:
        url = f"https://doi.org/{c}"
        try:
            req = Request(url, method="HEAD", headers={"User-Agent": UA})
            with urlopen(req, timeout=timeout) as r:
                return {"raw": doi, "matched_candidate": c, "status": r.status, "ok": True}
        except HTTPError as e:
            if e.code in (301, 302, 303, 307, 308, 403):
                return {"raw": doi, "matched_candidate": c, "status": e.code, "ok": True}
            continue
        except Exception:
            continue
    return {"raw": doi, "matched_candidate": None, "ok": False}


# ---------- Extraction patterns ----------
DOI_RX = re.compile(r"\b(10\.\d{4,9}/[\w.\-/:()#]+?)(?=[\s,;\"\'\)\]]|$)", re.I)
URL_RX = re.compile(r'https?://[\w\-._~:/?#\[\]@!$&\'()*+,;=%]+', re.I)
ARXIV_RX = re.compile(r"(?:arxiv[:/\s]+|arxiv\.org/abs/)\s*(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})(?:v\d+)?", re.I)
GH_RX = re.compile(r"github\.com[:/]([\w.\-]+/[\w.\-]+?)(?=[/.\s\"')\]]|$)", re.I)
GL_RX = re.compile(r"gitlab\.com/([\w./\-]+?)(?=[/.\s\"')\]]|$)", re.I)
HF_RX = re.compile(r"huggingface\.co/([\w.\-]+/[\w.\-]+?)(?=[/.\s\"')\]]|$)", re.I)


def parse_card(path: Path, kind: str) -> dict:
    text = path.read_text(errors="ignore")
    out = {"file": path.name, "card_id": path.stem.split("_")[0], "card_kind": kind}

    yaml_keys = {
        "model": ["model_name", "model_type", "license", "base_model",
                  "code_repository", "model_repository", "framework",
                  "paper_doi", "arxiv_id"],
        "agent": ["agent_name", "agent_type", "license",
                  "code_repository", "documentation_url", "endpoint", "framework",
                  "paper_doi", "arxiv_id"],
    }[kind]
    for key in yaml_keys:
        m = re.search(rf'^\s*{re.escape(key)}\s*:\s*["\']?([^"\'\n]+)', text, re.M)
        if m:
            v = m.group(1).strip()
            if not is_placeholder(v):
                out[key] = v

    dois = set()
    for m in DOI_RX.finditer(text):
        d = m.group(1).rstrip(".,;:)\"']")
        if not is_placeholder(d):
            dois.add(d)
    out["dois"] = sorted(dois)

    ghs = set()
    for m in GH_RX.finditer(text):
        s = m.group(1).rstrip(".git").rstrip(".")
        if s.count("/") == 1 and not is_placeholder(s):
            ghs.add(s)
    out["github_repos"] = sorted(ghs)

    out["gitlab_repos"] = sorted({m.group(1).rstrip(".git") for m in GL_RX.finditer(text)})
    out["hf_repos"] = sorted({m.group(1) for m in HF_RX.finditer(text)})
    out["arxiv_ids"] = sorted({m.group(1) for m in ARXIV_RX.finditer(text)})
    out["urls"] = sorted({m.group(0).rstrip(".,;:)\"']") for m in URL_RX.finditer(text)})
    return out


def signals_for_validation(card: dict, kind: str) -> list[tuple[str, str]]:
    sigs = [("GitHub", r) for r in card.get("github_repos", [])]
    sigs += [("GitLab", r) for r in card.get("gitlab_repos", [])]
    sigs += [("HuggingFace", r) for r in card.get("hf_repos", [])]
    sigs += [("arXiv", a) for a in card.get("arxiv_ids", [])]
    sigs += [("DOI", d) for d in card.get("dois", [])]
    # HF name-search is a FALLBACK for model cards w/o an HF URL.
    if kind == "model" and not card.get("hf_repos"):
        name = card.get("model_name", "").strip()
        if name and not is_placeholder(name) and 4 <= len(name) <= 80:
            sigs.append(("HF-search", name))
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
        elif kind == "GitHub":
            out["github"] = check_github(value); out["ok"] = bool(out["github"].get("ok"))
        elif kind == "GitLab":
            out["gitlab"] = check_gitlab(value); out["ok"] = bool(out["gitlab"].get("ok"))
        elif kind == "HuggingFace":
            out["hf"] = check_hf_model(value); out["ok"] = bool(out["hf"].get("ok"))
        elif kind == "HF-search":
            out["hf_search"] = search_hf_by_name(value)
            out["ok"] = False  # search hits are NOT confirmed matches
            out["weak_match"] = bool(out["hf_search"].get("ok"))
        elif kind == "arXiv":
            out["arxiv"] = check_arxiv(value); out["ok"] = bool(out["arxiv"].get("ok"))
        else:
            out["ok"] = False
    except Exception as e:
        out["ok"] = False; out["err"] = str(e)
    return out


def verdict(validations: list[dict]) -> dict:
    # CRITICAL: exclude HF-search from signal counters so a fallback miss does
    # NOT mark an otherwise-no-signal card as BROKEN_SIGNALS. This bit me hard
    # in the model-card pass 2026-06-05 — 30% miscategorized as broken before
    # the fix, 12.5% after.
    real_signals = [v for v in validations if v["kind"] != "HF-search"]
    n_sigs = len(real_signals)
    n_ok = sum(1 for v in real_signals if v.get("ok"))
    n_weak = sum(1 for v in validations if v.get("weak_match"))

    strong = []
    for v in validations:
        if not v.get("ok"):
            continue
        if v["kind"] in ("GitHub", "GitLab", "HuggingFace"):
            strong.append(v["kind"])
        elif v["kind"] == "DOI":
            dc = v.get("datacite") or {}
            if dc.get("ok") and dc.get("type_general") in ("Software", "Dataset", "Model"):
                strong.append(f"DataCite:{dc.get('type_general')}")
        elif v["kind"] == "arXiv":
            strong.append("arXiv")

    if strong:
        status = "FOUND_RUNNABLE" if any(s in ("GitHub", "GitLab", "HuggingFace") for s in strong) else "FOUND_PAPER_ONLY"
    elif n_weak > 0:
        status = "WEAK_MATCH_ONLY"
    elif n_ok > 0:
        status = "FOUND_DOI_ONLY"
    elif n_sigs > 0:
        status = "BROKEN_SIGNALS"
    else:
        status = "NO_SIGNALS"

    return {"status": status, "n_signals": n_sigs, "n_ok": n_ok,
            "n_weak": n_weak, "n_strong": len(strong), "strong_types": strong}


def process_card(path: Path, kind: str) -> dict:
    card = parse_card(path, kind)
    sigs = signals_for_validation(card, kind)
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
    if len(sys.argv) < 4:
        print("usage: cards_findability_code_model_pipeline.py <model|agent> <input_dir> <output_jsonl> [n_workers=6]",
              file=sys.stderr)
        sys.exit(1)
    kind = sys.argv[1]
    if kind not in ("model", "agent"):
        print(f"bad kind: {kind!r}", file=sys.stderr); sys.exit(1)
    in_dir = Path(sys.argv[2])
    out_path = Path(sys.argv[3])
    n_workers = int(sys.argv[4]) if len(sys.argv) > 4 else 6

    files = sorted(in_dir.glob("*.txt"))
    print(f"[{kind}] processing {len(files)} cards (workers={n_workers})", file=sys.stderr)
    t0 = time.time()
    done = 0
    with out_path.open("w") as out_f:
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = {ex.submit(process_card, f, kind): f for f in files}
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
                    print(f"  {done}/{len(files)}  {done/dt:.2f}/s",
                          file=sys.stderr)
    dt = time.time() - t0
    print(f"[{kind}] done in {dt:.1f}s ({len(files)/dt:.2f} cards/s)", file=sys.stderr)


if __name__ == "__main__":
    main()
