# Argo proxy endpoint quick reference

CELS Argo wrapper on cherryrd, exposed via Tailscale.

## Connection

| Item | Value |
|---|---|
| Base URL | `http://<tailnet-aggregator>:44497/v1` |
| Auth header | `Authorization: Bearer stevens` |
| Path style | OpenAI-compatible (`/chat/completions`, `/models`) |

## List available models

```bash
curl -s http://<tailnet-aggregator>:44497/v1/models | \
  python3 -c "import json,sys; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

As of 2026-05: ~30 models — full GPT-4/4.1/5 series, o1/o3/o4-mini, Claude
4.1–4.7 (opus/sonnet/haiku), Gemini 2.5 (pro/flash).

## Model choice cheat-sheet for structured extraction

| Model | When | Cost | Speed |
|---|---|---|---|
| `argo:claude-haiku-4.5` | Trivial extraction, regex-like patterns. Sometimes struggles with strict JSON. | cheapest | fastest |
| `argo:claude-sonnet-4.6` | **Default.** JSON-output extraction, scoring, summarization. Best price/perf. | cheap | ~0.3-2s |
| `argo:claude-opus-4.7` | Subtle judgment, multi-step reasoning, when sonnet gives mixed results. | expensive | ~2-10s |
| `argo:gpt-5-mini`, `argo:o4-mini` | Alternative when claude rate-limits or for comparison. | mid | mid |

## One-shot chat completion (bash)

```bash
curl -sS http://<tailnet-aggregator>:44497/v1/chat/completions \
  -H "Authorization: Bearer stevens" \
  -H "Content-Type: application/json" \
  -d '{"model":"argo:claude-sonnet-4.6","messages":[{"role":"user","content":"hi"}],"max_tokens":50}' \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['choices'][0]['message']['content'])"
```

## Parallel calls from Python

Use `concurrent.futures.ThreadPoolExecutor(max_workers=8)`. Argo handles 8 way
fine for sonnet 4.6 on the CELS judge endpoints; bump to 16 if you're
impatient. See `scripts/llm_extract.py` for the full pattern.

## Pitfalls

- **502s during peak hours.** Wrap calls in a 1-retry helper; usually clears
  on retry.
- **Argo strips `reasoning` field on reasoning models** (o1/o3/o4/oss120).
  If content is null, the reasoning was consumed silently — bump max_tokens
  to 800+ and use a terse prompt to avoid this on o-series models.
- **No streaming through the proxy.** Use blocking requests; streaming is not
  forwarded.
- **Don't run a parallel polling cron in the same minute as another job.**
  CELS rate-limit is per-source-IP per-minute, which all looks like cherryrd
  to Argo.
