"""
Grade matching and route scoring.

Routes from the Camptocamp API are scored against the user's search parameters.
The LLM is not involved here — all filtering is deterministic.

Scoring formula (per-route):
    P = total over-limit deviation
    N = total under-limit deviation
    score = α × (P + N) + (1 − α) × |P − N|   with α = 0.3

Skill grades (rock, ice, mixed, alpine) contribute a single net value:
  - If any discipline is at or above the user's limit: P += sum of all over-limit
    deltas; below-limit disciplines are ignored (one match frees the others).
  - If ALL disciplines are below the user's limit: N += the delta of the
    discipline closest to the limit (smallest gap), others are ignored.

Safety fields (engagement, objective risk, exposition) are scored per-field;
negative deltas are clamped to 0 (no benefit for being safer than the limit).
Equipment is also per-field but bidirectional (requiring less gear than expected
IS a penalty — you must carry more yourself).
"""

import re
import yaml
from pathlib import Path

# ---------------------------------------------------------------------------
# Grade scales
# ---------------------------------------------------------------------------

_g = yaml.safe_load((Path(__file__).parent.parent / "grade_systems.yaml").read_text())

ROCK       = [str(g) for g in _g["rock_french"]["ordered"]]
ICE        = _g["ice_wI"]["ordered"]
MIXED      = _g["mixed_m"]["ordered"]
ALPINE     = _g["alpine"]["ordered"]
ENGAGEMENT = _g["engagement"]["ordered"]
RISK       = _g["risk"]["ordered"]
EXPOSITION = _g["exposition"]["ordered"]
EQUIPMENT  = _g["equipment"]["ordered"]

ENGAGEMENT_LABELS = _g["engagement"]["labels"]
RISK_LABELS       = _g["risk"]["labels"]
EXPOSITION_LABELS = _g["exposition"]["labels"]
EQUIPMENT_LABELS  = _g["equipment"]["labels"]

# ---------------------------------------------------------------------------
# Field definitions
# (search_params_key, api_field, scale, clamp_under, default_route_val)
#
# clamp_under=True  → negative delta (route easier/safer than limit) is clamped to 0.
#   Skill disciplines (rock, ice, mixed, alpine): a route is a good match if
#     any one discipline is near the user's limit; the others being easier is fine.
#   Risk/safety fields (engagement, objective risk, exposition): being safer
#     than the limit is always acceptable.
#   Equipment: False because requiring less gear than expected IS a penalty
#     (you need to carry more yourself).
#
# default_route_val → assumed grade when the API field is None.
#                     None = treat missing as a match.
# ---------------------------------------------------------------------------

GRADE_FIELDS = [
    #                                                              clamp  route def
    ("rock_onsight",    "rock_free_rating",       ROCK,       True,  "2"),   # ungraded rock  = 2
    ("ice_max",         "ice_rating",             ICE,        True,  "WI1"), # ungraded ice   = WI1
    ("mixed_max",       "mixed_rating",           MIXED,      True,  "M2"),  # ungraded mixed = M2
    ("alpine_max",      "global_rating",          ALPINE,     True,  None),  # skip if missing
    ("engagement_max",  "engagement_rating",      ENGAGEMENT, True,  None),
    ("risk_max",        "risk_rating",            RISK,       True,  None),
    ("exposition_max",  "exposition_rock_rating", EXPOSITION, True,  None),
    ("equipment_min",   "equipment_rating",       EQUIPMENT,  False, None),
]

# Skill disciplines scored as a single net value (see module docstring).
SKILL_GRADE_KEYS = frozenset({"rock_onsight", "ice_max", "mixed_max", "alpine_max"})

# Activities that imply a grade field should be present (used for warnings).
# Rock and ice ratings are often absent even when relevant, so we only warn
# for the alpine global_rating which is more reliably filled in.
_EXPECTED_GRADES = {
    "global_rating": {"mountain_climbing", "snow_ice_mixed"},
}

