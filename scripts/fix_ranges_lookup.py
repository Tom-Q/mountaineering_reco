"""
Patch ranges_lookup.json in-place:
  - Replace float NaN values with None
  - Filter "nan" strings from local_names
  - Apply ancestry_en fallback when all name fields are empty

No geopandas needed — operates purely on the existing JSON.

Usage:
  python scripts/fix_ranges_lookup.py
"""

import json
import math
import re
from pathlib import Path

LOOKUP_PATH = Path(__file__).parent.parent / "data" / "ranges_lookup.json"


def _is_nan(val) -> bool:
    return isinstance(val, float) and math.isnan(val)


def _clean(val):
    return None if _is_nan(val) or not val else val


def _ancestry_fallback(ancestry_en: str | None) -> str | None:
    if not ancestry_en or not isinstance(ancestry_en, str):
        return None
    last = ancestry_en.strip().rsplit(" > ", 1)[-1].strip()
    last = re.sub(r"\s*\([a-z]{2,3}\)\s*$", "", last).strip()
    last = last.rstrip("*").strip()
    return last or None


def fix(lookup: dict) -> tuple[dict, int, int, int]:
    nan_fixed = 0
    local_fixed = 0
    fallback_applied = 0

    for entry in lookup.values():
        # Fix NaN name fields
        for field in ("name_en", "name_fr", "name_de", "ancestry_ids", "ancestry_en"):
            if _is_nan(entry.get(field)):
                entry[field] = None
                nan_fixed += 1

        # Filter "nan" strings from local_names
        before = entry.get("local_names") or []
        after = [n for n in before if n and n.lower() != "nan"]
        if len(after) != len(before):
            entry["local_names"] = after
            local_fixed += len(before) - len(after)

        # Apply ancestry fallback when no name exists at all
        if not entry.get("name_en") and not entry.get("name_fr") and \
           not entry.get("name_de") and not entry.get("local_names"):
            fallback = _ancestry_fallback(entry.get("ancestry_en"))
            if fallback:
                entry["name_en"] = fallback
                fallback_applied += 1

    return lookup, nan_fixed, local_fixed, fallback_applied


def main() -> None:
    print(f"Loading {LOOKUP_PATH}…")
    # parse_constant handles NaN literals that Python's json writer emits
    raw = LOOKUP_PATH.read_text(encoding="utf-8")
    lookup = json.loads(raw)

    lookup, nan_fixed, local_fixed, fallback_applied = fix(lookup)

    LOOKUP_PATH.write_text(json.dumps(lookup, ensure_ascii=False, indent=2))
    print(f"  NaN fields replaced:      {nan_fixed}")
    print(f"  'nan' local_names removed: {local_fixed}")
    print(f"  Ancestry fallbacks applied: {fallback_applied}")
    print(f"  Written → {LOOKUP_PATH}")


if __name__ == "__main__":
    main()
