# Email validation battery — N=100 OSTI sample

Worked example, 2026-06-09. Rick: "validate some random selection of 100
emails we extracted from papers ... devise a set of tests ... fix things if
needed and update the repo." Corpus: 106,241 papers × `extract_emails.py`
(LLM via `argo:claude-haiku-4.5` on first 4KB) → 117,501 distinct email
observations across 36K author cards.

## Battery — seven orthogonal tests, one verdict per email

For each sampled email, run all seven independently:

| ID  | Test          | What it checks                                                          |
|-----|---------------|-------------------------------------------------------------------------|
| T1  | syntactic     | RFC-lite regex, local-part ≤64, domain ≤255, no double-dot              |
| T2  | dns_mx        | Domain has at least one MX record (fallback to A)                       |
| T3  | smtp_rcpt     | EHLO + MAIL FROM + RCPT TO (probe — see CAVEAT)                         |
| T4  | osti_xref     | Local-part overlaps any author-name token in same paper                 |
| T5  | domain_cred   | Institutional / national-lab / academic / freemail / generic            |
| T6  | obfusc        | Leftover `at`/`dot`/`[at]`/Unicode obfuscation residue                  |
| T7  | multi_addr    | Detect un-split multi-email strings (`"a@x.org or b@y.org"`)            |

**Aggregate verdict** (priority order):
1. T1 fail → `syntax_bad`
2. T7 fail → `multi_addr` (extractor bug, fixable)
3. T6 fail → `obfusc`
4. T2 fail → `unreachable`
5. T3 hard 5xx → `not_deliverable` (the only meaningful T3 outcome)
6. else → `valid` + soft flags (`xref_miss`, `freemail`, `weak_tld`, `smtp_ok`)

## CAVEAT — T3 SMTP RCPT TO is a soft signal only

Modern enterprise mail systems (Outlook Protection, Proofpoint, Mimecast,
many `.edu` mailgates) **defeat callout verification by design**. Typical
failure shapes on a 100-sample from cels-rbdgx2:

```
SMTPServerDisconnected: Connection unexpectedly closed       41
SMTPServerDisconnected: Connection reset by peer             34
mail_from_454 (4.7.1 Service unavailable; Client host blocked) 15
gaierror (DNS unresolvable from probe host)                   2
Refused due to lack of security (probe IP not TLS-cleared)    1
5.7.1 Destination domain blacklisted (probe IP-blocked)       1
```

**Only `5xx on RCPT TO with a specific Mailbox-rejected-style reason`** is
meaningful — and even that's contaminated by source-IP-policy rejects
(e.g. `*.cels.anl.gov` is blacklisted by some `.edu.cn` mailgates).

**Practical rule:** for "is this email valid" judgment, weight
**T1 + T2 + T4 + T5 + T7**; treat T3 as confirmatory only when it hard-rejects
on a recognizable receiver-side address-validity message (mailbox doesn't
exist, user disabled, etc.). The 2/100 "rejects" in the OSTI sample were
**both source-side policy rejects** (UMich TLS-strictness, CUMT IP blocklist),
not bad addresses.

## Host-egress trap — outbound port 25 is firewalled from home/m1

m1 mac mini (and most ISP residential networks) block outbound port 25 to
fight spam, so every SMTP probe times out at the configured timeout. Confirm
**before scaling the battery**:

```bash
python3 -c "
import smtplib, socket
socket.setdefaulttimeout(8)
s=smtplib.SMTP('slac-mailgate.slac.stanford.edu', 25)
s.ehlo('test.local'); s.quit(); print('outbound 25 OPEN')
"
```

If that times out, run T3 via SSH to a host that has port 25 open. Verified
working on `cels-rbdgx2` (and other `cels-*` hosts):

```bash
ssh cels-rbdgx2 'python3 -c "
import smtplib, socket
socket.setdefaulttimeout(8)
s=smtplib.SMTP(\"slac-mailgate.slac.stanford.edu\", 25)
s.ehlo(\"kd9nwa.org\"); s.mail(\"kukla@kd9nwa.org\")
c,r=s.rcpt(\"claudiop@slac.stanford.edu\")
print(c, r.decode(errors=\"replace\")[:120]); s.quit()
"'
# → RCPT: 250 2.1.5 Ok
```

The full SSH-wrapped T3 implementation is in `run_tests_v2.py` of the
OSTI repo; key pattern is to embed the probe script as a one-liner with
`shlex.quote`, run via `subprocess.run(["ssh", host, "python3 -c ..."])`,
and parse a single JSON line of output.

## Extractor defect found and fixed: multi-email-string

Across 100 random observations, **3/100 were syntax-bad** — all the same
defect class. The LLM had returned multiple corresponding-author addresses
as a single string with the separator preserved:

```
"barkakatyb@ornl.gov or lokitzbs@ornl.gov"
"xiangwang.chn@gmail.com; wuz1@ornl.gov"
"tcchiang@illinois.edu; chuangtm@gate.sinica.edu.tw"
```

Corpus-wide audit (one-shot post-processor): **970 / 106,241 records (0.91%)
had the defect**, with **1,011 additional valid addresses recoverable**.

### Fix shape — splitter that runs both as in-line LLM-output sanitizer AND as one-shot corpus post-processor

