#!/usr/bin/env python3
"""Parallel LLM structured-field extraction from a corpus of documents.

Template — edit SYSTEM and USER_TMPL for your specific extraction task, then:

    python3 llm_extract.py targets.json > results.json 2> progress.log

where targets.json is a list of {"id": "...", "path": "/abs/path/to/doc.md"}.

Returns a JSON dict keyed by id, each value either an error dict or the
parsed JSON the LLM produced.

Hardcoded to Argo proxy on cherryrd (Rick's setup). Change ARGO/MODEL/HEADERS
for other endpoints.
"""
import json, os, re, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# === EDIT THESE FOR YOUR USE CASE =========================================
ARGO = "http://<tailnet-aggregator>:44497/v1/chat/completions"
MODEL = "argo:claude-sonnet-4.6"  # workhorse; haiku-4.5 for cheaper, opus-4.7 for harder judgment
HEADERS = {"Authorization": "Bearer stevens", "Content-Type": "application/json"}
ROOT = os.path.expanduser("~/Dropbox")  # base dir for resolving relative paths in targets

SYSTEM = """You are an expert reviewer extracting structured fields from a document.

You will be given a single document. Extract the following fields:

  field_a — short description
  field_b — short description
  ...

Output ONLY a single JSON line. Example:
{"field_a": 7, "field_b": "value", "note": "1-sentence justification"}
"""

USER_TMPL = """Extract fields from this document. Reply with one JSON line only — no markdown.

DOC ID: {rid}

---
{body}
---
"""
# ==========================================================================

MAX_BODY_CHARS = 18000  # Sonnet handles 200K but cost scales linearly; head+tail truncate

def strip_latex(s):
    """Light LaTeX → plain conversion for .tex inputs."""
    s = re.sub(r'\\(begin|end)\{[^}]+\}', '', s)
    s = re.sub(r'\\href\{[^}]*\}\{([^}]*)\}', r'\1', s)
    s = re.sub(r'\\(textbf|emph|texttt)\{([^}]*)\}', r'\2', s)
    s = re.sub(r'\\[a-zA-Z]+\*?\s*(\[[^\]]*\])?', '', s)
    return s

def call(rid, body):
    """Single LLM call with 3-strategy JSON parsing fallback."""
    if len(body) > MAX_BODY_CHARS:
        # Head + tail truncation preserves the introduction and conclusion
        half = MAX_BODY_CHARS // 2
        body = body[:half] + "\n... [TRUNCATED — middle removed] ...\n" + body[-half:]
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER_TMPL.format(rid=rid, body=body)},
        ],
        "max_tokens": 400,
        "temperature": 0.1,
    }
    req = urllib.request.Request(ARGO, data=json.dumps(payload).encode(), headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        content = data["choices"][0]["message"]["content"].strip()
        return parse_json_robust(content)
    except Exception as e:
        return {"error": str(e)[:200]}

def parse_json_robust(content):
    """Three-strategy JSON parser. Recovers from notes containing nested {}."""
    # Strategy 1: strict no-nested-brace match (cleanest case)
    m = re.search(r'\{[^{}]*"\w+"[^{}]*\}', content)
    if m:
        try: return json.loads(m.group(0))
        except Exception: pass
    # Strategy 2: greedy brace-counting from the first { that contains a quoted key
    idx = re.search(r'"\w+"\s*:', content)
    if idx:
        start = content.rfind('{', 0, idx.start())
        if start >= 0:
            depth = 0
            for i in range(start, len(content)):
                if content[i] == '{': depth += 1
                elif content[i] == '}':
                    depth -= 1
                    if depth == 0:
                        try: return json.loads(content[start:i+1])
                        except Exception: pass
                        break
    # Strategy 3: regex-extract individual fields (last resort, no nesting recovered)
    out = {}
    for m in re.finditer(r'"(\w+)"\s*:\s*(\d+|"[^"]*"|true|false|null)', content):
        k, v = m.group(1), m.group(2)
        try: out[k] = json.loads(v)
        except Exception: out[k] = v.strip('"')
    if out: return out
    return {"error": "no_json", "raw": content[:500]}

def main():
    if len(sys.argv) != 2:
        print("usage: llm_extract.py targets.json", file=sys.stderr)
        sys.exit(2)
    targets = json.load(open(sys.argv[1]))
    print(f"Extracting from {len(targets)} docs with {MODEL}...", file=sys.stderr)
    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for t in targets:
            full = t['path'] if t['path'].startswith('/') else os.path.join(ROOT, t['path'])
            try:
                text = open(full, errors='replace').read()
            except Exception as e:
                results[t['id']] = {"error": f"read_fail: {e}"}
                continue
            body = strip_latex(text) if full.endswith('.tex') else text
            futures[pool.submit(call, t['id'], body)] = t['id']
        done_n = 0
        for fut in as_completed(futures):
            rid = futures[fut]
            results[rid] = fut.result()
            done_n += 1
            r = results[rid]
            tag = 'OK' if 'error' not in r else 'ERR'
            print(f"  [{done_n:3d}/{len(futures)}] {tag} {rid}", file=sys.stderr)
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()