# Scoring weight. α < 0.5 means the formula rewards partial compensation — a route
# that is slightly hard in one dimension and slightly easy in another scores better
# than one that is off in one direction only. α = 0 would be pure compensation
# (only the net imbalance counts); α = 1 would be pure accumulation (no compensation).
# 0.3 was chosen to lean toward compensation while still penalising routes that are
# hard across multiple dimensions.
_ALPHA = 0.3

# ---------------------------------------------------------------------------
# Rock grade normalisation
# Camptocamp uses several notations for easy grades; we collapse them all to
# the canonical French scale used in grade_systems.yaml.
#
# Equivalences (user-defined):
#   2 / II / 2a / 2b / 2c  →  "2"    (scale bottom)
#   3 / III / 3a / 3b / 3c →  "3"
#   4 (plain) / IV          →  "4c"   (hardest sub-variant)
#   5 (plain) / V           →  "5c"
#   6 (plain) / VI          →  "6c"
# ---------------------------------------------------------------------------

_ROMAN = {"I": "2", "II": "2", "III": "3", "IV": "4c", "V": "5c", "VI": "6c"}
_PLAIN_INT = {"2": "2", "3": "3", "4": "4c", "5": "5c", "6": "6c"}


def _normalise_rock(val: str | None) -> str | None:
    if val is None:
        return None
    v = str(val).strip()
    if v in _ROMAN:
        return _ROMAN[v]
    if v in _PLAIN_INT:
        return _PLAIN_INT[v]
    # Sub-grades 2a/2b/2c and 3a/3b/3c → collapse to base
    if re.fullmatch(r"[23][abc]", v):
        return v[0]
    return v


# ---------------------------------------------------------------------------
# Delta helpers
# ---------------------------------------------------------------------------

def _float_index(val: str, scale: list[str]) -> float | None:
    """
    Return the float position of a grade in its scale.

    Grades ending in '+' are treated as a half-step above the base grade rather
    than a full step, so that e.g. 6a → 6a+ → 6b spans 1.0 total steps (not 2),
    and WI3 → WI3+ → WI4 also spans 1.0 step. This also applies to equipment
    (P1 → P1+ → P2 = 1.0 step).

    Returns None if the value is not in the scale and cannot be resolved.
    """
    v = str(val)
    if v.endswith('+'):
        base = v[:-1]
        if base in scale:
            return _float_index(base, scale) + 0.5
    try:
        raw = scale.index(v)
    except ValueError:
        return None
    # Discount full-integer position by the number of '+' entries that precede it,
    # since each of those occupies a 0.5 slot rather than a 1.0 slot.
    plus_before = sum(1 for g in scale[:raw] if str(g).endswith('+'))
    return raw - plus_before * 0.5


def grade_delta(route_val: str | None, user_limit: str | None, scale: list[str]) -> float | None:
    """
    Return the signed grade distance between a route's grade and the user's limit.

    Positive = route is harder than limit.
    Negative = route is easier than limit.
    Returns 0 if route_val is None (missing grade treated as a match).
    Returns None if user_limit is None (discipline not applicable — skip field).

    Grades ending in '+' count as a half-step, not a full step (e.g. 6a to 6b = 1.0,
    not 2.0; 6a to 6a+ = 0.5).
    """
    if user_limit is None:
        return None
    if route_val is None:
        return 0.0
    ri = _float_index(str(route_val), scale)
    ui = _float_index(str(user_limit), scale)
    if ri is None or ui is None:
        return 0.0  # unrecognised grade value — treat as match
    return ri - ui


def soft_delta(route_val: float | None, user_min: float | None, user_max: float | None) -> float:
    """
    Return the signed distance outside the user's [min, max] range.

    Returns 0 if route_val is within range or None.
    Positive if above max, negative if below min.
    """
    if route_val is None:
        return 0.0
    if user_max is not None and route_val > user_max:
        return route_val - user_max
    if user_min is not None and route_val < user_min:
        return -(user_min - route_val)
    return 0.0


# ---------------------------------------------------------------------------
# Elimination
# ---------------------------------------------------------------------------

