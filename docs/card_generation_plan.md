# Card Generation Plan (Phase 4.5)

## Card schema (new columns in each source DB)

| Field | Source | LLM? |
|---|---|---|
| `source_db` | hardcoded | no |
| `source_url` | existing DB field | no |
| `title` | existing DB field | no |
| `parent_title` | existing DB field (books only) | no |
| `doc_type` | LLM — JSON **array** (multiple types allowed) | yes |
| `date` | LLM parse | yes |
| `trustworthiness` | rule-based | no |
| `lat`, `lon` | existing DB | no |
| `mountain_range` | GMBA lookup | no |
| `grades` | LLM extract (JSON object) | yes |
| `language` | LLM detect | yes |
| `summary` | LLM generate | yes |
| `text_length` | `len(text)` computed | no |

**doc_type** is a JSON array — a document can have multiple types simultaneously.  
Values: `trip_report`, `route_description`, `hut`, `reference`, `other`.

**trustworthiness rules:**
- `hikr` → `"medium"`
- `summitpost` → copy `score` field (0–5 star rating)
- everything else → `"high"`

## Per-source truncation (text sent to LLM)

| DB | Send | Docs |
|---|---|---|
| hikr | First 800 chars of `full_text` | 10,700 |
| summitpost | First 1500 chars of section text | 2,313 |
| sac | Full `full_text` | 820 |
| passion_alpes | Full `full_text` | 148 |
| lemkeclimbs | Full `full_text` | 514 |
| freedom_of_hills | Full `text` | 1,215 |
| memento_ffcam | Full `text` | 205 |
| refuges | `access_desc` + `description` | 1,200 |
| **Total** | | **~17,115** |

## Cost estimate (Haiku 4.5: $1/MTok in, $5/MTok out)

- Input ~4.36M doc tokens + cached prompt prefix (~$0.34 effective) ≈ **$4.70**
- Output ~2.05M tokens × $5 ≈ **$10.25**
- **Total: ~$15 standard API, ~$7.50 with Batch API (50% discount)**

## System prompt (shared, cached)

```
You are generating structured metadata cards for a mountaineering document corpus.

Given a document excerpt, return JSON with exactly these fields:
- "doc_type": array of applicable types from [trip_report, route_description, hut, reference, other]
- "date": ISO date string if extractable (YYYY-MM-DD or YYYY-MM), else null
- "grades": object mapping discipline to grade string — keys from {alpine, rock, ice, mixed, ski, hiking}
- "language": ISO 639-1 code ("fr", "en", "de", "it", etc.)
- "summary": 1–3 sentences in English describing what this document is about

Return only valid JSON. No preamble, no explanation.
```

## Script structure (`scripts/generate_cards.py`)

- `--sample N` flag: picks N random rows across all DBs, runs synchronously, prints to stdout without writing to DB. Run with N=50 first; inspect before full batch.
- Full run: uses Anthropic Batch API (async, 50% discount), polls until complete.
- Safe to re-run: only processes rows where `summary IS NULL`.

## Dependencies
- `geopandas`, `shapely` (for GMBA lookup — see `src/mountain_ranges.py`)
- `anthropic` (already in requirements)

## GMBA data
Use full standard version (not _300): `_external/GMBA_Inventory_v2.0_standard/`  
Rationale: standard_300 collapses Alps sub-ranges; full version distinguishes Mont-Blanc massif, Écrins, Wallis Alps, etc.