```python
import re
RFC_LITE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
# Splitter covers ' or ', ' and ', ';', ',', ' / ', ' & '
SPLIT_RE = re.compile(r"(?:\s+(?:or|and)\s+|[;,]\s*|\s+/\s+|\s+&\s+)")

def split_multi_email(value):
    """Return (primary, additional_list).
    - Single clean address → (addr, [])
    - "a or b" / "a; b" / "a, b" / etc → (a, [b, ...])
    - Unparseable → (raw, []) — caller decides what to do
    """
    if not value or not isinstance(value, str):
        return (None if not value else value), []
    raw = value.strip()
    if not raw:
        return None, []
    if RFC_LITE.match(raw):
        return raw, []  # fast path
    parts = SPLIT_RE.split(raw)
    parts = [p.strip().strip("<>(),:;") for p in parts if p.strip()]
    valid = [p for p in parts if RFC_LITE.match(p)]
    if not valid:
        return raw, []
    return valid[0], valid[1:]
```

In the extractor, apply to both `corresponding_email` and each
`author.email` after the LLM returns. Attach the spilled addresses as
`additional_emails` (per-author and at record top level) so no information is
lost.

The exact strip-set `"<>(),:;"` matters — extracted strings often have
trailing punctuation from sentence context that the LLM didn't clean up.

### Coverage of the splitter

Tested against the 3 known-bad records, plus single-address (no-op),
empty string, None, and 3-address chains. Always test these five shapes
before declaring done:

```python
cases = [
    ("a@x.org or b@y.org",     ("a@x.org", ["b@y.org"])),
    ("a@x.org",                ("a@x.org", [])),
    ("",                       (None, [])),
    (None,                     (None, [])),
    ("a@x.org, b@y.org, c@z.org", ("a@x.org", ["b@y.org", "c@z.org"])),
]
```

## Deterministic-seed sampling — always pin the seed

```python
import random
rng = random.Random(42)  # PIN the seed
sample = rng.sample(pool, N)
```

Without a pinned seed, you can't reproduce the verdict-bucket results when
you re-run after a fix. Pin it once at the top of `sample_*.py` and never
change it. If you need a fresh sample to check for over-fitting, pin a
different seed and document why.

## What constitutes a "real" defect vs noise

In the 100-sample, the verdict distribution **after** the SMTP-probe
relegation:

```
valid (T1+T2+T4+T5+T7 ok):                  75
valid + xref_miss (no author-name overlap): 14
valid + weak_tld (.org/.com):                2
valid + freemail (gmail/etc from a paper):   2
syntax_bad (multi_addr):                     3
smtp_rejected (source-IP policy):            2
valid + smtp_ok + xref_miss:                 1
valid + xref_miss + freemail:                1
```

- `xref_miss` (no author-name overlap) is **not a defect** — it catches
  collaboration mailers (`auger_spokespersons@fnal.gov`), institutional
  contact aliases, and emails that simply happen not to share spelling with
  the romanized author name (very common for Chinese/Korean/Vietnamese
  authors whose paper name doesn't match their mailbox name). Treat it as
  a flag for follow-up, not a rejection.
- `weak_tld` / `freemail` are **soft signals** — many legitimate
  corresponding-author emails are gmail in physics/chem/bio papers
  (PI uses personal mail for cross-institution coordination).
- `multi_addr` and `syntax_bad` are **real defects** — fix the extractor.
- `not_deliverable` only counts when the 5xx message is mailbox-side, not
  source-IP-policy-side. In this sample, all 2 were source-IP-policy.

## Files that should land in the repo

```
extract_emails.py           # patched with split_multi_email() inline
tests/
  README.md                 # battery overview + caveats + recipe
  sample_emails.py          # deterministic-seed sampler (seed=42)
  run_tests_v2.py           # 7-test battery (SSH for T3)
  fix_split_emails.py       # one-shot corpus post-processor
  run_tests_after_fix.py    # retest harness
  results_v2.jsonl          # per-record machine output (pre-fix)
  results_after_fix.jsonl   # post-fix
  summary_v2.md             # human-readable summary
  summary_after_fix.md      # post-fix
  split_diff.jsonl          # audit log: every changed record
  .venv/                    # gitignored
.gitignore                  # exclude emails.jsonl (156MB), fulltext/, venv
```

The `.gitignore` should `!`-include the `tests/*.jsonl` and `tests/*.md`
artifacts — they're small and useful for future audits.

## Reuse — when this whole pattern applies

Any time the user asks "validate the X we extracted from Y" where X is a
typed identifier (emails, DOIs, accessions, URLs, ORCIDs, ROR IDs). The
shape is always:

1. Seven-ish independent tests, each cheap and orthogonal
2. Aggregate verdict by priority (hard fails first, soft flags last)
3. Pinned-seed random sample first, before scaling
4. Distinguish **format defects** (extractor bug, fixable) from
   **deliverability/validity** (corpus-property, not fixable by you)
5. Post-processor template that can fix the existing corpus in place,
   plus inline fix in the extractor for future runs
6. Retest on the patched corpus → prove the defect is gone

## See also

- `scripts/extract_authors_emails.py` — the extractor this validation tested
- Argo proxy `:44497` cheat sheet → `references/argo-endpoint.md`