def is_eliminated(grade_deltas: dict[str, float], soft_deltas: dict[str, float]) -> bool:
    """
    Return True if the route should be excluded entirely.

    Hard rule: any grade/risk field with delta > 1 → eliminate.
    Soft rule: 4 or more fields (grade + soft) with delta > 0 → eliminate.
    Soft fields (vert, time) only trigger the soft rule, not the hard rule.
    """
    over_count = 0

    for field, delta in grade_deltas.items():
        if delta > 1:
            return True   # hard elimination
        if delta > 0:
            over_count += 1

    for delta in soft_deltas.values():
        if delta > 0:
            over_count += 1

    return over_count >= 4


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _compute_score(grade_deltas: dict[str, float],
                   soft_deltas: dict[str, float],
                   clamp_under_keys: set[str],
                   easy_penalty: float = 0.0) -> tuple[float, str]:
    """
    Compute (score, direction) using the asymmetric compensation formula.

    direction is "over" (route harder/bigger), "under" (easier/smaller), or "match".
    """
    P = 0.0  # total over-limit
    N = 0.0  # total under-limit

    # Skill grades: treated as a single net contribution (see module docstring).
    skill = {k: v for k, v in grade_deltas.items() if k in SKILL_GRADE_KEYS}
    if skill:
        over = [d for d in skill.values() if d > 0]
        under = [d for d in skill.values() if d < 0]
        if over:
            P += sum(over)      # all over-limit disciplines accumulate
        elif under:
            N += easy_penalty * -max(under)   # scaled by focus slider

    # Safety / equipment fields: per-field, respecting clamp_under.
    for field, delta in grade_deltas.items():
        if field in SKILL_GRADE_KEYS:
            continue
        eff = max(0, delta) if field in clamp_under_keys else delta
        P += max(0.0, eff)
        N += max(0.0, -eff)

    for delta in soft_deltas.values():
        P += max(0.0, delta)
        N += max(0.0, -delta)

    score = _ALPHA * (P + N) + (1 - _ALPHA) * abs(P - N)

    if score == 0:
        direction = "match"
    elif P >= N:
        direction = "over"
    else:
        direction = "under"

    return round(score, 2), direction


# ---------------------------------------------------------------------------
# Missing-data warnings
# ---------------------------------------------------------------------------

def _missing_warnings(route: dict) -> list[str]:
    """Return warning strings for grade fields that should be present but aren't."""
    activities = set(route.get("activities") or [])
    warnings = []
    for api_field, relevant_activities in _EXPECTED_GRADES.items():
        if route.get(api_field) is None and activities & relevant_activities:
            warnings.append(f"{api_field} not filled in")
    return warnings


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def rank_routes(routes: list[dict], params: dict,
                easy_penalty: float = 0.0) -> list[dict]:
    """
    Score, filter, and rank routes against the user's search parameters.

    Returns up to 10 routes sorted by match score (best first), each augmented
    with these keys:
        _score      float — lower is better
        _direction  "over" | "under" | "match"
        _deltas     {field: delta} for all evaluated fields
        _warnings   list of missing-data warning strings
    """
    clamp_under_keys = {sp_key for sp_key, _, _, clamp_under, _ in GRADE_FIELDS if clamp_under}

    scored = []
    for orig_route in routes:
        route = dict(orig_route)  # shallow copy — don't mutate the original

        # Normalise ice_rating: API returns "4", "4+" etc.; our scale uses "WI4", "WI4+"
        ice_val = route.get("ice_rating")
        if ice_val is not None and not str(ice_val).startswith("WI"):
            route["ice_rating"] = "WI" + str(ice_val)

        # Normalise rock grade: handle Roman numerals, plain integers, sub-grades
        route["rock_free_rating"] = _normalise_rock(route.get("rock_free_rating"))

        # --- Grade deltas ------------------------------------------------
        g_deltas = {}
        for sp_key, api_field, scale, _, default_val in GRADE_FIELDS:
            route_val = route.get(api_field)
            if route_val is None and default_val is not None:
                route_val = default_val
            user_val = params.get(sp_key)
            if user_val is None and default_val is not None:
                user_val = default_val
            delta = grade_delta(route_val, user_val, scale)
            if delta is not None:
                g_deltas[sp_key] = delta

        # --- Soft deltas (vert and time) ---------------------------------
        duration_h = (route.get("calculated_duration") or 0) * 24  # days → hours
        pace = params.get("pace", 1.0)
        adjusted_duration_h = (duration_h * pace) if duration_h > 0 else None

        s_deltas = {
            # height_diff_access is absent from stubs; hiking_vert is always 0 until
            # we switch to full route fetches (fetch_route).
            "hiking_vert": soft_delta(
                route.get("height_diff_access"),
                None,
                params.get("hiking_vert_max"),
            ),
            "difficulties_vert": soft_delta(
                route.get("height_diff_difficulties"),
                params.get("difficulties_vert_min"),
                params.get("difficulties_vert_max"),
            ),
            "moving_time": soft_delta(
                adjusted_duration_h,
                params.get("moving_time_min"),
                params.get("moving_time_max"),
            ),
        }

        # --- Normalise soft deltas to grade-step units -------------------
        # Vert: treat every 200m excess as ~1 grade step
        # Time: treat every 2h excess as ~1 grade step
        s_deltas_norm = {
            "hiking_vert":      s_deltas["hiking_vert"]      / 200,
            "difficulties_vert": s_deltas["difficulties_vert"] / 200,
            "moving_time":      s_deltas["moving_time"]      / 2,
        }

        if is_eliminated(g_deltas, s_deltas_norm):
            continue

        score, direction = _compute_score(g_deltas, s_deltas_norm, clamp_under_keys,
                                           easy_penalty)

        route["_score"]     = score
        route["_direction"] = direction
        route["_deltas"]    = {**g_deltas, **s_deltas_norm}
        route["_warnings"]  = _missing_warnings(route)
        scored.append(route)

    scored.sort(key=lambda r: r["_score"])
    return scored


