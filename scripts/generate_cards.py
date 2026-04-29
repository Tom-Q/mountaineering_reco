#!/usr/bin/env python3
"""
Generate structured metadata cards for all source DBs.

Usage:
    python scripts/generate_cards.py --sample 10            # dry run, print to stdout
    python scripts/generate_cards.py --sample 10 --db hikr  # dry run on one source
    python scripts/generate_cards.py --limit 500            # sync run, 500 random rows, writes to DB
    python scripts/generate_cards.py                        # full batch run (Batch API)
    python scripts/generate_cards.py --db sac               # batch run for one source only
"""

import argparse
import json
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
LOOKUP_PATH = DATA_DIR / "ranges_lookup.json"

CARD_COLUMNS = [
    ("doc_type",        "TEXT"),
    ("date",            "TEXT"),
    ("trustworthiness", "TEXT"),
    ("mountain_range",  "TEXT"),
    ("grades",          "TEXT"),
    ("language",        "TEXT"),
    ("summary",         "TEXT"),
    ("text_length",     "INTEGER"),
    ("location_text",   "TEXT"),
]

# Minimum usable text length — skip rows below this
MIN_TEXT_LEN = 50

# Route/trip/hut sources: summary must name the entity and location
SYSTEM_PROMPT_ROUTE = """\
You are generating structured metadata cards for a mountaineering document corpus.

Given a document excerpt with a title and source, return JSON with exactly these fields:
- "doc_type": array of all applicable types — use as many as apply:
    "personal_trip_report"  first-person account of a specific ascent or outing by the author
    "route_description"     technical description of how to climb a route (grades, gear, approach, etc.)
    "hut"                   information about a mountain hut or refuge
    "manual"                instructional content: technique guide, safety procedure, gear advice
    "other"                 does not fit any of the above
- "date": ISO date string if extractable from the text (YYYY-MM-DD or YYYY-MM), else null
- "grades": object mapping discipline to grade string — keys from {alpine, rock, ice, mixed, ski, hiking} — empty object {} if none found
- "language": ISO 639-1 code of the source text ("fr", "en", "de", "it", etc.)
- "summary": 1–3 sentences in English. The first sentence MUST name the route/peak/hut and its location. Include discipline and grade if present. If the content is sparse, one sentence is sufficient — do not pad or invent details.

Example output for a SummitPost page about the Walker Spur:
{
  "doc_type": ["route_description"],
  "date": null,
  "grades": {"alpine": "ED1", "rock": "UIAA VI / 5.9 YDS"},
  "language": "en",
  "summary": "Walker Spur (Cassin Route), North Face of the Grandes Jorasses (4208m), Mont Blanc Massif, France — alpine ED1, rock to UIAA VI (5.9 YDS), 1200m. A serious multi-day undertaking approached via Montenvers and the Leschaux Hut, requiring crampons, axe, and rack. Generally one bivouac on route plus one on the descent."
}

Return only valid JSON. No preamble, no explanation."""

# Reference book sections: comprehensive topic coverage, no route name
SYSTEM_PROMPT_REFERENCE = """\
You are generating structured metadata cards for a mountaineering reference corpus.

Given a section from a mountaineering reference book, return JSON with exactly these fields:
- "doc_type": ["manual"]
- "date": null
- "grades": {}
- "language": ISO 639-1 code of the source text ("fr", "en", "de", "it", etc.)
- "garbled": true if the text is heavily garbled by OCR artifacts (stray characters, broken layout, unreadable content), false otherwise
- "summary": 1–3 sentences in English covering the main topics, techniques, or concepts in this section. Begin by naming the book and section. If the text is sparse or garbled, one sentence based on the section heading is sufficient — do not pad or invent details.

Example output for a Freedom of the Hills section on multipitch leading strategies:
{
  "doc_type": ["manual"],
  "date": null,
  "grades": {},
  "language": "en",
  "garbled": false,
  "summary": "Freedom of the Hills (10th ed.), Ch. 14 — Traditional Rock Climbing: Swinging Leads vs Block Leading. Explains the two strategies for leading on multipitch rock routes — swinging leads (trading every pitch) versus block leading (leading in groups) — with tradeoffs in fatigue, efficiency, and rope management."
}

Return only valid JSON. No preamble, no explanation."""

