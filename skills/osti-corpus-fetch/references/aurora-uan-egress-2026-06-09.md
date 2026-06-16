# Aurora UAN as an OSTI fetch host

**Discovered 2026-06-09.** The CELS DGX hosts are NOT the only viable fetch path. Aurora UAN (login nodes, e.g. `aurora-uan-0009`) reaches OSTI's PURL endpoint over a materially more stable network path than `cels-rbdgx2`, with 2.4× the first-attempt recovery rate on the same stratified sample.

This reference complements (does NOT replace) `failure-class-probe-2026-06-08.md`. Whenever you're staging a bulk OSTI fetch, **probe both hosts** before committing — the empirical answer changes over time and per-lab failure clustering is partly path-dependent.

## TL;DR

| Fetch host | Path | First-attempt recovery (same 38-ID sample) | Reset rate | Polite rate |
|---|---|---|---|---|
| Home (m1) | Direct | unusable (PURL 503/404) | n/a | n/a |
| cels-rbdgx2 | CELS NFS | 26% (first), 47% (retry) | 11/38 resets | 4 workers, 8s polite |
| **aurora-uan-0009** | **ALCF login** | **63%** | **0/38** | **1 req / 8s sequential** |

The Aurora UAN path is recommended for the recoverable-lab subset of the bulk re-fetch pool.

## Why use UAN, not Aurora compute

OSTI fetch is HTTP/network-bound — no GPU, no heavy CPU. Aurora compute nodes:
- may have restricted/no public egress
- burn allocation hours on what's mostly idle network waits
- need PBS queue submission for what's a single thread of work

UAN (login nodes):
- have direct outbound HTTPS to OSTI (verified `https://www.osti.gov/api/v1/records?osti_id=22321055` → 200 in 130ms)
- shared with other users — must run polite (1 req / 8s sequential, no parallelism)
- survive disconnect via `screen` or `tmux`

**Rule: use UAN for OSTI fetch. Use compute only if specifically testing compute-node egress as its own experiment.**

## Probe recipe (reusable)

Reuse the 38-ID stratified sample from any prior CELS probe so results are directly comparable. Bundle saved at `~/code/osti-replication-candidates/probe_uan_<YYYYMMDD-HHMMSS>/`.

### Setup

```bash
# From m1, two-hop scp via cherryrd (Aurora ssh master lives there)
scp ~/code/osti-replication-candidates/uan_egress_probe.py \
    ~/code/osti-replication-candidates/sample_50_for_cels_probe.tsv \
    cherryrd:/tmp/
ssh cherryrd 'ssh aurora "mkdir -p /home/stevens/osti_probe"'
ssh cherryrd 'scp /tmp/uan_egress_probe.py /tmp/sample_50_for_cels_probe.tsv \
              aurora:/home/stevens/osti_probe/'
```

### Run

```bash
# Detach via nohup + setsid so it survives the ssh wrapper's 60s timeout.
# Python 3.10 is at /usr/bin/python3.10 on Aurora UAN — no module load needed.
ssh cherryrd 'ssh aurora "cd /home/stevens/osti_probe && \
    setsid nohup /usr/bin/python3.10 -u uan_egress_probe.py > foreground.log 2>&1 < /dev/null & disown"'
```

Then poll the log:

```bash
ssh cherryrd 'ssh aurora "wc -l /home/stevens/osti_probe/foreground.log; \
    tail -20 /home/stevens/osti_probe/foreground.log"'
```

Wait ~5 minutes for the 38-ID run (38 × 8s polite + ~2s avg fetch).

### Probe script behavior

`scripts/uan_egress_probe.py` (sibling): sequential, single-threaded, 8s sleep between IDs, hard per-stage timeouts (meta 10s, landing 10s, PURL 20s), 50MB streaming cap, magic-byte content sniff. Classifies into `pdf_ok`, `not_pdf_html`, `not_pdf_other`, `http_403`, `http_404`, `oversize`, `reset`, `timeout`. NO PDF bodies retained — only status/size/error metadata.

The bundle includes `SUMMARY.md` (per-bucket + cross-host table) and `results.jsonl` (one row per ID with all three stages).

## 2026-06-09 measurements (aurora-uan-0009)

### Headline

24/38 = **63% pdf_ok** on first attempt. 25/38 = 66% if oversize counts as recoverable (the 1 oversize was a single 52MB real PDF; raising cap to 100MB recovers it).

### Per-bucket

