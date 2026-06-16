# Multi-modal corpus indexing — five axes over one card store

**When:** after extraction produces an atomic-card corpus (one card per entity:
contact, paper, model, dataset, person) and the consumer needs to *find* cards
multiple ways. Single-axis search (whatever the storage layer ships with) is
almost never enough at scale.

**Worked example:** OSTI contact corpus, 36,026 unique-email contact cards
loaded into a UMP markdown-card store (`~/Dropbox/XFER/ump/memory.d/*.ump.md`).
UMP's native `/ump/recall` does Jaccard token overlap only — fine for fuzzy
concept queries, fails on exact email lookup (`junlu@anl.gov` splits into 3
tokens and gets buried) and has no structured-filter API. Built a sidecar
multi-search HTTP server in ~12K bytes of Python that exposes 5 axes over
the same card store.

## The pattern

```
Cards on disk (source of truth)
    │
    ├── Native search of the store        (axis 1: whatever the store ships)
    │
    └── ump_index_builder.py
         ↓
         <project>_index.db   (SQLite: FTS5 + typed columns + multi-value field tables)
         ↓
         multisearch HTTP server
           /search/exact       — FTS5 MATCH (phrases, boolean, prefix)
           /search/regex       — Python re over indexed body text
           /search/structured  — SQL WHERE on typed columns + JOINs
           /search/semantic    — proxy to native store
           /search/hybrid      — Reciprocal Rank Fusion of the above
```

One source of truth (the card files), N orthogonal indexes built from it,
exposed behind a single HTTP surface. Each axis is one ~30-line function.

## When this pattern fires

Trigger phrases:
- "I want to do exact lookup AND fuzzy AND filter on the same store"
- "the recall is returning irrelevant matches but the structured query is exact"
- "regex AND semantic search"
- "give me ORNL battery contacts" (semantic query + structural filter — pure
  semantic misses precision, pure filter misses synonyms)

Threshold: ~5K+ atomic records OR ~3+ distinct retrieval modes needed.

## The five axes

| Axis | Backend | Best for | Latency |
|---|---|---|---|
| `exact` | SQLite FTS5 with `tokenize='unicode61 ... tokenchars '@.-_+''` | exact phrases, boolean (AND/OR/NOT), prefix (`foo*`) | <100ms on 36K cards |
| `regex` | Python `re` over indexed body | arbitrary patterns | ~400-800ms (linear scan) |
| `structured` | SQL on typed columns + JOIN on multi-value field table | filter by lab / topic / paper_count | <100ms with indexes |
| `semantic` | Native store recall (or vector store) | fuzzy concept match | depends on store |
| `hybrid` | RRF fusion `score = Σ w/(k+rank)`, k=60 | best overall recall+precision | sum of contributors |

## Schema (works for any card type)

```sql
-- typed columns: hoist the most-filtered fields out of the JSON blob
create table card (
  id text primary key,
  path text not null,         -- source file
  mtime real not null,        -- for incremental rebuild
  -- domain-specific typed columns (one per heavily-filtered field):
  primary_lab text,
  primary_name text,
  email text,
  paper_count integer,
  contact_kind text,
  -- generic:
  kind, owner, project, visibility,
  indexed_at real
);
create index idx_card_lab on card(primary_lab);
create index idx_card_email on card(email);

-- multi-value fields (labs, names, topics, affiliations): one row per (id, field, value)
-- enables `WHERE field='topics' AND value LIKE '%Fusion%'` JOINs
create table card_field (
  id text, field text, value text,
  primary key (id, field, value)
);
create index idx_field on card_field(field, value);

-- FTS5 with tokenchars that preserve identifier characters
create virtual table fts using fts5(
  id unindexed,
  text,
  tokenize = 'unicode61 remove_diacritics 2 tokenchars ''@.-_+'''
);

-- Optional: separate trigram FTS for arbitrary substring (~3x storage)
create virtual table fts_trigram using fts5(
  id unindexed, text,
  tokenize = 'trigram'
);
```

## Pitfalls hit on the OSTI build (2026-06-08)

1. **UMP frontmatter is JSON, not YAML.** First parser assumed `key: value`
   YAML and silently produced 0 structured fields out of 15,478 cards. The
   card format is `---\n{JSON record}\n---\n<markdown body>`. Always read
   one card by hand before writing the parser.

2. **List fields sometimes serialize as strings.** Same UMP corpus had
   `"labs": "[FNAL]"` (string with brackets) on some cards and
   `"labs": ["FNAL"]` (real list) on others, depending on which upstream
   step wrote them. Normalize at parse time:
   ```python
   def as_list(v):
       if v is None: return []
       if isinstance(v, list): return [str(x) for x in v]
       s = str(v).strip()
       if s.startswith("[") and s.endswith("]"):
           return [p.strip().strip('"\'') for p in s[1:-1].split(",") if p.strip()]
       return [s]
   ```