MODEL = "claude-haiku-4-5-20251001"
BATCH_REQUEST_LIMIT = 10_000
POLL_INTERVAL = 30
COMMIT_EVERY = 100


# ---------------------------------------------------------------------------
# Mountain range lookup
# ---------------------------------------------------------------------------

_RANGES: dict | None = None


def _load_ranges() -> dict:
    if not LOOKUP_PATH.exists():
        return {}
    with open(LOOKUP_PATH, encoding="utf-8") as f:
        return json.load(f)


def mountain_range_name(gmba_id: str | None) -> str | None:
    if not gmba_id:
        return None
    first_id = str(gmba_id).split(",")[0].strip()
    global _RANGES
    if _RANGES is None:
        _RANGES = _load_ranges()
    entry = _RANGES.get(first_id)
    if not entry:
        return None
    def _valid(v):
        return v if v and str(v).lower() != "nan" else None
    return _valid(entry.get("name_en")) or _valid(entry.get("name_fr")) or _valid(entry.get("name_de"))


# ---------------------------------------------------------------------------
# Source definitions
# ---------------------------------------------------------------------------

def _hikr_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, url, title, date_of_hike, full_text, gmba_id,
                  grade_mountaineering, grade_climbing, grade_ski, grade_hiking
           FROM reports WHERE summary IS NULL"""
    ).fetchall()


def _hikr_grades(row: sqlite3.Row) -> dict:
    mapping = [
        ("alpine",  "grade_mountaineering"),
        ("rock",    "grade_climbing"),
        ("ski",     "grade_ski"),
        ("hiking",  "grade_hiking"),
    ]
    return {k: row[col] for k, col in mapping if row[col]}


def _hikr_text(row: sqlite3.Row) -> str:
    title = row["title"] or ""
    body = (row["full_text"] or "")[:1500]
    return f"Title: {title}\nSource: hikr\n\n{body}"


def _summitpost_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT r.sp_id, r.url, r.name, r.difficulty, r.properties,
                  r.lat, r.lon, r.score, r.gmba_id,
                  GROUP_CONCAT(
                      CASE WHEN s.heading IS NOT NULL THEN s.heading || ': ' ELSE '' END
                      || COALESCE(s.body, ''),
                      '\n\n'
                  ) AS full_text
           FROM routes r
           LEFT JOIN sections s ON s.route_id = r.sp_id
           WHERE r.summary IS NULL
           GROUP BY r.sp_id"""
    ).fetchall()


def _summitpost_grades(row: sqlite3.Row) -> dict:
    props = {}
    try:
        props = json.loads(row["properties"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    g = {}
    if props.get("Rock Difficulty"):
        g["rock"] = props["Rock Difficulty"]
    if props.get("Grade"):
        g["alpine"] = props["Grade"]
    return g


def _summitpost_text(row: sqlite3.Row) -> str:
    title = row["name"] or ""
    difficulty = row["difficulty"] or ""
    props = {}
    try:
        props = json.loads(row["properties"] or "{}")
    except (json.JSONDecodeError, TypeError):
        pass
    meta_parts = [f"Title: {title}", "Source: SummitPost"]
    if difficulty:
        meta_parts.append(f"Difficulty: {difficulty}")
    for key in ("Rock Difficulty", "Season", "Time Required", "Number of Pitches"):
        if props.get(key):
            meta_parts.append(f"{key}: {props[key]}")
    body = (row["full_text"] or "")[:2500]
    return "\n".join(meta_parts) + "\n\n" + body


def _sac_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, title, grade, full_text, latitude, longitude, gmba_id FROM topos WHERE summary IS NULL"
    ).fetchall()


def _sac_grades(row: sqlite3.Row) -> dict:
    g = row["grade"]
    return {"alpine": g} if g else {}


def _sac_text(row: sqlite3.Row) -> str:
    title = row["title"] or ""
    body = (row["full_text"] or "")[:2000]
    return f"Title: {title}\nSource: SAC (Swiss Alpine Club)\n\n{body}"


def _passion_alpes_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, title, full_text, gmba_id FROM topos WHERE summary IS NULL"
    ).fetchall()


def _passion_alpes_text(row: sqlite3.Row) -> str:
    title = row["title"] or ""
    body = (row["full_text"] or "")[:2000]
    return f"Title: {title}\nSource: passion-alpes.com\n\n{body}"


def _lemkeclimbs_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, title, date_of_climb, full_text, gmba_id FROM topos WHERE summary IS NULL"
    ).fetchall()


