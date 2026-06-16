#!/usr/bin/env python3
"""
Corpus augmentation template — two-pass cheap-source + LLM gap-fill, with
idempotent marker-delimited injection into existing Markdown documents.

Generalized from the 2026-06-08 xCards contact augmentation. Edit the
sections marked CUSTOMIZE for your specific field/corpus.

Usage:
    python3 augment_corpus_with_markers.py --pass 1            # SQLite-driven
    python3 augment_corpus_with_markers.py --pass 2 --workers 8  # LLM fill-gap
    python3 augment_corpus_with_markers.py --pass 1 --limit 20 --sample-mode mixed  # smoke
"""
from __future__ import annotations
import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CUSTOMIZE these paths/markers for your corpus
# ============================================================
ROOT = Path.home() / "Dropbox/ARGONNE-PAPERS/XCARDS"
CORPUS_DIRS = {
    "DATA":  ROOT / "MARKDOWN-DATA-CARDS",
    "MODEL": ROOT / "MARKDOWN-MODEL-CARDS",
    "AGENT": ROOT / "MARKDOWN-AGENT-CARDS",
}
DB_PATH = Path.home() / "Dropbox/XFER/osti-contacts/contacts.db"

START_MARK = "<!-- AUGMENT:CONTACTS START -->"
END_MARK   = "<!-- AUGMENT:CONTACTS END -->"
BLOCK_RE = re.compile(re.escape(START_MARK) + r".*?" + re.escape(END_MARK), re.DOTALL)

# Filename → primary key extractor. Match the FIRST 6-8 digit run.
ID_RE = re.compile(r'(\d{6,8})')

# LLM endpoint (CELS llama70 free; or swap for Argo)
LLM_URL = "http://<cels-chicago-2>:80/v1/chat/completions"
LLM_MODEL = "llama70"
LLM_KEY = "CELS"

EMPTY_SENTINEL_RE = re.compile(r"_No .* available|_No .* extractable")

# ============================================================
# CUSTOMIZE the SQLite query for your cheap source
# ============================================================
def fetch_from_cheap_source(con: sqlite3.Connection, pk: str) -> list[dict]:
    """Pass 1: pull augmentation data for one primary key from SQLite."""
    rows = con.execute(
        """
        SELECT cp.email, cp.is_corresponding,
               c.primary_name, c.primary_lab, c.paper_count
          FROM contact_paper cp
          LEFT JOIN contact c ON c.email = cp.email
         WHERE cp.osti_id = ?
        """,
        (pk,),
    ).fetchall()
    out = []
    for email, is_corr, name, lab, pcount in rows:
        out.append({
            "email": email or "",
            "is_corresponding": bool(is_corr),
            "name": name or "",
            "lab": lab or "",
            "paper_count": pcount or 0,
        })
    # Corresponding first, then by paper-count desc, then name
    out.sort(key=lambda r: (not r["is_corresponding"], -r["paper_count"], r["name"].lower()))
    return out


def fetch_paper_meta(con: sqlite3.Connection, pk: str) -> dict | None:
    r = con.execute(
        "SELECT title, year, doi, journal FROM paper WHERE osti_id = ?",
        (pk,),
    ).fetchone()
    if not r:
        return None
    return {"title": r[0], "year": r[1], "doi": r[2], "journal": r[3]}


# ============================================================
# CUSTOMIZE the LLM prompt + JSON shape for your field
# ============================================================
PROMPT = """You are extracting the author/contact block from the head of a scientific paper.

Return a JSON array (NOT wrapped in markdown fences) of contact entries. Each entry has:
  - name: full author name as written (string)
  - email: email address if present, else empty string
  - is_corresponding: true if marked as corresponding/contact author, else false
  - affiliation: primary institution as a short string; empty string if unknown

Rules:
- Only include people; skip institutional addresses and editors.
- If no emails appear anywhere in the text, still list authors with empty email fields.
- Output ONLY the JSON array. No prose, no explanation, no code fences.
- If you find nothing, output [].

Paper head:
---
{text}
---
"""


