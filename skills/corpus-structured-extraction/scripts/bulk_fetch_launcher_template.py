#!/usr/bin/env python3
"""
bulk_fetch_launcher_template.py — hardened bulk-fetch launcher template.

For the class of task "bulk fetch N thousand things from a flaky external API"
(OSTI PURLs, arXiv PDFs, publisher landing pages, registry records, etc.).

Embeds the locked rules that have emerged across several OSTI/arXiv corpus
projects in this skill:

  - streaming download + magic-byte check (configurable; %PDF for PDFs)
  - configurable size cap, classify overshoot as oversize_* (recoverable
    with --cap-mb raise) NOT as failure
  - per-stage timeouts (meta / landing / payload) — payload is the longest
  - 3 attempts max with exponential backoff 1s/3s/9s on TRANSIENT buckets
    only (reset, timeout). NEVER retry: 403, 404, terminal magic-byte
    failures (e.g. canned "not available" HTML pages with fixed byte size),
    oversize.
  - append-only JSONL checkpoint keyed by primary ID; restart skips
    completed IDs (true resume, no in-memory dedup needed)
  - polite rate floor (default 8s/req — UAN/login-node safe); --rate Hz to
    relax up to a hard cap (3 Hz here, raise/lower per API tolerance)
  - per-record exclusion-by-attribute (e.g. exclude certain labs/years/
    sources by default; --include-deferred flag to override)
  - --dry-run mode prints lab/year/source distribution of TO-RUN records,
    excluded counts, output paths, settings, first 10 IDs, wall-time
    estimate — and exits WITHOUT any network fetch
  - --limit N for pilot runs
  - MANIFEST.json snapshot per run with full settings + counts for audit

Input manifest: JSONL with at least {<id_field>, <stratum_field>}. Optional
metadata fields are passed through to the output for downstream analysis.

Output:
  <outdir>/results.jsonl   append-only, one row per ID processed
  <outdir>/MANIFEST.json   settings snapshot
  <outdir>/launcher.log    operational log
  <outdir>/payloads/       payload files (PDFs etc) named by ID

Designed to run via screen/tmux on a host with stable network egress.
No fetches happen in --dry-run mode.

Customization points (marked CUSTOMIZE):
  - ID field name + stratum field name (default: osti_id, lab)
  - Stage URLs (default: OSTI meta + biblio + PURL)
  - Payload magic bytes (default: %PDF)
  - Terminal HTML size signatures (default: 4231 = OSTI "PDF not available")
  - DEFERRED_STRATA set (default: Fermi + JLab for OSTI)
  - Default cap, rate floor, retry schedule

Reference: data-science/corpus-structured-extraction/SKILL.md "Pitfalls"
  section, especially the diagnostic-probe-shape and the
  fetch-time-magic-check pitfalls.
"""

from __future__ import annotations
import argparse
import collections
import datetime as dt
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request

# ---- CUSTOMIZE: field names ----
ID_FIELD = "osti_id"
STRATUM_FIELD = "lab"

# ---- CUSTOMIZE: stage URLs ----
def meta_url(id_):    return f"https://www.osti.gov/api/v1/records?osti_id={id_}"
def landing_url(id_): return f"https://www.osti.gov/biblio/{id_}"
def payload_url(id_): return f"https://www.osti.gov/servlets/purl/{id_}"

# ---- CUSTOMIZE: payload validation ----
PAYLOAD_MAGIC = b"%PDF"
PAYLOAD_EXT = ".pdf"
# Terminal failure: server returns HTTP 200 with a fixed-size HTML "not
# available" page. Discovered by inspecting probe outputs — all such
# responses were byte-identical at this size. Adjust per API.
TERMINAL_HTML_SIZES = {4231}  # OSTI canned "PDF not available"

# ---- CUSTOMIZE: deferred strata excluded by default ----
DEFERRED_STRATA = {
    "Fermi National Accelerator Laboratory",
    "Thomas Jefferson National Accelerator Facility",
}

