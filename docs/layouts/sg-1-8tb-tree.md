# SG-1-8TB directory layout

USB external HDD on m1-mac-mini. Case-sensitive HFS+ Journaled. 8.0 TB raw.

```
/Volumes/SG-1-8TB/
├── osti/                          ← canonical working tree
│   ├── pdfs/<year>/<osti_id>.pdf  ← 99,787 PDFs, 29 year buckets (2000–2026 + unknown)
│   ├── text/<year>/<osti_id>.md   ← Marker output (TBD)
│   ├── text/<year>/<osti_id>.mmd  ← Nougat output (TBD)
│   ├── catalog/
│   │   └── catalog.sqlite         ← single-source-of-truth DB (1.13 GB)
│   ├── manifests/                 ← stage/job manifests
│   ├── logs/                      ← reconcile + fetch worker logs
│   ├── scripts/                   ← runner scripts (mirrored to SC-OSTI/tools/)
│   ├── _staging/                  ← scratch
│   └── _archive/                  ← snapshots before destructive ops
├── _legacy/                       ← pre-rationalize OSTI dirs (preserved for fallback)
│   ├── osti_fulltext/
│   ├── osti_fulltext_v2/
│   ├── osti_fulltext_unpay/
│   ├── osti_fulltext_v2_md/
│   ├── osti_recovery_2026-06-09/
│   └── osti_probe/
├── BV-BRC-cites/                  ← unrelated corpus
├── Dropbox/                       ← unrelated
├── Ozan_PARSED_PDFS/              ← unrelated
├── argonium_mcqa/                 ← unrelated
└── misc/                          ← unrelated
```

## Capacity (2026-06-16)
- 1.5 TB used / 5.8 TB free / 21% capacity
- ~321 GB in `osti/pdfs/` (canonical)
- ~600 GB in `_legacy/` (deduplicated against canonical; can be pruned once OCR complete)

## I/O baseline
- Sequential write: 134 MB/s
- Sequential read: 197 MB/s
- Random 4K read F_NOCACHE: 119 IOPS / 477 KB/s
- Small-file create: 8,911 IOPS (buffered)

Adequate for OCR bulk reads; won't bottleneck Polaris/Aurora staging via rsync.

## Lifecycle

Cherry6TB (legacy 6TB external) is being erased today. SG-1-8TB is the new working root.
All `/Volumes/Cherry6TB/...` paths in older skills/docs should rewrite to `/Volumes/SG-1-8TB/osti/...`.
