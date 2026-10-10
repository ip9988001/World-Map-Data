# World-map-data

**A consolidated, cleaned and traceable open address-data commons for the world.**

> 世界地图公开数据库 · 归集整理总仓库
> [中文说明 →](README.zh-CN.md)

---

## Why this exists

Address data is essential public infrastructure. Yet across most of the world it is:

- **scattered** across thousands of county, city, provincial and national portals;
- **inconsistent** in schema, encoding, language and granularity;
- **expensive** when obtained commercially;
- **hard to audit** — you rarely know where a record actually came from.

At the same time, developers, QA engineers, researchers and students constantly need *plausible test addresses*. When no trustworthy open test data exists, the path of least resistance is to sample **real people's home addresses** — which leaks personal data and causes real harm to real households.

**This project exists to break that trade-off.**

We gather *publicly available* address data, normalise it, preserve its provenance, and publish it in an open, machine-readable form — so that anyone can build software, tests and research against real-world address structures **without touching a single real household's private information**.

Free, open, attributable, and auditable. That is the point.

---

## What this is

- **A data commons.** Aggregated from public, traceable sources and standardised into a single schema.
- **A provenance-preserving pipeline.** Every record retains its source and licence.
- **Real structure, not invention.** Entries come from address records that genuinely exist.

## What this is not

- **Not personal data.** No names, no phone numbers, no identities, no personal attributes.
- **Not a fraud tool.** Not for impersonation, credit fraud, or evading verification.
- **Not a commercial validation service.** Use an official postal provider for that.

---

## Two iron rules

### 1. Data integrity — we never fabricate

- Only address records that **actually exist** are admitted.
- Where a source publishes only **ranges** (e.g. `100–198`), we keep only the **endpoints of each side**. Mid-range numbers are discarded, never synthesised.
- Addresses without a house number are excluded entirely.
- **Less data is better than fake data.**

### 2. Ethical collection — we never burden a public service

- Every collector honours the source's published limits. If an API permits 1,000 records per call, we take **900**.
- Rate limits, concurrency ceilings and exponential back-off are built in. We crawl slowly rather than get blocked or strain a public endpoint.
- Every request is logged. Every source's licence and required attribution are recorded alongside the data.

---

## Sources

All upstream sources are **publicly and freely accessible**. We do not bypass authentication or paywalls.

| Layer | Source | Note |
|---|---|---|
| Aggregator | **OpenAddresses** | Source definitions published on GitHub |
| Aggregator | **OpenStreetMap** | Geofabrik extracts / Overpass API |
| Discovery | **ArcGIS Online / Hub** | Entry point to county & city feature services |
| Discovery | **Socrata** | Entry point to municipal open-data portals |
| National (US) | **US Census TIGER/Line** | Full coverage, public domain |
| Sub-national | State / county / city open-data portals | Per-source adapters |
| International | JP · FR · AU · NL · UK · KR · TW · HK · SG · NZ | Official open address registries |

Per-source licence and attribution live in the [`license`](data/源总表.csv) column of the source table.

---

## Repository layout

```
docs/        Survey and design documents
data/        Normalised source tables (CSV)
collectors/  Collection adapters (ArcGIS / file download) + normalisation + merge
scripts/     Parsing, reporting and probing utilities
.github/     Collection workflows
raw/         Scratch downloads (git-ignored, never committed)
```

---

## How this repository is used

**This repository is the pipeline and the provenance record — not the data warehouse.**

| Where | What lives there |
|---|---|
| **This repository** | collection adapters, source definitions, normalisation rules, source tables, manifests |
| **A dedicated data server (self-hosted)** | the cleaned, merged address datasets, published and served from object storage |

In short: **this repo produces the data; our own data server serves it.**

Downstream applications never read the dataset from here. They read it from the data server through a single configurable base URL, so the storage backend can change without touching application code.

### Why datasets are not committed here

Large address datasets are **intentionally never committed to this repository**:

- git keeps every version of every file forever, so dataset updates would grow the
  repository without bound;
- cloning, CI checkout and backup all degrade as history grows;
- publishing volumes of data from a source-code host is not what it is designed for.

So the split is deliberate:

```
collect  →  normalise  →  merge  →  publish to data server  →  applications fetch by URL
   ↑                                                                    ↑
 this repository                                            DATA_BASE_URL (configurable)
```

The only data artefacts kept here are small, human-reviewable tables: the source inventory,
per-source availability results, and the manifests that describe what was published elsewhere.

### Operating the pipeline

1. Keep the source inventory (`data/源总表.csv`) up to date — it is the single source of truth
   for what is collected, from where, and under which licence.
2. Run a collection job (see the workflows) — it respects each source's published limits.
3. Review the normalised output and the merge report.
4. Publish the merged dataset to the data server and record a manifest entry here.
5. Point applications at the data server; never at this repository.

---

## Status

- [x] Global source survey (all endpoints HTTP-verified)
- [x] US source table — **1,923 sources / 1,933 address layers**
- [x] Collection-channel analysis — **ArcGIS 83.2% · file download 12.9% · CSV 3.9%**
- [x] Availability sampling — 121 sources → **73% reachable**; 64 sources yielded **32.1 million** real address records
- [x] **ArcGIS adapter + file-download adapter** (with politeness: server limit × 0.9)
- [x] First pilot run — **100% source success on Actions**; 5.57M records in a single state source
- [x] **Full sweep of all 1,933 address layers** — 77.1% reachable, **141.4 million** real address points, **50 states covered**
- [ ] State-sharded full collection → merge & dedupe
- [ ] Full collection, deduplication and merge

---

## Licensing & attribution

Upstream licences differ. Three families dominate:

- **Public domain** (e.g. US Census TIGER/Line) — unrestricted.
- **CC-BY / attribution required** (e.g. GeoNames, most municipal datasets).
- **ODbL** (OpenStreetMap) — attribution **and** share-alike on derivative databases.

We preserve this information per source. **If you redistribute derived data, you inherit the obligations of the upstream licence.**

---

## Responsible use

This dataset is intended for: software development, automated testing, QA, education, research, geospatial engineering, and privacy-preserving data practice.

**Explicitly prohibited:** identity fraud, credit or benefits fraud, impersonation, circumventing regulatory or platform controls, or any use causing harm to a real person or organisation.

Nothing in this repository constitutes real personal data, and no contributor may introduce any.

---

## Contributing

Issues and pull requests are welcome — especially:

- new public sources (with licence and attribution);
- corrections where a source has changed or gone offline;
- adapter code that respects the collection rules above.

*If you find that a source has changed its terms, please open an issue rather than working around it.*