# ---- Defaults (tunable) ----
USER_AGENT = "bulk-fetch-template/1.0 (set-this-per-project)"
META_TIMEOUT = 10
LANDING_TIMEOUT = 10
PAYLOAD_TIMEOUT = 60
DEFAULT_CAP_BYTES = 100 * 1024 * 1024  # 100 MB (raised from 50 — OSTI probe
                                       # 2026-06-09 found real PDFs up to 52MB;
                                       # 50MB cap rejected ~3% as false-oversize)
DEFAULT_RATE_FLOOR_SEC = 8.0  # 1 req / 8s — UAN/login-node polite
MAX_RATE_HZ = 3.0  # hard cap
RETRY_SCHEDULE = [1, 3, 9]  # 3 attempts total (initial + 2 retries)
TRANSIENT_BUCKETS = {"reset", "timeout"}


def _now() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")


def _classify_network_error(e: Exception) -> str:
    """Map a network exception to a stable bucket label."""
    name = type(e).__name__
    msg = str(e).lower()
    if isinstance(e, socket.timeout) or "timed out" in msg or "timeout" in msg:
        return "timeout"
    if isinstance(e, ConnectionResetError) or "reset" in msg:
        return "reset"
    if isinstance(e, http.client.RemoteDisconnected):
        return "reset"
    if isinstance(e, urllib.error.HTTPError):
        return f"http_{e.code}"
    if isinstance(e, urllib.error.URLError):
        return "url_error"
    if isinstance(e, ssl.SSLError):
        return "ssl_error"
    return f"err_{name.lower()}"