- `pdf_ok`: 24 (63%)
- `not_pdf_html`: 7 (18%) — **6 of 7 were exactly 4231 bytes** (OSTI's canned "PDF not available" page returned with HTTP 200)
- `http_403`: 6 (15%)
- `oversize`: 1 (>50MB, real PDF rejected by cap)
- `reset` / `timeout`: **0**

### Per-lab

| Lab | N | pdf_ok | 403 | not_pdf | oversize |
|---|---|---|---|---|---|
| Argonne | 5 | **5** | 0 | 0 | 0 |
| Lawrence Berkeley | 5 | **5** | 0 | 0 | 0 |
| Princeton Plasma Physics | 3 | **3** | 0 | 0 | 0 |
| Brookhaven | 1 | **1** | 0 | 0 | 0 |
| Oak Ridge | 5 | 4 | 0 | 0 | 1 |
| SLAC | 5 | 4 | 1 | 0 | 0 |
| Pacific Northwest | 5 | 2 | 3 | 0 | 0 |
| **Fermi** | 5 | **0** | 0 | **5** | 0 |
| **Thomas Jefferson** | 4 | **0** | 2 | 2 | 0 |

### Cross-host comparison (same 38 IDs)

| Bucket | rbdgx2 first | rbdgx2 retry | aurora UAN |
|---|---|---|---|
| pdf_ok | 10 (26%) | 18 (47%) | **24 (63%)** |
| 403/404 | 13 | 5 | 6 |
| not_pdf | 4 | 2 | 7 |
| oversize | 0 (5MB cap) | 13 (5MB cap) | 1 (50MB cap) |
| reset | 11 | 0 | **0** |

## Findings (durable, generalize beyond OSTI)

1. **Zero TCP resets from UAN.** The Aurora→OSTI path is materially more stable than CELS→OSTI. Whenever you're staging a bulk fetch from a CELS host and seeing more than a handful of `RemoteDisconnected` errors, try the same probe from Aurora UAN before adding retry logic.

2. **The Fermi 4231-byte canned HTML body is host-independent.** Same content on both hosts, both fail with magic-byte sniff. This confirms Fermi is genuinely OSTI-side (publisher-walled with a polite landing page). **Always validate PDF body with magic-byte check, never trust HTTP 200 alone.**

3. **JLab 0/4 on both hosts.** Also genuinely OSTI-side, mixed 403/HTML.

4. **PNNL 2/5 on UAN vs 0/5 first-pass + 4/5 retry on rbdgx2.** Likely host-IP rate limiting that's tighter on the Aurora /9 than on the CELS subnet — but partially recoverable on both. If you need PNNL coverage, retry from CELS after a UAN pass.

5. **Argonne, LBNL, PPPL, BNL are 100% recoverable from UAN.** No retry needed.

## Recommended pattern for any future bulk OSTI re-fetch

Before deciding which host to bulk from:

1. Pull a stratified sample (38-100 IDs, balanced across all labs that show up in the pool).
2. Run the failure-class probe on `cels-rbdgx2` (recipe: `failure-class-probe-2026-06-08.md`).
3. Run the egress probe on `aurora-uan-0009` (recipe: this file).
4. Compare per-bucket and per-lab. If UAN >> rbdgx2 with no resets, prefer UAN.
5. Bulk from the winner, excluding any labs that fail on both hosts (those need a `comments@osti.gov` email, not a retry).

## Bulk subset plan (if approved)

For the ~8,707-ID recovery pool:

- Exclude Fermi + JLab (~500-700 IDs based on per-lab miss rate, structurally unrecoverable from any host)
- Run from aurora-uan-0009
- Sequential, single-threaded, 8s polite floor
- 50MB content cap (raise to 100MB if oversize cluster appears)
- 3-attempt retry **only** on `reset` / `timeout` / `5xx` (never on 403/404/not_pdf)
- Resumable JSONL checkpoint keyed by `osti_id`
- Launch under `screen` or `tmux` so the run survives ssh disconnect
- Estimated wall time: ~17 hours for ~7,800 IDs at 8s/req
- Should NOT compete materially with other users — single-thread HTTP, minimal CPU

## Pitfalls hit during the 2026-06-09 probe

- **Aurora UAN's default `python3` is 3.6.15** — too old for f-strings with complex expressions, type hints, and modern `urllib` idioms. Use the explicit `/usr/bin/python3.10` shebang. The `module load python` loads 3.12 but isn't needed.

- **The 60s ssh wrapper timeout will look like the probe is hanging silently.** It's not — the probe is running, writing to disk; the ssh wrapper is just timing out before the probe logs anything observable. Detach with `setsid nohup ... & disown` and poll separately via fresh ssh.

- **The probe needs to redirect stdout/stderr to file BEFORE backgrounding**, not after — once setsid detaches, you can't recover the file descriptors. Do `> log 2>&1 < /dev/null &`, not `& > log`.

- **Empty `probe.log` doesn't mean the probe failed.** It usually means the Python `-u` flag isn't getting honored through the two-hop ssh wrapper, OR the log is buffering on the kernel side and hasn't flushed yet. Wait 60 more seconds before declaring it dead; check for a non-empty timestamped `probe_uan_*` directory as a second signal.

- **Two empty rundirs in a row** is a real failure mode — script reached the `RUNDIR.mkdir()` line and then died. Run the script in genuine foreground for a few IDs first (no nohup, no setsid) to see the actual error.

- **Don't run the probe from m1 over the double-hop.** Direct urllib calls through the m1-→cherryrd-→aurora ssh tunnel will hit weird timeouts. The script must run on the Aurora host itself.

## Aurora ssh path

- `cherryrd` has live ControlMasters for `aurora.alcf.anl.gov`, `polaris-login-04.alcf.anl.gov`, `crux.alcf.anl.gov` (PIDs 35547, 21094, 87998 as of 2026-06-09).
- From m1: `ssh cherryrd 'ssh -o ConnectTimeout=10 aurora "..."'`
- Two-hop scp: `scp file cherryrd:/tmp/` then `ssh cherryrd 'scp /tmp/file aurora:/path/'`
- Aurora homedir on UAN: `/home/stevens/`
- No `screen` configured by default on UAN — use `tmux` or `nohup ... & disown` for long runs.
