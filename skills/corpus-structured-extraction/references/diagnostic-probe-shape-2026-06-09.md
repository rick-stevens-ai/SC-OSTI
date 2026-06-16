# Diagnostic probe shape must match bulk-launcher shape

**Worked example: OSTI failed-recovery refetch via cels-rbdgx2, 2026-06-09.**

## The rule

When you run a diagnostic probe to decide whether to launch an expensive bulk
operation, the probe MUST share the same operational shape as the bulk it's
gating — same retry policy, same timeouts, same response-size caps, same
rate-limiting. A probe that runs single-attempt with one set of caps and a
bulk launcher that runs multi-attempt with different caps are measuring
**different upstream surfaces** and you cannot derive a gate decision from one
about the other.

## The failure case

The 8,707 OSTI failed-recovery list (gap from prior corpus refresh) needed a
gate decision: does CELS-host fetch materially beat home-IP fetch?

**Probe v1** (single-attempt, 30s timeout, no retries, 5MB cap):
- 38-ID stratified sample across 9 SC labs
- Result: 10/38 = **26.3%** pdf_ok
- Buckets: 13 × 403 Forbidden, 11 × RemoteDisconnected, 4 × not_pdf, 10 × pdf_ok
- First diagnosis: "OSTI walls off PNNL/LBNL/Fermi/JLab at PURL endpoint —
  structural failure, bulk not justified, draft email to comments@osti.gov"
- Drafted email v1 indicting OSTI for 4-lab walling.

**Probe v2** (3-attempt retry with exponential backoff 1s/3s/9s, per-stage
timeouts meta 10s / landing 10s / PURL+PDF 20s, 5MB cap kept, retry ONLY on
RemoteDisconnected/timeout — never on 403/404):
- Same 38-ID sample
- Result: 18/38 = **47.4%** pdf_ok on first attempt; **zero retries actually
  triggered** (`attempts: {1: 38}`)
- Cross-tab vs v1: 15 IDs went `first_fail → retry_ok` (transient!), 7 went
  `first_ok → retry_fail` (5MB cap rejected real big PDFs that v1 had
  truncated-and-counted as ok), 3 stayed ok, 13 stayed fail
- **Realistic recovery excluding the 5MB cap rejections: 31/38 = 81.6%**

The reversal: gate decision flipped from FAIL (26% — not worth bulk) to PASS
(82% — strong CELS-side improvement worth the bandwidth). The email draft v1
got retracted; v2 scoped to just the actual residual (Fermi 5/5 + JLab 3/4
landing-page HTML instead of PDF — true OSTI-side issue).

## Per-lab numbers (probe v2)

| Lab | v2 recovery | v1 recovery | Notes |
|---|---|---|---|
| LBNL | 5/5 | 0/5 | Transient resets, all recovered |
| PNNL | 4/5 | ? | Transient |
| SLAC | 4/5 | ? | Transient |
| ORNL | 3/5 | ? | Mixed |
| Argonne | 0/5 | 5/5 | ALL 5 oversize >5MB (cap rejected real PDFs) |
| Fermi | 0/5 | 0/5 | Real residual — HTML landing pages, not PDFs |
| JLab | 1/4 | ? | Mixed; 3/4 fail is OSTI-side |

The 5MB cap turned out to be the biggest sampling artifact — it inverted the
verdict for Argonne entirely. Any future probe should size the cap to the
bulk-target cap, not pick something tighter "to be safe."

## What the probe should have been from the start

```
- N stratified samples (≥5 per lab, ≥30 total)
- Same retry policy bulk will use (3 attempts, exp backoff)
- Same per-stage timeouts bulk will use
- Same response-size cap bulk will use — NOT a smaller "safe" cap
- Same rate limit bulk will use
- Same user-agent and headers bulk will use
- Classify failures into buckets (not aggregate counts):
  recovered / 404 / 403 / timeout / rate-limit / wrong-type / empty / extract-error
- Decision rule: if recovery materially beats baseline AND dominant failure
  bucket is fixable from this vantage, PASS gate. If dominant bucket is
  structural (host-policy 403s on specific labs), STOP and inspect.
```

Then a single probe run gives you the gate decision. No reversal cycle.

## Pre-flight checklist for any "should I launch this bulk operation" probe

Answer in writing before scp'ing the probe script:

1. **What's the bulk launcher's retry policy?** Probe matches it.
2. **What's the bulk launcher's per-stage timeout?** Probe matches it.
3. **What's the bulk launcher's response-size cap?** Probe matches it.
4. **What's the bulk launcher's rate limit?** Probe matches it.
5. **Which failure buckets are fixable from my vantage vs structural?**
   Probe classifies into both.
6. **What's the stratification axis** (per lab / per year / per source)?
   Probe samples that axis, ≥5 per stratum.
7. **What is the actual population distribution along that axis?** Compute
   it from the manifest BEFORE proposing a "stratified pilot." A
   lab-balanced 38-ID probe of an 8,707-ID set that turns out to be 92%
   LBNL gave us false confidence that the pilot/launcher plan should be
   "stratified across recoverable labs." Reality: an honest pilot off the
   actual manifest is ~92% LBNL too. See "Probe stratification vs
   population stratification" section below.

