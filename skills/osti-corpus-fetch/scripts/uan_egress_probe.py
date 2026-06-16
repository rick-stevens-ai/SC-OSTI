#!/usr/bin/env python3.10
"""Aurora UAN egress probe for OSTI PURL recovery — polite, low-rate, bounded.

Reuses the stratified sample from a prior CELS probe so buckets are
directly comparable. Sequential, 8s between requests, hard per-stage
timeouts. NO PDF bodies retained — status/error metadata only.

Run on aurora-uan-*: /usr/bin/python3.10 -u uan_egress_probe.py
Run detached: setsid nohup /usr/bin/python3.10 -u uan_egress_probe.py > log 2>&1 < /dev/null & disown

Expects sample_50_for_cels_probe.tsv (or any TSV with osti_id as first column)
in the same directory.
"""
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
SAMPLE = HERE / "sample_50_for_cels_probe.tsv"
RUNDIR = HERE / f"probe_uan_{time.strftime('%Y%m%d-%H%M%S')}"

META_TIMEOUT = 10
LANDING_TIMEOUT = 10
PURL_TIMEOUT = 20
SLEEP_BETWEEN = 8  # polite — shared login node
MAX_BYTES = 50 * 1024 * 1024  # 50MB cap (raise to 100MB if oversize cluster appears)
UA = "Mozilla/5.0 (compatible; aurora-uan-egress-probe/1.0; rick.stevens.ai@gmail.com)"


def fetch(url, timeout, want_pdf=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.getcode()
            ctype = r.headers.get("Content-Type", "")
            final_url = r.geturl()
            if want_pdf:
                buf = b""
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        return {"http": code, "ctype": ctype, "size": len(buf),
                                "final_url": final_url, "bucket": "oversize"}
                magic = buf[:5]
                if magic == b"%PDF-":
                    return {"http": code, "ctype": ctype, "size": len(buf),
                            "final_url": final_url, "bucket": "pdf_ok"}
                head = buf[:200].decode("utf-8", errors="replace")
                is_html = b"<html" in buf[:500].lower() or b"<!doctype" in buf[:200].lower()
                return {"http": code, "ctype": ctype, "size": len(buf),
                        "final_url": final_url,
                        "bucket": "not_pdf_html" if is_html else "not_pdf_other",
                        "head": head}
            return {"http": code, "ctype": ctype, "final_url": final_url, "bucket": "ok"}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "bucket": f"http_{e.code}", "err": str(e.reason)[:120]}
    except urllib.error.URLError as e:
        msg = str(e.reason)[:200]
        if "timed out" in msg.lower():
            return {"http": 0, "bucket": "timeout", "err": msg}
        if "RemoteDisconnected" in msg or "Connection reset" in msg:
            return {"http": 0, "bucket": "reset", "err": msg}
        return {"http": 0, "bucket": "url_err", "err": msg}
    except (socket.timeout, TimeoutError) as e:
        return {"http": 0, "bucket": "timeout", "err": str(e)[:120]}
    except (ssl.SSLError, ConnectionResetError) as e:
        return {"http": 0, "bucket": "reset", "err": str(e)[:120]}
    except Exception as e:
        return {"http": 0, "bucket": f"exc:{type(e).__name__}", "err": str(e)[:120]}


def probe_one(osti_id):
    out = {"osti_id": osti_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    out["meta"] = fetch(f"https://www.osti.gov/api/v1/records?osti_id={osti_id}", META_TIMEOUT)
    out["landing"] = fetch(f"https://www.osti.gov/biblio/{osti_id}", LANDING_TIMEOUT)
    out["pdf"] = fetch(f"https://www.osti.gov/servlets/purl/{osti_id}", PURL_TIMEOUT, want_pdf=True)
    return out


def main():
    RUNDIR.mkdir(exist_ok=True)
    rows = []
    with open(SAMPLE) as f:
        next(f)  # header
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if parts and parts[0]:
                rows.append(parts[0])

    print(f"# UAN egress probe — {len(rows)} IDs, ~{len(rows) * SLEEP_BETWEEN}s polite floor",
          flush=True)
    print(f"# rundir: {RUNDIR}", flush=True)
    results = []
    for i, osti_id in enumerate(rows, 1):
        t0 = time.time()
        r = probe_one(osti_id)
        r["dt"] = round(time.time() - t0, 3)
        results.append(r)
        b = r["pdf"]["bucket"]
        sz = r["pdf"].get("size", 0)
        print(f"[{i:02d}/{len(rows)}] {osti_id:>10} pdf={b:<14} size={sz:>10} dt={r['dt']:.2f}s",
              flush=True)
        if i < len(rows):
            time.sleep(SLEEP_BETWEEN)

    (RUNDIR / "results.jsonl").write_text("\n".join(json.dumps(r) for r in results) + "\n")
    buckets = Counter(r["pdf"]["bucket"] for r in results)
    summary = [
        "# Aurora UAN egress probe summary",
        "",
        f"- host: {os.uname().nodename}",
        f"- ts: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"- N: {len(results)}",
        f"- rate: 1 req/{SLEEP_BETWEEN}s sequential",
        f"- cap: {MAX_BYTES // 1024 // 1024}MB",
        "",
        "## Buckets",
        "",
    ]
    for b, c in buckets.most_common():
        summary.append(f"- {b}: {c} ({100 * c // len(results)}%)")
    (RUNDIR / "SUMMARY.md").write_text("\n".join(summary) + "\n")
    print("\n" + "\n".join(summary[7:]))


if __name__ == "__main__":
    main()