def fetch_stage_head(url: str, timeout: int, accept: str = "*/*") -> dict:
    """Generic non-streaming stage fetch (meta, landing)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"http": resp.status, "ctype": resp.headers.get("Content-Type", ""),
                    "final_url": resp.geturl(), "bucket": "ok"}
    except urllib.error.HTTPError as e:
        return {"http": e.code, "ctype": e.headers.get("Content-Type", "") if e.headers else "",
                "final_url": url, "bucket": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001 (broad except is intentional — see SKILL.md)
        return {"http": None, "ctype": "", "final_url": url,
                "bucket": _classify_network_error(e), "error": type(e).__name__}


def fetch_payload_streaming(id_: str, payload_outdir: str, cap_bytes: int) -> dict:
    """
    Stream the payload, enforcing cap and magic-byte check. Writes payload to
    <payload_outdir>/<id><PAYLOAD_EXT> on success. Returns bucket result dict.
    """
    url = payload_url(id_)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                "Accept": f"application/{PAYLOAD_MAGIC[1:].decode()},*/*"})
    tmppath = os.path.join(payload_outdir, f"{id_}{PAYLOAD_EXT}.tmp")
    finalpath = os.path.join(payload_outdir, f"{id_}{PAYLOAD_EXT}")
    try:
        with urllib.request.urlopen(req, timeout=PAYLOAD_TIMEOUT) as resp:
            ctype = resp.headers.get("Content-Type", "")
            final_url = resp.geturl()
            written = 0
            head_bytes = b""
            sha = hashlib.sha256()
            with open(tmppath, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    if not head_bytes:
                        head_bytes = chunk[:8]
                    written += len(chunk)
                    sha.update(chunk)
                    out.write(chunk)
                    if written > cap_bytes:
                        out.close()
                        try: os.remove(tmppath)
                        except: pass
                        is_payload = head_bytes.startswith(PAYLOAD_MAGIC)
                        return {"http": resp.status, "ctype": ctype, "size": written,
                                "final_url": final_url, "cap_bytes": cap_bytes,
                                "bucket": "oversize_payload" if is_payload else "oversize_non_payload",
                                "head_hex": head_bytes.hex()}

        is_payload = head_bytes.startswith(PAYLOAD_MAGIC)
        if is_payload:
            os.rename(tmppath, finalpath)
            return {"http": resp.status, "ctype": ctype, "size": written,
                    "final_url": final_url, "sha256": sha.hexdigest(), "bucket": "payload_ok"}
        else:
            try: os.remove(tmppath)
            except: pass
            # Terminal "not available" page check
            bucket = f"not_payload_html_{written}" if written in TERMINAL_HTML_SIZES else "not_payload"
            return {"http": resp.status, "ctype": ctype, "size": written, "final_url": final_url,
                    "head_hex": head_bytes.hex(), "bucket": bucket}
    except urllib.error.HTTPError as e:
        try: os.remove(tmppath)
        except: pass
        return {"http": e.code, "ctype": e.headers.get("Content-Type", "") if e.headers else "",
                "final_url": url, "bucket": f"http_{e.code}"}
    except Exception as e:  # noqa: BLE001
        try: os.remove(tmppath)
        except: pass
        return {"http": None, "ctype": "", "final_url": url,
                "bucket": _classify_network_error(e), "error": type(e).__name__}


def fetch_with_retry(id_: str, payload_outdir: str, cap_bytes: int, log) -> dict:
    """Run meta → landing → payload. Retry ONLY payload on transient bucket."""
    t0 = time.time()
    meta = fetch_stage_head(meta_url(id_), META_TIMEOUT, accept="application/json")
    landing = fetch_stage_head(landing_url(id_), LANDING_TIMEOUT, accept="text/html")

    payload = fetch_payload_streaming(id_, payload_outdir, cap_bytes)
    attempts = 1
    while payload["bucket"] in TRANSIENT_BUCKETS and attempts < len(RETRY_SCHEDULE) + 1:
        backoff = RETRY_SCHEDULE[attempts - 1]
        log(f"  [{id_}] payload {payload['bucket']} attempt {attempts}/{len(RETRY_SCHEDULE)+1}, sleeping {backoff}s")
        time.sleep(backoff)
        payload = fetch_payload_streaming(id_, payload_outdir, cap_bytes)
        attempts += 1

    return {
        ID_FIELD: id_, "ts": _now(),
        "meta": meta, "landing": landing, "payload": payload,
        "attempts": attempts, "dt": round(time.time() - t0, 2),
    }


def load_manifest(path: str) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            out.append(json.loads(line))
    return out


def load_checkpoint(results_path: str) -> set[str]:
    """Return set of IDs already in results.jsonl (true resume)."""
    done = set()
    if not os.path.exists(results_path):
        return done
    with open(results_path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if ID_FIELD in r:
                    done.add(str(r[ID_FIELD]))
            except: pass
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True, help=f"JSONL with {{{ID_FIELD}, {STRATUM_FIELD}, ...}} per line")
    ap.add_argument("--outdir", required=True, help="Output directory (created if absent)")
    ap.add_argument("--cap-mb", type=int, default=100, help="Payload size cap in MB (default 100)")
    ap.add_argument("--rate", type=float, default=None,
                    help=f"Max requests per second (default polite {1/DEFAULT_RATE_FLOOR_SEC:.3f} Hz = 1/{DEFAULT_RATE_FLOOR_SEC}s); cap {MAX_RATE_HZ} Hz")
    ap.add_argument("--limit", type=int, default=None, help="Process only first N (after exclusions+checkpoint)")
    ap.add_argument("--include-deferred", action="store_true",
                    help=f"Include deferred strata (default: excluded). Deferred: {sorted(DEFERRED_STRATA)}")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan + first 10 IDs, write MANIFEST.json, do NOT fetch")
    ap.add_argument("--stratum-filter", default=None,
                    help=f"Comma-separated {STRATUM_FIELD} substrings; only matching records processed")
    args = ap.parse_args()

    cap_bytes = args.cap_mb * 1024 * 1024
    if args.rate is not None:
        rate_hz = min(args.rate, MAX_RATE_HZ)
        sleep_per = max(1.0 / rate_hz, 1.0 / MAX_RATE_HZ)
    else:
        sleep_per = DEFAULT_RATE_FLOOR_SEC
        rate_hz = 1.0 / sleep_per

    os.makedirs(args.outdir, exist_ok=True)
    payload_outdir = os.path.join(args.outdir, "payloads")
    os.makedirs(payload_outdir, exist_ok=True)
    results_path = os.path.join(args.outdir, "results.jsonl")
    manifest_snapshot = os.path.join(args.outdir, "MANIFEST.json")
    log_path = os.path.join(args.outdir, "launcher.log")

    manifest = load_manifest(args.manifest)
    n_total = len(manifest)

    if args.stratum_filter:
        needles = [s.strip() for s in args.stratum_filter.split(",") if s.strip()]
        manifest = [r for r in manifest if any(n in r.get(STRATUM_FIELD, "") for n in needles)]
    n_after_stratum = len(manifest)

    if not args.include_deferred:
        manifest = [r for r in manifest if r.get(STRATUM_FIELD) not in DEFERRED_STRATA]
    n_after_deferred = len(manifest)
    n_deferred_excluded = n_after_stratum - n_after_deferred

    done = load_checkpoint(results_path)
    manifest = [r for r in manifest if str(r[ID_FIELD]) not in done]
    n_after_checkpoint = len(manifest)
    n_already_done = n_after_deferred - n_after_checkpoint

    if args.limit is not None:
        manifest = manifest[: args.limit]
    n_to_run = len(manifest)

    stratum_counts = collections.Counter(r.get(STRATUM_FIELD, "UNKNOWN") for r in manifest)
    deferred_counts = collections.Counter(
        r.get(STRATUM_FIELD, "UNKNOWN") for r in load_manifest(args.manifest)
        if r.get(STRATUM_FIELD) in DEFERRED_STRATA
    )

    snap = {
        "version": "1.0", "ts": _now(), "host": socket.gethostname(), "argv": sys.argv,
        "settings": {
            "cap_mb": args.cap_mb, "cap_bytes": cap_bytes,
            "rate_hz": round(rate_hz, 4), "sleep_per_req_sec": round(sleep_per, 3),
            "max_attempts": len(RETRY_SCHEDULE) + 1, "retry_schedule_sec": RETRY_SCHEDULE,
            "transient_buckets_retried": sorted(TRANSIENT_BUCKETS),
            "terminal_buckets_no_retry": ["http_403", "http_404", "not_payload",
                                          f"not_payload_html_{s}" for s in TERMINAL_HTML_SIZES] +
                                         ["oversize_payload", "oversize_non_payload"],
            "stage_timeouts": {"meta_sec": META_TIMEOUT, "landing_sec": LANDING_TIMEOUT,
                               "payload_sec": PAYLOAD_TIMEOUT},
            "deferred_strata_excluded_by_default": sorted(DEFERRED_STRATA),
            "include_deferred": args.include_deferred,
            "stratum_filter": args.stratum_filter, "limit": args.limit, "dry_run": args.dry_run,
        },
        "counts": {
            "manifest_total": n_total, "after_stratum_filter": n_after_stratum,
            "deferred_excluded": n_deferred_excluded,
            "already_completed_in_checkpoint": n_already_done, "to_run": n_to_run,
        },
        "stratum_distribution_to_run": dict(stratum_counts.most_common()),
        "deferred_stratum_distribution": dict(deferred_counts.most_common()),
        "paths": {
            "manifest": os.path.abspath(args.manifest), "outdir": os.path.abspath(args.outdir),
            "results_jsonl": os.path.abspath(results_path),
            "payload_outdir": os.path.abspath(payload_outdir), "log": os.path.abspath(log_path),
        },
    }
    with open(manifest_snapshot, "w") as f:
        json.dump(snap, f, indent=2, default=str)

    print(f"=== bulk fetch — {'DRY RUN' if args.dry_run else 'LIVE'} ===")
    print(f"  host:                  {snap['host']}")
    print(f"  manifest:              {snap['paths']['manifest']}")
    print(f"  outdir:                {snap['paths']['outdir']}")
    print(f"  cap:                   {args.cap_mb} MB ({cap_bytes:,} bytes)")
    print(f"  rate:                  {rate_hz:.4f} Hz ({sleep_per:.3f} s/req)")
    print(f"  retry:                 up to {len(RETRY_SCHEDULE)+1} attempts on {sorted(TRANSIENT_BUCKETS)}, backoff {RETRY_SCHEDULE}")
    print(f"  stage timeouts:        meta={META_TIMEOUT}s landing={LANDING_TIMEOUT}s payload={PAYLOAD_TIMEOUT}s")
    print()
    print(f"  manifest total:        {n_total:>6}")
    if args.stratum_filter:
        print(f"  after stratum filter:  {n_after_stratum:>6}  (filter: {args.stratum_filter})")
    print(f"  deferred-excluded:     {n_deferred_excluded:>6}  ({'INCLUDED' if args.include_deferred else 'excluded'}: {sorted(DEFERRED_STRATA)})")
    print(f"  already in checkpoint: {n_already_done:>6}")
    if args.limit:
        print(f"  after --limit {args.limit:<5}    {n_to_run:>6}")
    print(f"  TO RUN:                {n_to_run:>6}")
    print()
    print(f"  Stratum distribution (to-run, top 15):")
    for s, n in stratum_counts.most_common(15):
        print(f"    {n:>6}  {s}")
    if deferred_counts:
        print(f"  Deferred (excluded by default):")
        for s, n in deferred_counts.most_common():
            print(f"    {n:>6}  {s}")
    print()
    print(f"  First 10 IDs:")
    for r in manifest[:10]:
        print(f"    {str(r[ID_FIELD]):>10}  [{r.get(STRATUM_FIELD,'UNKNOWN'):.40s}]")
    print()
    eta_sec = n_to_run * sleep_per
    print(f"  Wall-time estimate (no retries, no fetch time): {eta_sec:.0f}s = {eta_sec/3600:.2f}h")
    print(f"  MANIFEST snapshot: {manifest_snapshot}")

    if args.dry_run:
        print("\n=== DRY RUN — no fetches performed. Review and re-run without --dry-run to launch. ===")
        return 0

    log_f = open(log_path, "a")
    def log(msg):
        line = f"[{_now()}] {msg}"
        print(line, flush=True); log_f.write(line + "\n"); log_f.flush()

    log(f"LAUNCH n_to_run={n_to_run} rate={rate_hz:.4f}Hz cap={args.cap_mb}MB")
    res_f = open(results_path, "a")
    last_req_t = 0.0
    bucket_counts = collections.Counter()
    started = time.time()
    try:
        for i, r in enumerate(manifest, 1):
            now = time.time()
            wait = sleep_per - (now - last_req_t)
            if wait > 0:
                time.sleep(wait)
            last_req_t = time.time()

            res = fetch_with_retry(str(r[ID_FIELD]), payload_outdir, cap_bytes, log)
            res[STRATUM_FIELD] = r.get(STRATUM_FIELD, "UNKNOWN")
            res_f.write(json.dumps(res) + "\n")
            res_f.flush()
            bucket_counts[res["payload"]["bucket"]] += 1
            if i % 25 == 0 or i == n_to_run:
                elapsed = time.time() - started
                top = ", ".join(f"{k}={v}" for k, v in bucket_counts.most_common(6))
                log(f"  progress {i}/{n_to_run} ({i/elapsed:.2f}/s avg) buckets: {top}")
    except KeyboardInterrupt:
        log("interrupted by user")
    finally:
        res_f.close()
        log(f"DONE buckets: {dict(bucket_counts)}")
        log_f.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