If any of these differ between probe and bulk, you are about to make a
bad gate decision. Fix the probe shape first.

## Probe stratification vs population stratification — distinct concerns

The 38-ID probe was correctly **lab-balanced for measurement fairness**:
≥3 per lab so per-lab recovery rates are statistically meaningful and the
cross-lab pattern (Fermi/JLab structural, others recoverable) is visible.
That's the right shape for a diagnostic.

The pilot/launcher plan that follows the probe is a **different question**.
The pilot's job is to validate launcher mechanics + recovery rate at scale
against the actual production data shape — not to re-validate the cross-lab
pattern (which the probe already did).

These shapes can differ wildly, and they did:

| Lab | Probe (38) | Manifest (8,707) | Pilot if naive limit-500 |
|---|---|---|---|
| LBNL | 5 (13%) | 7,969 (92%) | 494 |
| SLAC | 5 (13%) | 420 (5%) | 5 |
| ORNL | 5 (13%) | 136 (1.6%) | 1 |
| Argonne | 5 (13%) | 122 (1.4%) | 0 |
| PNNL | 5 (13%) | 9 (0.1%) | 0 |
| Fermi | 5 (13%) | 13 (0.15%) | excluded |
| JLab | 4 (10%) | 2 (0.02%) | excluded |

If you propose "a 500-ID pilot stratified across recoverable labs" off the
probe shape without ever looking at the manifest distribution, you spend
the pilot validating a cross-lab pattern the probe already established,
on a sample that does not resemble the actual run at all. When you scale
to the full bulk, you're running 92% LBNL traffic for the first time —
the pilot didn't actually de-risk it.

**Two pilot shapes, pick deliberately:**

**A. Population-shaped pilot.** Straight `--limit N` off the manifest in
manifest order. Validates launcher mechanics at scale, validates recovery
rate on the lab that dominates the run, gives an honest preview. Doesn't
re-validate cross-lab pattern (which the probe already did). Recommended
default when probe already covered cross-lab.

**B. Stratified-validation pilot.** Build a separate manifest with N/k per
recoverable lab. Useful only if you want fresh cross-lab signal at larger
N — e.g. if the probe was very small (<5 per lab) or if probe recovery
rates were close to your decision threshold and you want tighter
confidence intervals. Otherwise it's redundant.

**Mandatory step before proposing any pilot shape:** load the manifest,
print the lab/year/source distribution, compare to probe distribution.
If they diverge sharply, surface the divergence in the recommendation —
do not silently inherit the probe's stratification as the pilot's
stratification.

### Worked numbers — OSTI 8,707 failed-recovery, 2026-06-09 launcher dry-run

Lab breakdown of the actual manifest (computed from `recon_v2/*.jsonl`
join — 8,696 tagged, 11 UNKNOWN):

```
 7969  Lawrence Berkeley National Laboratory   (92%)
  420  SLAC National Accelerator Laboratory    (4.8%)
  136  Oak Ridge National Laboratory           (1.6%)
  122  Argonne National Laboratory             (1.4%)
   18  Brookhaven National Laboratory          (0.2%)
   11  UNKNOWN
    9  Pacific Northwest National Laboratory   (0.1%)
    4  Ames Laboratory                         (0.05%)
    3  Princeton Plasma Physics Laboratory     (0.03%)
   13  Fermi (deferred)
    2  JLab (deferred)
```

The default-exclusion of Fermi+JLab saves 15 IDs out of 8,707 — not the
"500-700 structurally unrecoverable" estimate I'd previously memorized
from the probe-era framing. The exclusion is still right (those IDs
won't recover), but the savings are small and the rest of the work is
overwhelmingly LBNL.

## Related pitfalls already in this skill

- "Smoke before scale" — establishes you should run a small test first.
  This reference adds: the small test must have the same operational shape
  as the eventual large one.
- "Rate-limit signature is per-API" — picks the worker count by progressive
  testing. Same idea but for retry/timeout/cap.
- "Multi-candidate cleanup beats smarter regex for mangled identifiers" —
  resolver judges, regex transforms. Probe shape parallels this:
  *single-attempt* judges nothing about the *3-attempt* world.

## Artifacts on disk

- Probe v1 script: `~/code/osti-replication-candidates/probe_cels.py`
- Probe v2 script: `cels-rbdgx2:/rbstor/stevens/osti_probe/probe_cels_rbdgx2_20260609-0302/retry_probe.py`
- Run dir: `cels-rbdgx2:/rbstor/stevens/osti_probe/probe_cels_rbdgx2_20260609-0302/`
  - SUMMARY.md (v1)
  - RETRY_SUMMARY.md (v2)
  - results.tsv (v1)
  - retry_results.jsonl (v2)
- Email drafts: `~/code/osti-replication-candidates/osti_email_DRAFT.txt`
  (v1 retracted; v2 scoped to Fermi+JLab residual)
