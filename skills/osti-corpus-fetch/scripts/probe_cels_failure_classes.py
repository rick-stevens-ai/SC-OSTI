#!/usr/bin/env python3
"""Stage-by-stage diagnostic probe of OSTI PDF fetch — failure-class classifier.

Run this on a CELS host (cels-rbdgx2 or cels-rbdgx3) where the OSTI PURL
endpoint behaves well, NOT from home. The purpose is to DECIDE whether a
bulk re-fetch is worth launching, by classifying *why* the prior pass failed
on a stratified sample.

Stages per ID (each gets its own status column, so failures are class-coded):
  1. metadata    GET https://www.osti.gov/api/v1/records/<id>    (does record exist?)
  2. landing     GET https://www.osti.gov/biblio/<id>            (is it routable?)
  3. purl_fetch  GET https://www.osti.gov/servlets/purl/<id>     (PDF? 403? RST?)
  4. pdf_body    inspect saved body for %PDF magic + size
  5. text_check  optional pdftotext probe (skipped if not installed)

Failure-class labels per stage:
  200 / 302       ok
  403             forbidden                       <- OSTI access policy
  404             not_found
  5xx             server_error
  err:RemoteDisconnected   TCP reset              <- transient rate limit OR access
  timeout                  socket timeout         <- network
  tls / dnserr             handshake / DNS errors
  empty / not_pdf          200 OK, but no PDF     <- redirect to HTML landing
  parse_*                  pdftotext failure mode

USAGE (run on cels-rbdgx2):
    cd /rbstor/stevens/osti_probe
    python3 probe_cels_failure_classes.py
    # reads sample_50_for_cels_probe.tsv (see build_failure_class_sample.py)
    # writes sample_50_probe_results.tsv + prints per-stage Counter summary

INTERPRETING THE OUTPUT:
  - Compute recovery rate = pdf_ok / total. If < 30% from CELS, DO NOT bulk fetch
    — investigate the dominant failure class first.
  - Break out the s4 outcome BY LAB. The failure pattern is usually lab-structural,
    not random. In the 2026-06-08 probe, PNNL / LBNL / Fermi / JLab were all
    0% recovery (mostly 403), while Argonne / SLAC / PPPL / BNL were 60-100%.
  - Distinguish the 403 cluster (OSTI access policy — won't change with retry,
    needs an email to comments@osti.gov) from the RemoteDisconnected cluster
    (transient — retry might recover) before deciding bulk strategy.

DOES NOT need pdftotext to be useful — the s1-s4 stages are the decision-grade
signal. The s5 stage is a bonus for verifying parse-ability when you have it.
"""
from __future__ import annotations
import csv
import subprocess
import sys
import time
import urllib.request
import urllib.error
import socket
import ssl
from pathlib import Path

INPUT = Path("sample_50_for_cels_probe.tsv")
OUTPUT = Path("sample_50_probe_results.tsv")
TMP_PDF = Path("/tmp/_osti_probe.pdf")
TIMEOUT = 30
UA = "Mozilla/5.0 (compatible; Argonne-OSTI-corpus-probe/1.0)"


def classify(exc_or_status):
    if isinstance(exc_or_status, int):
        if exc_or_status == 200: return "200"
        if exc_or_status == 302: return "302"
        if exc_or_status == 404: return "404"
        if exc_or_status == 403: return "403"
        if 500 <= exc_or_status < 600: return f"{exc_or_status}"
        return f"http_{exc_or_status}"
    e = exc_or_status
    if isinstance(e, (socket.timeout, TimeoutError)): return "timeout"
    if isinstance(e, ssl.SSLError): return "tls"
    if isinstance(e, socket.gaierror): return "dnserr"
    if isinstance(e, urllib.error.HTTPError): return classify(e.code)
    if isinstance(e, urllib.error.URLError):
        reason = str(e.reason)
        if "timed out" in reason: return "timeout"
        if "TLS" in reason or "SSL" in reason: return "tls"
        if "Name or service" in reason or "nodename" in reason: return "dnserr"
        return f"urlerr:{reason[:30]}"
    return f"err:{type(e).__name__}"