# ---------------------------------------------------------------------------
# Colour helper (used by app.py for display)
# ---------------------------------------------------------------------------

def match_colour(score: float, direction: str) -> str:
    """Return a CSS colour string reflecting match quality and direction."""
    if direction == "match":
        return "#2ecc71"   # green
    if direction == "over":
        if score <= 0.5:  return "#f1c40f"   # yellow
        if score <= 1.5:  return "#e67e22"   # orange
        return "#e74c3c"                     # red
    # under: cool blue → violet scale
    if score <= 1:  return "#5dade2"         # slate blue
    if score <= 2:  return "#2e86c1"         # medium blue
    return "#7d3c98"                         # deep violet


def match_label(score: float, direction: str) -> str:
    """Return a short human-readable label for a match quality."""
    if direction == "match":
        return "great match"
    if direction == "over":
        if score <= 0.5:  return "a bit too hard"
        if score <= 1.5:  return "too hard"
        return "much too hard"
    # under
    if score <= 1:  return "a bit too easy"
    if score <= 2:  return "too easy"
    return "much too easy"


def delta_colour(delta: int | float | None) -> str:
    """Return a CSS colour for a single grade field delta (used in per-field display)."""
    if delta is None or delta == 0:
        return "#2ecc71"   # green — match / not evaluated
    if delta > 0:
        if delta <= 0.5:  return "#f1c40f"   # yellow — a bit over
        if delta <= 1:    return "#e67e22"   # orange — over
        return "#e74c3c"                     # red — well over (rare; normally eliminated)
    # negative: cool blue → violet scale, mirrors match_colour
    if delta >= -1:   return "#5dade2"       # slate blue
    if delta >= -2:   return "#2e86c1"       # medium blue
    return "#7d3c98"                         # deep violet


def delta_label(delta: float | None) -> str:
    """Return a short human-readable label for a per-field delta."""
    if delta is None:  return "not evaluated"
    if delta == 0:     return "at your limit"
    if delta > 0:
        if delta <= 0.5:  return "a bit above your limit"
        if delta <= 1:    return "above your limit"
        return "well above your limit"
    if delta >= -1:    return "a bit below your limit"
    if delta >= -2:    return "below your limit"
    return "well below your limit"
