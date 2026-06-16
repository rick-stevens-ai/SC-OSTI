# OSTI / CELS fleet network discipline

Verified 2026-06-07 via direct probes. Updates: re-test annually or whenever a host moves IPs.

## OSTI endpoint behavior by source host

| Source | OSTI API `/api/v1/records` | OSTI PURL `/servlets/purl/<id>` |
|--------|----------------------------|----------------------------------|
| m1 (home) | 200 in ~190ms | 503 / 404 / flaky — DO NOT use for bulk |
| cels-rbdgx2 (<tailnet-host>) | 200 in 153ms | 200 application/pdf in 100-300ms |
| cels-rbdgx3 (<tailnet-host>) | 200 fast | 200 application/pdf, full speed |
| cels-oss120 (<cels-chicago-3>) | 200 in 164ms | 200 in 270ms |
| uicgpu (no env.sh) | 000 — NO EGRESS | 000 |
| uicgpu (after `source ~/env.sh`) | 200 in 115ms via proxy <lan-host>:3128 | 200 via proxy |

PURL discrimination from home is consistent and likely IP-reputation based. Don't fight it — use CELS.

## SSH alias hygiene

```bash
# CORRECT — Tailscale direct route, no jump
ssh cels-rbdgx2
ssh cels-rbdgx3
ssh cels-oss120

# WRONG — bare names fall through `Host *.cels.anl.gov` → HostName logins.cels.anl.gov (DEAD)
ssh rbdgx2     # hangs 30-60s then times out
ssh rbdgx3     # same

# WRONG — Rick's explicit ban
ssh -J logins.cels.anl.gov cels-rbdgx2
```

`logins.cels.anl.gov` was both currently unreachable and explicitly off-limits per Rick (2026-06-07): "Don't use logins.cels.anl.gov use the DGXs."

## Storage by host

| Host | Path | Size | Free | Notes |
|------|------|------|------|-------|
| cels-rbdgx{1,2,3} | `/rbstor/stevens` | 43TB (NFS `140.221.79.211:/radbiostor`) | 17TB | Shared across all three RadBio DGXs. Group: RadBio. mode rwx. **Primary bulk staging.** |
| cels-oss120 | `/` | tiny | ~12GB | Recon and small jobs only. |
| uicgpu | `/data` | 14TB NVMe | varies | HOT tier per env.sh policy. |
| uicgpu | `/gpustor` | 80TB HDD | varies | COLD tier. |
| uicgpu | `/` | 1.8TB NVMe | — | Avoid per env.sh policy. |
| m1 | `/Volumes/Cherry6TB/osti_fulltext` | 6TB | varies | Final consolidated store. Root catalog scan stalls — use deep paths. |

## uicgpu proxy quirk

uicgpu lives behind a network with NO direct internet egress for outbound HTTPS to public sites.

```bash
ssh uicgpu
source ~/env.sh           # exports HTTP_PROXY / HTTPS_PROXY = http://<lan-host>:3128
                          # exports NO_PROXY including 100.0.0.0/8 (Tailscale range)
curl -sI https://www.osti.gov/api/v1/records   # now 200 in ~115ms
```

Without `env.sh`, every outbound `curl` to a public URL returns 000 / connection refused. Tailscale-internal (100.x) traffic still works because NO_PROXY exempts it.

Not relevant for OSTI fetch (extra hop vs. just using cels-rbdgx2), but documented because the symptom looks like "uicgpu has no internet" which is technically wrong.

## Dropbox dashboard pointer

When you're confused about which host does what, source of truth is:

- `~/Dropbox/AI-ENVIRONMENT/CELS_ENDPOINT_MAP.md` — endpoint catalog
- `~/Dropbox/AI-ENVIRONMENT/ENV-STATUS.md` — full DGX inventory with IPs, storage, GPU counts
- `~/Dropbox/AI-ENVIRONMENT/distribute_osti_downloads.sh` — prior-art partitioned fetch script

Rick's instruction (2026-06-07): "look at Dropbox/AIEN/dashboard if you are confused." Read these BEFORE asking him where a host lives.

## Quick health probe

```bash
# From any host, before launching a bulk fetch:
for h in cels-rbdgx2 cels-rbdgx3 cels-oss120; do
  echo -n "$h: "
  ssh -o ConnectTimeout=5 "$h" 'curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://www.osti.gov/servlets/purl/1172426'
done
```

Expect `200 0.1-0.3s` from all three. Anything else = network issue, abort fetch.
