#!/usr/bin/env python3
"""Multi-judge bake-off for picking the production LLM judge.

Run the same prompt across N candidate judges on the SAME smoke set, report
verdict distribution, pairwise agreement vs a chosen baseline, and per-judge
average latency. Use this before scaling to a 10k+ inference run.

Output JSONL (one line per judge × item) suitable for the analysis snippet
at the bottom of this docstring.

USAGE:
    python3 judge_bakeoff.py --input smoke.jsonl --output bakeoff.jsonl --limit 50

INPUT FORMAT:
    JSONL with at least {"id": "...", "title": "...", "abstract": "..."}
    Tweak the user_msg construction below to match your record shape.

CUSTOMIZE BEFORE RUNNING:
- JUDGES dict — add/remove endpoints, set max_tokens appropriately
- SYSTEM_PROMPT — your task-specific instructions
- parse_verdict() — your task-specific output format
- user_msg construction in call() — your record shape

ANALYSIS (run after bakeoff.jsonl is populated):

    import json, collections
    recs = [json.loads(l) for l in open('bakeoff.jsonl')]
    by_id_judge = {(r['id'], r['judge']): r for r in recs}
    judges = sorted(set(r['judge'] for r in recs))
    ids = sorted(set(r['id'] for r in recs))

    # Per-judge verdict distribution + avg latency
    for j in judges:
        c = collections.Counter()
        total_s = n = 0
        for oid in ids:
            r = by_id_judge.get((oid, j))
            if r: c[r['verdict']] += 1; total_s += r['elapsed_s']; n += 1
        print(j, dict(c), f"{total_s/n:.2f}s avg")

    # Pairwise agreement vs baseline (the first judge in JUDGES)
    base_name = judges[0]
    base = {oid: by_id_judge[(oid, base_name)]['verdict'] for oid in ids}
    for j in judges[1:]:
        agree = total = 0
        for oid in ids:
            b, o = base[oid], by_id_judge[(oid, j)]['verdict']
            if b in ('REPLICABLE_NO_LAB','NEEDS_LAB') and o in ('REPLICABLE_NO_LAB','NEEDS_LAB'):
                total += 1
                if b == o: agree += 1
        print(f"{j} vs {base_name}: {agree}/{total} = {100*agree/total if total else 0:.0f}%")
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen


JUDGES = {
    # Order matters: first judge is treated as the baseline in analysis.
    "argo-sonnet46": {
        "url": "http://<tailnet-aggregator>:44497/v1/chat/completions",
        "auth": "Bearer stevens",
        "model": "argo:claude-sonnet-4.6",
        "max_tokens": 200,
    },
    "cels-llama70": {
        "url": "http://<cels-chicago-2>:80/v1/chat/completions",
        "auth": "Bearer CELS",
        "model": "llama70",
        "max_tokens": 200,
    },
    "cels-gemma4": {
        "url": "http://<tailnet-host>:9999/v1/chat/completions",
        "auth": "Bearer CELS",
        "model": "gemma4",
        "max_tokens": 200,
    },
    # WARNING: reasoning models below tend to abstain (UNCLEAR) on classification
    # tasks. Include them in the bake-off ONLY to confirm the abstention pattern
    # — don't run them at scale. See SKILL.md "DO NOT use reasoning models for
    # short-answer classification".
    "cels-kimi": {
        "url": "http://<cels-chicago-1>:80/v1/chat/completions",
        "auth": "Bearer CELS",
        "model": "kimi-k2.6",
        "max_tokens": 1200,
    },
    "cels-oss120": {
        "url": "http://<cels-chicago-3>:80/v1/chat/completions",
        "auth": "Bearer CELS",
        "model": "oss120",
        "max_tokens": 1200,
    },
}


# CUSTOMIZE: your task-specific prompt
SYSTEM_PROMPT = """You are a classifier. Respond with EXACTLY this format on two lines:
VERDICT: <CLASS_A | CLASS_B | UNCLEAR>
WHY: <one short sentence>"""

VALID_VERDICTS = ("CLASS_A", "CLASS_B", "UNCLEAR")


def parse_verdict(content: str) -> tuple[str, str]:
    verdict = "UNCLEAR"
    why = ""
    for line in content.splitlines():
        line = line.strip()
        u = line.upper()
        if u.startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            for cand in VALID_VERDICTS:
                if cand in v:
                    verdict = cand
                    break
        elif u.startswith("WHY:"):
            why = line.split(":", 1)[1].strip()
    return verdict, why


def call(judge_name: str, judge_cfg: dict, rec: dict) -> dict:
    # CUSTOMIZE: your record shape
    user_msg = f"TITLE: {rec.get('title','')}\n\nABSTRACT: {rec.get('abstract','')[:3000]}"

    payload = {
        "model": judge_cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": judge_cfg["max_tokens"],
        "temperature": 0.0,
    }
    body = json.dumps(payload).encode()
    req = Request(
        judge_cfg["url"],
        data=body,
        headers={"Authorization": judge_cfg["auth"], "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    for attempt in range(3):
        try:
            with urlopen(req, timeout=90) as r:
                data = json.loads(r.read())
            content = data["choices"][0]["message"].get("content") or ""
            # Reasoning models may put answer in reasoning trace and leave content empty
            if not content:
                content = data["choices"][0]["message"].get("reasoning", "") or ""
            verdict, why = parse_verdict(content)
            return {
                "judge": judge_name,
                "id": rec["id"],
                "verdict": verdict,
                "why": why,
                "elapsed_s": round(time.time() - t0, 2),
                "raw_chars": len(content),
            }
        except Exception as e:  # noqa: BLE001 — broad catch intentional for network
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {
                "judge": judge_name,
                "id": rec["id"],
                "verdict": "ERROR",
                "why": f"{type(e).__name__}: {str(e)[:120]}",
                "elapsed_s": round(time.time() - t0, 2),
                "raw_chars": 0,
            }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="JSONL of records with id/title/abstract")
    ap.add_argument("--output", required=True)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--workers-per-judge", type=int, default=4)
    args = ap.parse_args()

    recs = []
    for line in open(args.input):
        r = json.loads(line)
        if not r.get("id"):
            # Try common alternate id fields
            for alt in ("osti_id", "doi", "arxiv_id"):
                if r.get(alt):
                    r["id"] = str(r[alt])
                    break
        if not r.get("id"):
            continue
        recs.append(r)
        if len(recs) >= args.limit:
            break

    print(f"running {len(JUDGES)} judges × {len(recs)} items = {len(JUDGES)*len(recs)} calls", file=sys.stderr)

    out_f = open(args.output, "w")
    for judge_name, judge_cfg in JUDGES.items():
        print(f"\n=== {judge_name} ({judge_cfg['model']}) ===", file=sys.stderr)
        t0 = time.time()
        n = 0
        with ThreadPoolExecutor(max_workers=args.workers_per_judge) as ex:
            futures = {ex.submit(call, judge_name, judge_cfg, rec): rec["id"] for rec in recs}
            for fut in as_completed(futures):
                r = fut.result()
                out_f.write(json.dumps(r) + "\n")
                out_f.flush()
                n += 1
                if n % 10 == 0:
                    print(f"  {n}/{len(recs)} @ {n/(time.time()-t0):.2f}/s", file=sys.stderr)
        dt = time.time() - t0
        print(f"  {judge_name} done: {n} in {dt:.1f}s ({n/dt:.2f}/s)", file=sys.stderr)
    out_f.close()
    print("\nSee module docstring for the analysis snippet to run on the output.", file=sys.stderr)


if __name__ == "__main__":
    main()