def probe(url, save_to=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if save_to is not None:
                save_to.write_bytes(r.read())
            else:
                r.read(1)
            return classify(r.status), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        return classify(e), int((time.time() - t0) * 1000)
    except Exception as e:
        return classify(e), int((time.time() - t0) * 1000)


def probe_id(osti_id):
    out = {"osti_id": osti_id}

    s, ms = probe(f"https://www.osti.gov/api/v1/records/{osti_id}")
    out["s1_metadata"], out["s1_ms"] = s, ms

    s, ms = probe(f"https://www.osti.gov/biblio/{osti_id}")
    out["s2_landing"], out["s2_ms"] = s, ms

    if TMP_PDF.exists():
        TMP_PDF.unlink()
    s, ms = probe(f"https://www.osti.gov/servlets/purl/{osti_id}", save_to=TMP_PDF)
    out["s3_purl_fetch"], out["s3_ms"] = s, ms

    if TMP_PDF.exists():
        size = TMP_PDF.stat().st_size
        out["pdf_bytes"] = size
        if size == 0:
            out["s4_pdf_body"] = "empty"
        else:
            with TMP_PDF.open("rb") as f:
                head = f.read(8)
            out["s4_pdf_body"] = "pdf_ok" if head.startswith(b"%PDF") else "not_pdf"
    else:
        out["pdf_bytes"] = 0
        out["s4_pdf_body"] = "no_file"

    if out["s4_pdf_body"] == "pdf_ok":
        try:
            r = subprocess.run(
                ["pdftotext", "-l", "2", "-q", str(TMP_PDF), "-"],
                capture_output=True, timeout=20
            )
            text = r.stdout.decode(errors="ignore").strip()
            if r.returncode == 0 and len(text) > 50:
                out["s5_text"] = f"ok_{len(text)}c"
            elif r.returncode != 0:
                out["s5_text"] = f"parse_rc{r.returncode}"
            else:
                out["s5_text"] = "parse_empty"
        except FileNotFoundError:
            out["s5_text"] = "no_pdftotext"
        except subprocess.TimeoutExpired:
            out["s5_text"] = "parse_timeout"
        except Exception as e:
            out["s5_text"] = f"parse_err:{type(e).__name__}"
    else:
        out["s5_text"] = "skipped"

    return out


def main():
    if not INPUT.exists():
        print(f"FAIL: {INPUT} not in cwd", file=sys.stderr); sys.exit(1)
    rows_in = list(csv.DictReader(INPUT.open(), delimiter="\t"))
    print(f"probing {len(rows_in)} IDs from {INPUT}", file=sys.stderr)
    print(f"hostname: {socket.gethostname()}", file=sys.stderr)

    results = []
    for i, row in enumerate(rows_in, 1):
        oid = row["osti_id"]
        r = probe_id(oid)
        r["lab"] = row["lab"]
        r["year"] = row["year"]
        results.append(r)
        print(f"  [{i:2}/{len(rows_in)}] {oid:>8} {row['lab'][:20]:20} "
              f"s3={r['s3_purl_fetch']:>10} s4={r['s4_pdf_body']:>10} "
              f"s5={r['s5_text']:>15}", file=sys.stderr)

    cols = ["osti_id", "lab", "year",
            "s1_metadata", "s1_ms", "s2_landing", "s2_ms",
            "s3_purl_fetch", "s3_ms", "s4_pdf_body", "pdf_bytes", "s5_text"]
    with OUTPUT.open("w") as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in cols})
    print(f"\nwrote {OUTPUT}", file=sys.stderr)

    from collections import Counter
    print("\n== summary ==", file=sys.stderr)
    for stage in ["s1_metadata", "s2_landing", "s3_purl_fetch", "s4_pdf_body", "s5_text"]:
        c = Counter(r[stage] for r in results)
        print(f"  {stage:18} {dict(c)}", file=sys.stderr)

    recovered = sum(1 for r in results if r["s4_pdf_body"] == "pdf_ok")
    print(f"\n  recovery (s4=pdf_ok): {recovered}/{len(results)} = "
          f"{100*recovered/len(results):.1f}%", file=sys.stderr)
    print("\n  GATE: if recovery < 30% from CELS, DO NOT bulk fetch yet.", file=sys.stderr)
    print("        Investigate the dominant failure class first.", file=sys.stderr)
    print("        Cluster s4 by lab — failure pattern is usually lab-structural.", file=sys.stderr)


if __name__ == "__main__":
    main()