def _lemkeclimbs_text(row: sqlite3.Row) -> str:
    title = row["title"] or ""
    body = (row["full_text"] or "")[:2000]
    return f"Title: {title}\nSource: lemkeclimbs.com\n\n{body}"


def _freedom_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, part, chapter, section, text FROM sections WHERE summary IS NULL"
    ).fetchall()


def _freedom_text(row: sqlite3.Row) -> str:
    return (
        f"Book: Freedom of the Hills (10th edition)\n"
        f"Part: {row['part'] or '—'}  Chapter: {row['chapter'] or '—'}  Section: {row['section'] or '—'}\n\n"
        f"{row['text'] or ''}"
    )


def _memento_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, major_section, chapter, section, text FROM sections WHERE summary IS NULL"
    ).fetchall()


def _memento_text(row: sqlite3.Row) -> str:
    return (
        f"Book: Mémento FFCAM / UIAA (French mountaineering reference)\n"
        f"Major section: {row['major_section'] or '—'}  Chapter: {row['chapter'] or '—'}  Section: {row['section'] or '—'}\n\n"
        f"{row['text'] or ''}"
    )


def _refuges_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, url, name, lat, lon, gmba_id, access_desc, description FROM huts WHERE summary IS NULL"
    ).fetchall()


def _refuges_text(row: sqlite3.Row) -> str:
    name = row["name"] or ""
    parts = [f"Title: {name}\nSource: refuges.info\n"]
    if row["access_desc"]:
        parts.append(row["access_desc"])
    if row["description"]:
        parts.append(row["description"])
    return "\n\n".join(p for p in parts if p)


def _trust_summitpost(row: sqlite3.Row) -> str:
    score = row["score"]
    if not score:
        return "0.50"
    if score <= 5:
        return f"{score / 5:.2f}"
    return f"{score / 100:.2f}"