3. **FTS5 default tokenizer splits on `@.-_+`.** Without explicit
   `tokenchars '@.-_+'`, `junlu@anl.gov` becomes three tokens (`junlu`,
   `anl`, `gov`) and exact-phrase MATCH for the email returns nothing.
   This is the single most important configuration knob for any
   identifier-bearing FTS5 corpus (emails, URLs, paths, accession IDs).

4. **macOS `lsof` shows `(CLOSED)` for valid listening sockets** when no
   peer is currently connected. Wasted three tool calls chasing a "dead"
   server before realizing the actual listen was fine — `curl --max-time 3
   http://127.0.0.1:<port>/health` is the authoritative test. Same trap
   on FreeBSD; doesn't happen on Linux `lsof`.

5. **The native-store's MCP server may cache its index at startup.** UMP's
   MCP tool surface (`ump_recall`) is bound to whatever was on disk when the
   gateway started; later HTTP writes are invisible until restart. The
   sidecar multi-search server reads SQLite which reads the current
   filesystem — so it sees writes immediately. Don't rely on the native
   tool seeing fresh records; the sidecar is the live read path.

6. **`bulk_load → reindex` cycle benefits from a 10-min refresh cron** with
   mtime-based incremental rebuild. Full rebuild on 36K cards is ~6
   minutes; incremental (only changed mtime) is sub-second. The builder
   should `select id, mtime from card` first and skip files whose mtime
   matches the indexed value.

7. **Native semantic axis can be uniformly noisy.** UMP's Jaccard scoring
   gave everything ~0.45 on a 5-result smoke query — barely distinguishable.
   When the native semantic axis is weak, the hybrid axis is what makes
   the system useful (RRF combines exact + structured filters with the
   weak semantic signal and produces sharp top results). Vector-store
   swap (Qdrant / Pinecone / Weaviate) is the next step if Jaccard
   continues to dominate the bottom of the result list.

## RRF fusion (the hybrid axis)

Reciprocal Rank Fusion is the standard combiner for multi-axis IR:

```
score(doc) = Σ over axes:  weight_axis / (k + rank_of_doc_in_axis)
k = 60  # well-validated default
weights: exact=1.0, semantic=1.0, structured=1.5  # filters are precise
```

Documents that appear in multiple axes get summed contributions and rise
to the top. Documents in only one axis still appear but score lower.

The weight on structured filters (1.5) is empirical: when the user
provides explicit filters they're saying "I know what I want" — bias
the fusion toward the precision axis.

## Operational layout (this build)

| Path | Role |
|---|---|
| `~/code/osti-replication-candidates/ump_index_builder.py` | scan + parse + populate, incremental |
| `~/code/osti-replication-candidates/ump_multisearch.py` | HTTP server, 5 endpoints + /health, port 4100 |
| `~/Dropbox/XFER/ump/ump_index.db` | SQLite index (regen-from-cards-safe) |
| `~/.hermes/scripts/ump_index_refresh.sh` | cron wrapper, incremental |
| Cron `51b4ef5fb74a` | every 10 min |

The vault card at `~/Dropbox/XFER/memory-vault/infra/ump-multimodal-indexing.md`
has the full schema, API reference, and query examples.

## When this pattern does NOT apply

- **<5K cards** — single-axis is usually enough; the FTS5 setup is overhead.
- **Pure semantic workload** with no exact / filter requirements — just swap
  the native store backend to vector and skip the sidecar.
- **Heavy write throughput** (>10/s sustained writes) — incremental rebuild
  every 10 min is fine for OSTI's batch-load shape; for streaming writes,
  use a watchdog-based file observer or eat the SQLite write directly in
  the card writer.

## Verification recipe

Before declaring the multi-axis layer "working":

1. **Coverage check** — `select count(*) from card`, `from fts`, and
   `from card_field` should all be non-zero and in reasonable proportion
   (~3-5 field rows per card for a contact corpus).
2. **Distinct typed-column values** — `select count(distinct primary_lab)`
   should match the cardinality you expect (10 for SC labs, etc.).
   If 0, the parser is broken.
3. **Smoke each axis** with the queries you actually plan to run:
   - exact: `"<known-identifier>"` should return the canonical card
   - regex: `\bknown-pattern\b` should return ≥1 hit
   - structured: filter on a typed column with `paper_count_min: N`
     should return the right number of rows
   - semantic: any query string should return ≥1 result
   - hybrid: query + filter should fuse cleanly (signals.axes shows
     multiple contributors for top results)

## Deferred work (do later, not in v1)

- **MCP tool registration** — wrap each `/search/*` endpoint as an MCP tool
  alongside the native `ump_recall`. Add to Hermes `~/.hermes/config.yaml`
  under `mcp_servers`. Gateway restart picks up.
- **Vector backend swap** — replace Jaccard with QdrantStore/PineconeStore.
- **Watchdog daemon** — `watchdog.observers.Observer` for sub-second
  freshness if the 10-min cron isn't fast enough.

Last updated: 2026-06-08
