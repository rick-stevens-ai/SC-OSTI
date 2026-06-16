# SC-OSTI

**Scientific Corpus — OSTI**: the authoritative repository of tooling, schemas, prompts, skills, and operational docs for Rick's OSTI fulltext corpus (~280K papers, 2000–2026, Argonne + DOE labs).

## Why this exists

This corpus has been built and refined across many sessions on many hosts. Without one canonical home, the working knowledge — DB schemas, fetch strategies, extraction prompts, classifier taxonomies, the hard-won "this lab walls IP, that one doesn't" lessons — drifts and gets re-derived.

SC-OSTI is the single source of truth. Everything that touches the corpus lands here.

## Layout

```
SC-OSTI/
├── README.md                  ← this file
├── tools/                     ← runnable scripts (reconcile, fetch, build, classify)
├── schemas/
│   ├── sql/                   ← catalog.sqlite DDL, migrations
│   └── json/                  ← manifest, xCard, classifier output schemas
├── prompts/
│   ├── extract/               ← xCard extraction (data/model/agent)
│   ├── classify/              ← replication-needs, taxonomy, lab-vs-no-lab
│   └── judge/                 ← quality scoring, consensus prompts
├── skills/                    ← copy of relevant Hermes/OpenClaw skills
├── docs/
│   ├── layouts/               ← directory schemas (SG-1-8TB, Polaris, Aurora)
│   ├── runbooks/              ← step-by-step ops
│   └── state/                 ← daily corpus-state snapshots
├── analysis/                  ← notebooks + ad-hoc analyses
└── .github/                   ← CI (later)
```

## Working corpus location

- **Canonical PDFs:** `/Volumes/SG-1-8TB/osti/pdfs/<year>/<osti_id>.pdf` (99,787 files as of 2026-06-16)
- **Catalog DB:** `/Volumes/SG-1-8TB/osti/catalog/catalog.sqlite` (278,645 papers, 1.13 GB)
- **OCR output:** `/Volumes/SG-1-8TB/osti/text/<year>/<osti_id>.{md,mmd}` (TBD)
- **Logs:** `/Volumes/SG-1-8TB/osti/logs/`
- **Pipeline scripts:** `/Volumes/SG-1-8TB/osti/scripts/` (mirrored into `tools/` here)

## Current focus

1. **Gap filling** — 118,152 papers (42.4%) still missing PDFs. Strategies in `docs/runbooks/gap-filling.md`.
2. **OCR (.md + .mmd)** — Marker on Polaris for `.md`, Nougat for `.mmd` (math-heavy subset).
3. **xCards** — re-run extract-o-matic on full corpus, fill NO_SIGNALS gaps.
4. **Replication classifier** — lab-work-needed vs no-lab-work (LLM judge, full corpus).
5. **NO-DOI characterization** — 63,476 papers lack DOI; understand what they are before designing gap fill for them.

## Update discipline

Kukla updates SC-OSTI at least daily as state changes. Per-day state snapshot in `docs/state/YYYY-MM-DD.md`. Schema changes get a migration in `schemas/sql/migrations/`. New tools land in `tools/` with a docstring.

## Provenance

- **Owner:** Rick Stevens
- **Maintained by:** Kukla (Hermes agent, m1-mac-mini)
- **Initial check-in:** Ollie (OpenClaw, cherryrd) — owns the GitHub push
- **Created:** 2026-06-16