SOURCES: list[dict] = [
    {
        "name":       "hikr",
        "db":         "hikr.db",
        "table":      "reports",
        "pk":         "id",
        "rows_fn":    _hikr_rows,
        "text_fn":    _hikr_text,
        "trust_fn":   lambda row: "0.70",
        "grades_fn":  _hikr_grades,
        "lat_col":    None,
        "lon_col":    None,
        "date_col":   "date_of_hike",
        "reference":  False,
    },
    {
        "name":       "summitpost",
        "db":         "summitpost.db",
        "table":      "routes",
        "pk":         "sp_id",
        "rows_fn":    _summitpost_rows,
        "text_fn":    _summitpost_text,
        "trust_fn":   _trust_summitpost,
        "grades_fn":  _summitpost_grades,
        "lat_col":    "lat",
        "lon_col":    "lon",
        "date_col":   None,
        "reference":  False,
    },
    {
        "name":       "sac",
        "db":         "sac.db",
        "table":      "topos",
        "pk":         "id",
        "rows_fn":    _sac_rows,
        "text_fn":    _sac_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  _sac_grades,
        "lat_col":    "latitude",
        "lon_col":    "longitude",
        "date_col":   None,
        "reference":  False,
    },
    {
        "name":       "passion_alpes",
        "db":         "passion_alpes.db",
        "table":      "topos",
        "pk":         "id",
        "rows_fn":    _passion_alpes_rows,
        "text_fn":    _passion_alpes_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  None,
        "lat_col":    None,
        "lon_col":    None,
        "date_col":   None,
        "reference":  False,
    },
    {
        "name":       "lemkeclimbs",
        "db":         "lemkeclimbs.db",
        "table":      "topos",
        "pk":         "id",
        "rows_fn":    _lemkeclimbs_rows,
        "text_fn":    _lemkeclimbs_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  None,
        "lat_col":    None,
        "lon_col":    None,
        "date_col":   "date_of_climb",
        "reference":  False,
    },
    {
        "name":       "freedom_of_hills",
        "db":         "freedom_of_the_hills.db",
        "table":      "sections",
        "pk":         "id",
        "rows_fn":    _freedom_rows,
        "text_fn":    _freedom_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  None,
        "lat_col":    None,
        "lon_col":    None,
        "date_col":   None,
        "reference":  True,
    },
    {
        "name":       "memento_ffcam",
        "db":         "memento_ffcam.db",
        "table":      "sections",
        "pk":         "id",
        "rows_fn":    _memento_rows,
        "text_fn":    _memento_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  None,
        "lat_col":    None,
        "lon_col":    None,
        "date_col":   None,
        "reference":  True,
    },
    {
        "name":       "refuges",
        "db":         "refuges.db",
        "table":      "huts",
        "pk":         "id",
        "rows_fn":    _refuges_rows,
        "text_fn":    _refuges_text,
        "trust_fn":   lambda row: "1.00",
        "grades_fn":  None,
        "lat_col":    "lat",
        "lon_col":    "lon",
        "date_col":   None,
        "reference":  False,
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_llm_json(text: str) -> dict:
    """Parse JSON from LLM response, stripping markdown code fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]  # drop opening fence line
        text = text.rsplit("```", 1)[0]  # drop closing fence
    return json.loads(text.strip())


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def add_card_columns(db_path: Path, table: str) -> None:
    conn = sqlite3.connect(db_path)
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, typ in CARD_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Card assembly
# ---------------------------------------------------------------------------

def _col(row: sqlite3.Row, col: str) -> Any:
    try:
        return row[col]
    except IndexError:
        return None


def _source_text_length(row: sqlite3.Row, src: dict) -> int:
    """Length of the original source text, before any truncation."""
    if src["name"] == "summitpost":
        return len(row["full_text"] or "")
    if src["name"] in ("hikr", "lemkeclimbs", "passion_alpes", "sac"):
        return len(row["full_text"] or "")
    if src["name"] in ("freedom_of_hills", "memento_ffcam"):
        return len(row["text"] or "")
    if src["name"] == "refuges":
        return len(row["access_desc"] or "") + len(row["description"] or "")
    return 0


def needs_location(row: sqlite3.Row, src: dict) -> bool:
    """True when we lack both coordinates and a GMBA range for this row."""
    if src["reference"]:
        return False
    if src["lat_col"] and _col(row, src["lat_col"]):
        return False
    if _col(row, "gmba_id"):
        return False
    return True


def user_message(row: sqlite3.Row, src: dict, text: str) -> str:
    if needs_location(row, src):
        return (
            text
            + '\n\n[Also provide "location_text": the most specific named location '
            "in this document — peak name, massif, or region — as a short string "
            "suitable for geocoding. Example: \"Piz Palü, Bernina Alps, Switzerland\".]"
        )
    return text


def assemble_card(row: sqlite3.Row, src: dict, llm: dict, text: str) -> dict:
    gmba_id = _col(row, "gmba_id")
    lat = _col(row, src["lat_col"]) if src["lat_col"] else None
    lon = _col(row, src["lon_col"]) if src["lon_col"] else None
    date = _col(row, src["date_col"]) if src["date_col"] else llm.get("date")
    db_grades = src["grades_fn"](row) if src["grades_fn"] else {}
    grades = db_grades if db_grades else (llm.get("grades") or {})
    if src["name"] == "memento_ffcam" and llm.get("garbled"):
        trustworthiness = "0.10"
    else:
        trustworthiness = src["trust_fn"](row)
    return {
        "pk":              row[src["pk"]],
        "source_db":       src["name"],
        "doc_type":        json.dumps(llm.get("doc_type") or []),
        "date":            date,
        "trustworthiness": trustworthiness,
        "mountain_range":  mountain_range_name(gmba_id),
        "grades":          json.dumps(grades),
        "language":        llm.get("language"),
        "summary":         llm.get("summary"),
        "text_length":     _source_text_length(row, src),
        "location_text":   llm.get("location_text") if needs_location(row, src) else None,
        "lat":             lat,
        "lon":             lon,
    }


def write_card(conn: sqlite3.Connection, src: dict, card: dict) -> None:
    conn.execute(
        f"""UPDATE {src['table']}
            SET doc_type=?, date=?, trustworthiness=?, mountain_range=?,
                grades=?, language=?, summary=?, text_length=?, location_text=?
            WHERE {src['pk']}=?""",
        (
            card["doc_type"], card["date"], card["trustworthiness"],
            card["mountain_range"], card["grades"], card["language"],
            card["summary"], card["text_length"], card["location_text"],
            card["pk"],
        ),
    )


def system_prompt(src: dict) -> str:
    return SYSTEM_PROMPT_REFERENCE if src["reference"] else SYSTEM_PROMPT_ROUTE


# ---------------------------------------------------------------------------
# Row collection
# ---------------------------------------------------------------------------

def collect_pending(
    db_filter: str | None,
    limit: int | None,
) -> tuple[list[tuple[dict, sqlite3.Row, str]], dict[str, sqlite3.Connection]]:
    pending: list[tuple[dict, sqlite3.Row, str]] = []
    connections: dict[str, sqlite3.Connection] = {}

    for src in SOURCES:
        if db_filter and src["name"] != db_filter:
            continue
        db_path = DATA_DIR / src["db"]
        if not db_path.exists():
            continue
        add_card_columns(db_path, src["table"])
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = src["rows_fn"](conn)
        skipped = 0
        for row in rows:
            if _source_text_length(row, src) < MIN_TEXT_LEN:
                skipped += 1
                continue
            text = src["text_fn"](row)
            pending.append((src, row, text))
        label = f"{src['name']}: {len(rows) - skipped} usable"
        if skipped:
            label += f" ({skipped} skipped — too short)"
        print(f"  {label}")
        connections[src["name"]] = conn

    if limit and limit < len(pending):
        pending = random.sample(pending, limit)
        print(f"  → limited to {limit} random rows")

    return pending, connections


# ---------------------------------------------------------------------------
# Sample (dry run, no DB writes)
# ---------------------------------------------------------------------------

def run_sample(n: int, db_filter: str | None, per_db: bool = False) -> None:
    client = anthropic.Anthropic()
    print("Collecting rows…")
    pending, connections = collect_pending(db_filter, limit=None)
    for conn in connections.values():
        conn.close()

    if per_db:
        by_db: dict[str, list] = {}
        for item in pending:
            by_db.setdefault(item[0]["name"], []).append(item)
        sample = []
        for rows in by_db.values():
            sample.extend(random.sample(rows, min(n, len(rows))))
        random.shuffle(sample)
    else:
        sample = random.sample(pending, min(n, len(pending)))
    print(f"\nSampling {len(sample)} rows…\n")

    for i, (src, row, text) in enumerate(sample, 1):
        pk = row[src["pk"]]
        bar = "=" * 70
        thin = "-" * 70
        print(f"\n{bar}")
        print(f"[{i}/{len(sample)}]  source={src['name']}  pk={pk}")
        print(thin)

        # Show any structured DB fields that will bypass LLM
        notes = []
        if src["date_col"] and _col(row, src["date_col"]):
            notes.append(f"date (from DB): {_col(row, src['date_col'])}")
        if src["grades_fn"]:
            db_grades = src["grades_fn"](row)
            if db_grades:
                notes.append(f"grades (from DB): {db_grades}")
        if notes:
            print("DB overrides: " + " | ".join(notes))
            print(thin)

        print(f"TEXT SENT TO LLM ({len(text)} chars):\n")
        print(text)
        print(thin)

        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=system_prompt(src),
                messages=[{"role": "user", "content": user_message(row, src, text)}],
            )
            raw = resp.content[0].text
            try:
                llm = parse_llm_json(raw)
            except (json.JSONDecodeError, ValueError):
                print(f"ERROR: could not parse LLM response:\n{raw}")
                continue
            card = assemble_card(row, src, llm, text)
            print("GENERATED CARD:\n")
            display_keys = ["doc_type", "language", "date", "grades", "trustworthiness",
                            "mountain_range", "location_text", "summary", "text_length"]
            for k in display_keys:
                print(f"  {k}: {card.get(k)}")
            if llm.get("garbled") is not None:
                print(f"  garbled: {llm['garbled']}")
        except Exception as exc:
            print(f"ERROR: {exc}")

    print(f"\n{'='*70}")


# ---------------------------------------------------------------------------
# Synchronous run (--limit)
# ---------------------------------------------------------------------------

def run_sync(db_filter: str | None, limit: int) -> None:
    client = anthropic.Anthropic()
    print("Collecting pending rows…")
    pending, connections = collect_pending(db_filter, limit=limit)

    if not pending:
        print("Nothing to process.")
        return

    print(f"\nProcessing {len(pending)} rows…\n")
    written = errors = 0

    for i, (src, row, text) in enumerate(pending, 1):
        pk = row[src["pk"]]
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=system_prompt(src),
                messages=[{"role": "user", "content": user_message(row, src, text)}],
            )
            llm = parse_llm_json(resp.content[0].text)
            card = assemble_card(row, src, llm, text)
            write_card(connections[src["name"]], src, card)
            written += 1
        except Exception as exc:
            print(f"  ERROR [{src['name']} pk={pk}]: {exc}")
            errors += 1

        if i % COMMIT_EVERY == 0:
            for conn in connections.values():
                conn.commit()
        if i % 50 == 0 or i == len(pending):
            print(f"  {i}/{len(pending)}  ({written} written, {errors} errors)")

    for conn in connections.values():
        conn.commit()
        conn.close()

    print(f"\nDone. {written} written, {errors} errors.")


# ---------------------------------------------------------------------------
# Full batch run
# ---------------------------------------------------------------------------

def _custom_id(src_name: str, pk: Any) -> str:
    return f"{src_name}--{pk}"[:64]


def run_batch(db_filter: str | None, batch_limit: int | None = None) -> None:
    client = anthropic.Anthropic()
    print("Collecting pending rows…")
    pending, connections = collect_pending(db_filter, limit=batch_limit)

    if not pending:
        print("Nothing to process.")
        return

    print(f"\nTotal: {len(pending)} rows\n")

    row_index: dict[str, tuple[dict, sqlite3.Row, str]] = {
        _custom_id(src["name"], row[src["pk"]]): (src, row, text)
        for src, row, text in pending
    }

    all_results: dict[str, str] = {}

    requests = [
        {
            "custom_id": _custom_id(src["name"], row[src["pk"]]),
            "params": {
                "model": MODEL,
                "max_tokens": 512,
                "system": system_prompt(src),
                "messages": [{"role": "user", "content": user_message(row, src, text)}],
            },
        }
        for src, row, text in pending
    ]

    for chunk_start in range(0, len(requests), BATCH_REQUEST_LIMIT):
        chunk = requests[chunk_start : chunk_start + BATCH_REQUEST_LIMIT]
        print(f"Submitting batch rows {chunk_start}–{chunk_start + len(chunk)}…")
        batch = client.messages.batches.create(requests=chunk)
        print(f"  Batch ID: {batch.id}")

        while batch.processing_status == "in_progress":
            time.sleep(POLL_INTERVAL)
            batch = client.messages.batches.retrieve(batch.id)
            c = batch.request_counts
            print(f"  {c.processing} in progress, {c.succeeded} done, {c.errored} errors")

        print("  Batch complete. Retrieving results…")
        for result in client.messages.batches.results(batch.id):
            if result.result.type == "succeeded":
                all_results[result.custom_id] = result.result.message.content[0].text
            else:
                print(f"  FAILED: {result.custom_id} — {result.result.type}")

    print(f"\nWriting {len(all_results)} results to DBs…")
    written = errors = 0

    for custom_id, raw_json in all_results.items():
        src, row, text = row_index[custom_id]
        try:
            llm = parse_llm_json(raw_json)
        except (json.JSONDecodeError, ValueError):
            print(f"  Bad JSON for {custom_id}: {raw_json[:120]}")
            errors += 1
            continue
        card = assemble_card(row, src, llm, text)
        write_card(connections[src["name"]], src, card)
        written += 1

    for conn in connections.values():
        conn.commit()
        conn.close()

    print(f"\nDone. {written} written, {errors} errors.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate metadata cards for mountaineering corpus")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="Dry run: sample N rows, print to stdout, no DB writes")
    parser.add_argument("--sample-per-db", type=int, metavar="N",
                        help="Dry run: sample N rows from EACH source DB, print to stdout")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Sync run capped at N random rows (writes to DB)")
    parser.add_argument("--batch-limit", type=int, metavar="N",
                        help="Batch run capped at N random rows (uses Batch API, writes to DB)")
    parser.add_argument("--db", metavar="NAME",
                        help="Restrict to one source (hikr, summitpost, sac, …)")
    args = parser.parse_args()

    if args.sample_per_db:
        run_sample(args.sample_per_db, db_filter=args.db, per_db=True)
    elif args.sample:
        run_sample(args.sample, db_filter=args.db)
    elif args.limit:
        run_sync(db_filter=args.db, limit=args.limit)
    elif args.batch_limit:
        run_batch(db_filter=args.db, batch_limit=args.batch_limit)
    else:
        run_batch(db_filter=args.db)


if __name__ == "__main__":
    main()