def call_llm(text_head: str, timeout: float = 45.0) -> list[dict]:
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": PROMPT.format(text=text_head)}],
        "temperature": 0.0,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {LLM_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"].strip()
    # Strip code fences if model added them
    if content.startswith("```"):
        content = re.sub(r'^```(?:json)?\s*', '', content)
        content = re.sub(r'\s*```$', '', content)
    # Greedy brace-match: first `[` to last `]`
    s, e = content.find('['), content.rfind(']')
    if s == -1 or e == -1 or e < s:
        return []
    try:
        parsed = json.loads(content[s:e+1])
        return [p for p in parsed if isinstance(p, dict)]
    except json.JSONDecodeError:
        return []


# ============================================================
# CUSTOMIZE the rendered block for your field shape
# ============================================================
def render_block(pk: str, paper_meta: dict | None, entries: list[dict],
                 source: str) -> str:
    lines = [START_MARK, "", "## Contacts (augmented)", ""]
    meta = [f"osti_id: `{pk}`"]
    if paper_meta:
        if paper_meta.get("year"): meta.append(f"year: {paper_meta['year']}")
        if paper_meta.get("doi"):  meta.append(f"doi: `{paper_meta['doi']}`")
        if paper_meta.get("journal"): meta.append(f"journal: {paper_meta['journal']}")
    meta.append(f"source: {source}")
    meta.append(f"as_of: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    lines.append("_" + " · ".join(meta) + "_")
    lines.append("")
    if not entries:
        lines.append("_No contacts available in source for this id._")
    else:
        # Display columns differ by source — pick consistent header
        if "lab" in entries[0]:
            lines.append("| Role | Name | Email | Lab | Papers |")
            lines.append("|------|------|-------|-----|--------|")
            for c in entries:
                role = "**corresponding**" if c.get("is_corresponding") else "author"
                name = (c.get("name") or "_(unknown)_").replace("|", "\\|")
                email = f"`{c['email']}`" if c.get("email") else "—"
                lab = (c.get("lab") or "—").replace("|", "\\|")
                pc = c.get("paper_count", 0) or 0
                lines.append(f"| {role} | {name} | {email} | {lab} | {pc} |")
        else:
            lines.append("| Role | Name | Email | Affiliation |")
            lines.append("|------|------|-------|-------------|")
            for c in entries:
                role = "**corresponding**" if c.get("is_corresponding") else "author"
                name = (c.get("name") or "_(unknown)_").replace("|", "\\|")
                email = f"`{c['email']}`" if c.get("email") else "—"
                aff = (c.get("affiliation") or "—").replace("|", "\\|")
                lines.append(f"| {role} | {name} | {email} | {aff} |")
    lines.append("")
    lines.append(END_MARK)
    return "\n".join(lines)


# ============================================================
# Core upsert primitives (rarely need customization)
# ============================================================
def upsert_block(text: str, block: str) -> str:
    if BLOCK_RE.search(text):
        return BLOCK_RE.sub(block, text, count=1)
    sep = "" if text.endswith("\n") else "\n"
    return text + sep + "\n" + block + "\n"


def pk_from_name(name: str) -> str | None:
    m = ID_RE.search(name)
    return m.group(1) if m else None


def has_empty_block(text: str) -> bool:
    """True if file has an AUGMENT block whose body is empty-sentinel."""
    m = BLOCK_RE.search(text)
    if not m:
        return True  # no block at all
    return bool(EMPTY_SENTINEL_RE.search(m.group(0)))


# ============================================================
# Pass 1 — SQLite-driven canonical injection
# ============================================================
def process_pass1(card: Path, con: sqlite3.Connection, backup: bool) -> dict:
    name = card.name
    pk = pk_from_name(name)
    if not pk:
        return {"file": str(card), "pk": "", "n": 0, "status": "no_id"}
    entries = fetch_from_cheap_source(con, pk)
    paper_meta = fetch_paper_meta(con, pk)
    block = render_block(pk, paper_meta, entries, source="cheap_source_db")
    body = card.read_text(errors="ignore")
    new = upsert_block(body, block)
    if new == body:
        return {"file": str(card), "pk": pk, "n": len(entries), "status": "idempotent"}
    status = "updated" if BLOCK_RE.search(body) else "inserted"
    if backup and not card.with_suffix(card.suffix + ".bak").exists():
        shutil.copy2(card, card.with_suffix(card.suffix + ".bak"))
    card.write_text(new)
    return {"file": str(card), "pk": pk, "n": len(entries), "status": status}


# ============================================================
# Pass 2 — LLM fill-gap on misses
# ============================================================
def build_text_index(text_roots: list[Path]) -> dict[str, Path]:
    idx: dict[str, Path] = {}
    for r in text_roots:
        if not r.exists(): continue
        for f in r.glob("*.txt"):
            m = ID_RE.search(f.name)
            if not m: continue
            pk = m.group(1)
            existing = idx.get(pk)
            if not existing or f.stat().st_size > existing.stat().st_size:
                idx[pk] = f
    return idx


def process_pass2(card: Path, text_path: Path, paper_meta: dict | None,
                  head_chars: int = 8000) -> dict:
    pk = pk_from_name(card.name)
    if not pk:
        return {"file": str(card), "pk": "", "n": 0, "status": "no_id"}
    try:
        head = text_path.read_text(errors="ignore")[:head_chars]
    except Exception as e:  # noqa: BLE001
        return {"file": str(card), "pk": pk, "n": 0, "status": f"read_err:{type(e).__name__}"}
    try:
        entries = call_llm(head)
    except Exception as e:  # noqa: BLE001
        return {"file": str(card), "pk": pk, "n": 0, "status": f"llm_err:{type(e).__name__}"}
    block = render_block(pk, paper_meta, entries, source=f"{LLM_MODEL}+paper_head")
    body = card.read_text(errors="ignore")
    new = upsert_block(body, block)
    if new != body:
        card.write_text(new)
        return {"file": str(card), "pk": pk, "n": len(entries),
                "status": "filled" if entries else "empty_response"}
    return {"file": str(card), "pk": pk, "n": len(entries), "status": "no_change"}


# ============================================================
# Driver
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="phase", type=int, choices=[1, 2], required=True)
    ap.add_argument("--kinds", nargs="+", default=list(CORPUS_DIRS.keys()),
                    choices=list(CORPUS_DIRS.keys()))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sample-mode", choices=["head", "mixed", "missing"], default="head")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--text-roots", nargs="+",
                    default=[str(Path.home() / "Dropbox/ARGONNE-PAPERS/GOOD/ALL-PAPERS-DATA-CARDS")])
    ap.add_argument("--manifest", default=str(Path.cwd() / "augment_manifest.csv"))
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: cheap-source DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(2)

    EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
    targets = []
    for kind in args.kinds:
        base = CORPUS_DIRS[kind]
        files = sorted(base.glob("*.md"))
        if args.limit:
            if args.sample_mode == "head":
                files = files[: args.limit]
            elif args.sample_mode == "missing":
                files = [f for f in files if not EMAIL.search(f.read_text(errors="ignore"))][: args.limit]
            elif args.sample_mode == "mixed":
                with_e = [f for f in files if EMAIL.search(f.read_text(errors="ignore"))]
                without_e = [f for f in files if not EMAIL.search(f.read_text(errors="ignore"))]
                half = args.limit // 2
                files = with_e[:half] + without_e[: args.limit - half]
        targets.extend(files)
    print(f"Pass {args.phase}: {len(targets)} candidate files", file=sys.stderr)

    results = []
    t0 = time.time()

    if args.phase == 1:
        con = sqlite3.connect(DB_PATH)
        for f in targets:
            try:
                results.append(process_pass1(f, con, not args.no_backup))
            except Exception as e:  # noqa: BLE001
                results.append({"file": str(f), "pk": "", "n": 0,
                                "status": f"error:{type(e).__name__}"})
        con.close()
    else:
        text_index = build_text_index([Path(p) for p in args.text_roots])
        print(f"  text index: {len(text_index):,} ids", file=sys.stderr)
        con = sqlite3.connect(DB_PATH)
        pass2_targets = []
        for f in targets:
            pk = pk_from_name(f.name)
            if not pk: continue
            body = f.read_text(errors="ignore")
            if not has_empty_block(body): continue
            if pk not in text_index: continue
            pass2_targets.append((f, text_index[pk], fetch_paper_meta(con, pk)))
        con.close()
        print(f"  pass-2 work queue: {len(pass2_targets)} files", file=sys.stderr)
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process_pass2, c, t, pm): c for c, t, pm in pass2_targets}
            for fut in as_completed(futs):
                results.append(fut.result())
                done += 1
                if done % 25 == 0:
                    rate = done / (time.time() - t0)
                    eta = (len(pass2_targets) - done) / max(rate, 1e-6)
                    print(f"  {done}/{len(pass2_targets)} ({rate:.1f}/s, ETA {eta:.0f}s)",
                          file=sys.stderr)

    # Manifest + summary
    with open(args.manifest, "w", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["file", "pk", "n", "status"])
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})

    from collections import Counter
    by_status = Counter(r["status"] for r in results)
    with_data = sum(1 for r in results if r["n"] > 0)
    elapsed = time.time() - t0
    print(f"\n=== pass {args.phase} summary ===")
    print(f"files processed:   {len(results)}")
    print(f"files with data:   {with_data} ({100*with_data/max(len(results),1):.1f}%)")
    print(f"status breakdown:  {dict(by_status)}")
    print(f"elapsed:           {elapsed:.1f}s")
    print(f"manifest:          {args.manifest}")


if __name__ == "__main__":
    main()
