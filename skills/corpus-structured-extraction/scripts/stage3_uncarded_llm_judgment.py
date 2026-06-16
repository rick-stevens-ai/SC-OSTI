#!/usr/bin/env python3
"""
Stage-3 LLM judgment template for the "un-carded selection-pool members"
fill pattern. See pipeline-coverage-gap-as-selection-bottleneck-2026-06-15.md
"Stage-3 worked recipe" section for the full design.

When card-based findability covers <10% of the selection pool, use this
pattern to fill the gap with metadata-only LLM judgment in parallel.

Customize:
  - SYS prompt (the credibility rubric for your task)
  - build_user(rec) (the per-record metadata you want the LLM to see)
  - Argo endpoint / model (or any OpenAI-compatible chat endpoint)
  - Card-result file globs in main() (the exclusion list)

Verified shape (OSTI 2026-06-15):
  - 23,662 un-carded REPLICABLE_NO_LAB papers
  - Argo Sonnet 4.6, 16 workers → 3.5 req/s sustained, 0 errors
  - ~110 min wall, $0 cost on ALCF proxy
  - 12% HIGH / 60% MEDIUM / 28% LOW verdict distribution
"""
import json, os, sys, time, argparse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ────────────────────────────────────────────────────────────────────────────
# CUSTOMIZE: endpoint and model
# ────────────────────────────────────────────────────────────────────────────
ARGO_URL  = "http://<tailnet-aggregator>:44497/v1/chat/completions"
ARGO_AUTH = "Bearer stevens"
ARGO_MODEL = "argo:claude-sonnet-4.6"

# ────────────────────────────────────────────────────────────────────────────
# CUSTOMIZE: rubric — the prompt that governs the judgment
# ────────────────────────────────────────────────────────────────────────────
SYS = """You are scoring a scientific paper for how CREDIBLY REPLICABLE it is
from public materials, given only its bibliographic metadata.

A previous reviewer flagged this paper as appearing reproducible without
specialized lab equipment. Your job is a stricter second pass: given
title + subjects + the prior reviewer's reasoning, estimate how likely it is
that a competent third party could actually reproduce the paper's main
quantitative results using ONLY public code/data/methods.

Output STRICT JSON:
{
  "stage3_verdict": "HIGH" | "MEDIUM" | "LOW",
  "stage3_score": <0.0-1.0>,
  "stage3_why": "<one sentence>",
  "needs_code": <true|false>,
  "needs_data": <true|false>,
  "domain_tag": "<short tag e.g. theory/simulation/ml/data-analysis/numerical>"
}

Calibration:
- HIGH (0.7-1.0): widely-available methods/code (standard ML benchmark,
  well-known simulation tool with public input files, theory paper whose
  calculations any competent reader could redo)
- MEDIUM (0.4-0.7): plausibly replicable but key dependencies likely closed
  (custom code not released, proprietary input data, one-off setup)
- LOW (0.0-0.4): despite prior label, reproduction would require non-public
  artifacts (collaboration-internal data/code, custom pipeline, fit to
  private data)

Be honest. Theory/numerical-derivation papers will be HIGH; applied
experimental papers with one-off codes will be LOW even if not requiring
laboratory work."""

def build_user(rec):
    """CUSTOMIZE: what per-record context to show the LLM."""
    subj = ", ".join(rec.get("subjects_preview", [])[:5])
    why = rec.get("why", "")
    return f"""Title: {rec['title']}
Year: {rec['year']}
Subjects: {subj}
Prior reviewer reasoning: {why}

Score this paper. Output STRICT JSON only, no preamble."""

# ────────────────────────────────────────────────────────────────────────────
# Core call (you usually don't need to touch this)
# ────────────────────────────────────────────────────────────────────────────
def call_argo(rec, timeout=60):
    body = {
        "model": ARGO_MODEL,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": build_user(rec)},
        ],
        "temperature": 0.0,
        "max_tokens": 400,
    }
    req = urllib.request.Request(
        ARGO_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": ARGO_AUTH},
    )
    t0 = time.time()
    content = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
        elapsed = time.time() - t0
        content = j["choices"][0]["message"]["content"].strip()
        # Strip fences if model adds them despite STRICT JSON instruction
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(l for l in lines if not l.startswith("```"))
        verdict = json.loads(content)
        return {"osti_id": rec["osti_id"], "year": rec.get("year"),
                "title": rec["title"][:200],
                "stage3": verdict, "elapsed_s": elapsed}
    except json.JSONDecodeError:
        return {"osti_id": rec["osti_id"], "error": "json_decode",
                "raw": (content or "")[:500], "elapsed_s": time.time()-t0}
    except Exception as e:  # noqa: BLE001
        # Broad-except mandatory inside ThreadPoolExecutor — narrow catches
        # let RemoteDisconnected / ConnectionResetError / ssl.SSLError escape
        # and kill threads silently. See corpus-structured-extraction skill
        # pitfall "Network fetch loops need broad except Exception".
        return {"osti_id": rec["osti_id"],
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "elapsed_s": time.time()-t0}

# ────────────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="JSONL of selection-pool records (must have osti_id + title + year + why)")
    ap.add_argument("--carded-results-dir", required=True,
                    help="Dir of card-findability result JSONLs (used for exclusion)")
    ap.add_argument("--carded-result-globs", nargs="+",
                    default=["data_*results.jsonl", "model_*results.jsonl", "agent_*results.jsonl"],
                    help="Globs (relative to carded-results-dir) of result files to exclude by card_id")
    ap.add_argument("--output", required=True, help="Append-only output JSONL")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N records (0 = no limit)")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--skip-existing", action="store_true",
                    help="Resume: skip osti_ids already in output file")
    args = ap.parse_args()

    # Build carded exclusion set
    carded = set()
    for pattern in args.carded_result_globs:
        for p in Path(args.carded_results_dir).glob(pattern):
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                        if r.get("card_id"):
                            carded.add(str(r["card_id"]))
                    except Exception:
                        continue
    print(f"carded count: {len(carded)}", file=sys.stderr)

    # Resume: skip already-completed
    done = set()
    if args.skip_existing and Path(args.output).exists():
        with open(args.output) as f:
            for line in f:
                try:
                    done.add(str(json.loads(line)["osti_id"]))
                except Exception:
                    continue
        print(f"resume: {len(done)} already done", file=sys.stderr)

    # Queue = pool − carded − done
    with open(args.input) as f:
        pool = [json.loads(l) for l in f]
    queue = [p for p in pool
             if str(p["osti_id"]) not in carded
             and str(p["osti_id"]) not in done]
    print(f"un-carded queue: {len(queue)}", file=sys.stderr)
    if args.limit:
        queue = queue[:args.limit]
    print(f"processing: {len(queue)}", file=sys.stderr)

    out_f = open(args.output, "a")
    n_done, n_err, t_start = 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(call_argo, rec): rec for rec in queue}
        for fut in as_completed(futs):
            res = fut.result()
            out_f.write(json.dumps(res) + "\n")
            out_f.flush()
            n_done += 1
            if "error" in res:
                n_err += 1
            if n_done % 50 == 0:
                rate = n_done / (time.time() - t_start)
                eta = (len(queue) - n_done) / rate if rate > 0 else 0
                print(f"  {n_done}/{len(queue)}  {rate:.2f}/s  err={n_err}  "
                      f"eta={eta:.0f}s", file=sys.stderr)
    out_f.close()
    print(f"done {n_done} in {time.time()-t_start:.1f}s (err={n_err})",
          file=sys.stderr)

if __name__ == "__main__":
    main()
