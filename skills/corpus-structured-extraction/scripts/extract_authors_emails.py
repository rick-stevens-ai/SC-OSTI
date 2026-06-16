#!/usr/bin/env python3
"""Extract structured author info + emails from a directory of paper fulltext files.

Use case: after a paper-corpus PDF→text pipeline (e.g. OSTI fetch + pymupdf
extraction), produce per-paper JSONL with author list, affiliations, and
corresponding-author email. Downstream uses:
  - artifact-gap follow-up (write to corresponding authors of cards with missing data/code)
  - replication-project contact list
  - author→Genesis-Mission-challenge-area portfolio mapping

Engine: Argo proxy Claude Haiku 4.5 (free, ~22 papers/s with 12 workers).

Configure FULLTEXT_DIR and OUT_PATH for your corpus. Resumable on osti_id.

PREREQ — run BEFORE first launch:
  curl -sS http://<tailnet-aggregator>:44497/v1/models -H "Authorization: Bearer stevens" | \
    python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
  -> confirm 'argo:claude-haiku-4.5' is in the list. If not, pick another
     non-reasoning Argo model (NOT gpt-o1/o3/o4 — they burn the reasoning
     budget and abstain on classification tasks).
"""
import json, re, os, sys, time, urllib.request, urllib.error
import concurrent.futures as cf
from pathlib import Path
import ssl

# === CONFIGURE ===
ARGO = "http://<tailnet-aggregator>:44497/v1/chat/completions"
MODEL = "argo:claude-haiku-4.5"
KEY = "stevens"

FULLTEXT_DIR = Path.home() / "code/osti-replication-candidates/fulltext"
OUT_PATH = Path.home() / "code/osti-replication-candidates/emails.jsonl"
DONE_PATH = Path.home() / "code/osti-replication-candidates/emails.done"
# =================

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
OBFUSC_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[\(\{]?\s*(?:at|AT)\s*[\]\)\}]?\s*"
    r"([A-Za-z0-9.\-]+)\s*[\[\(\{]?\s*(?:dot|DOT)\s*[\]\)\}]?\s*([A-Za-z]{2,})"
)

PROMPT = """Extract author information from this paper excerpt. Return ONLY valid JSON, no prose.

Schema:
{"authors": [{"name": "Full Name", "email": "name@inst.edu", "affiliation": "Lab/University", "is_corresponding": true/false}],
 "corresponding_email": "primary@inst.edu or null if not marked"}

Rules:
- Corresponding author is usually marked with * or † or text like "corresponding author"
- If email is obfuscated (name AT inst DOT edu), convert to standard form
- If no email is shown for an author, set email to null
- Return null for fields you cannot determine; do not invent
- If no authors detectable, return {"authors": [], "corresponding_email": null}

Excerpt:
"""

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def llm_extract(text, osti_id):
    """Call Argo Haiku for structured author info. Returns dict or error-dict."""
    excerpt = text[:4000]
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT + excerpt}],
        "max_tokens": 4096,  # MUST be >= 4096 — papers with 10+ authors truncate at 1024
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        ARGO,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"].strip()
        # Strip fenced code block if present
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```\s*$", "", content)
        # Find first {...} balanced block, greedy-shrink until parse succeeds
        s = content.find("{")
        if s < 0:
            return {"_error": "no_json_block"}
        for end in range(len(content), s, -1):
            try:
                return json.loads(content[s:end])
            except json.JSONDecodeError:
                continue
        return {"_error": "json_parse_failed"}
    except Exception as e:  # noqa: BLE001 — broad catch needed for thread pool
        return {"_error": type(e).__name__ + ": " + str(e)[:120]}


def regex_emails(text):
    """Fast belt-and-suspenders email regex over first 4KB (author block region)."""
    head = text[:4000]
    emails = set(EMAIL_RE.findall(head))
    for m in OBFUSC_RE.finditer(head):
        emails.add(f"{m.group(1)}@{m.group(2)}.{m.group(3)}")
    return sorted(emails)


def process_one(osti_id):
    fpath = FULLTEXT_DIR / f"{osti_id}.txt"
    try:
        text = fpath.read_text(errors="ignore")
    except Exception as e:  # noqa: BLE001
        return {"osti_id": osti_id, "error": f"read: {e}"}
    if not text.strip():
        return {"osti_id": osti_id, "error": "empty"}
    fallback = regex_emails(text)
    llm = llm_extract(text, osti_id)
    rec = {"osti_id": osti_id, "regex_emails": fallback}
    if llm and not llm.get("_error"):
        rec["authors"] = llm.get("authors") or []
        rec["corresponding_email"] = llm.get("corresponding_email")
        rec["extraction"] = "llm"
    else:
        rec["authors"] = []
        rec["corresponding_email"] = None
        rec["extraction"] = "regex_only"
        if llm and llm.get("_error"):
            rec["llm_error"] = llm["_error"]
    return rec


def main():
    all_ids = [f.stem for f in FULLTEXT_DIR.iterdir() if f.suffix == ".txt"]
    print(f"total fulltext files: {len(all_ids):,}", flush=True)

    done = set()
    if OUT_PATH.exists():
        with open(OUT_PATH) as f:
            for line in f:
                try:
                    done.add(json.loads(line)["osti_id"])
                except Exception:  # noqa: BLE001
                    pass
    todo = [i for i in all_ids if i not in done]
    print(f"already done: {len(done):,}   todo: {len(todo):,}", flush=True)

    workers = int(os.environ.get("EMAIL_WORKERS", "12"))
    t0 = time.time()
    n = 0
    with open(OUT_PATH, "a") as fh, cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(process_one, oid): oid for oid in todo}
        for fut in cf.as_completed(futures):
            rec = fut.result()
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            n += 1
            if n % 100 == 0:
                rate = n / (time.time() - t0)
                eta = (len(todo) - n) / max(rate, 0.01) / 60
                print(f"  done={n:>6,}/{len(todo):,}  rate={rate:.1f}/s  eta={eta:.0f}min", flush=True)

    DONE_PATH.write_text("done\n")
    print(f"COMPLETE: {n:,} processed in {(time.time()-t0)/60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
