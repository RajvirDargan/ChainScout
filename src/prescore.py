"""Cheap deterministic gates, applied before the scorer reads anything.

Rejects are hard (never scored). Flags are advisory - the scorer decides what they cost.
"""

from __future__ import annotations

import re

SALARY_MONTH = re.compile(
    r"(?:€|eur\s?)\s?([\d.]{3,7})(?:\s?[-–tot]{1,3}\s?€?\s?([\d.]{3,7}))?\s*"
    r"(?:per\s+)?(?:p/?m|maand|month|monthly|bruto per maand)",
    re.I,
)
SALARY_YEAR = re.compile(
    r"(?:€|eur\s?)\s?([\d.]{4,9})(?:\s?[-–to]{1,3}\s?€?\s?([\d.]{4,9}))?\s*"
    r"(?:per\s+)?(?:p/?[jy]|jaar|year|annum|annually)",
    re.I,
)
# An experience word must sit on EITHER side of the number. Requiring it only
# afterwards missed "Experience: Minimum 8 years" and a bare "5+ years" bullet, which
# is how 5-plus-year postings kept reaching the digest. The negative lookahead still
# excludes "must be at least 18 years of age", which is an age bar, not a seniority one.
_EXP_WORDS = (r"experience|ervaring|werkervaring|erfahrung|background|in a similar|"
              r"relevant|professional|working|track record|expertise|minimum|minimaal|"
              r"at least|requirement|required|seniority")
YEARS_REQ = re.compile(
    r"(?:(?<=\W)|^)"
    r"(?:(?P<before>[^.;\n]{0,60}?)\b(?:" + _EXP_WORDS + r")\b[^.;\n]{0,40}?)?"
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:[-\u2013]|to|tot)?\s*(?:\d{1,2})?\s*"
    r"(?:year|jaar|jahre|jr)s?\b"
    r"(?!\s+of\s+age)"
    r"(?:(?=[^.;\n]{0,60}\b(?:" + _EXP_WORDS + r")\b))?",
    re.I,
)
# A number with no experience word on either side is not, on its own, an experience bar.
YEARS_CONTEXT = re.compile(
    r"[^.;\n]{0,70}\b(?:" + _EXP_WORDS + r")\b[^.;\n]{0,70}", re.I)
# "5+ years" / "5 plus years" — self-evidently a requirement, keyword or not.
PLUS_YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)\s*(?:[-\u2013]|to|tot)?\s*(?:\d{1,2})?\s*"
    r"(?:year|jaar|jahre|jr)s?\b(?!\s+of\s+age)", re.I)
BARE_YEARS = re.compile(
    r"(\d{1,2})\s*(?:[-\u2013]|to|tot)?\s*(?:\d{1,2})?\s*"
    r"(?:year|jaar|jahre|jr)s?\b(?!\s+of\s+age)", re.I)

# Per-geography gross monthly salary floors now come from `salary_floor_month` on each
# geography in config.yaml, so a new country is a config change rather than a code one.
# A geography with no floor (Germany's sec. 18b has no statutory minimum under 45;
# Poland/UAE/India have none that binds) skips the check entirely rather than
# defaulting to zero, which would silently mark every posting as "clears the floor".


def _to_float(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = raw.replace(".", "").replace(",", ".").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None
    return val if val > 0 else None


def parse_salary(text: str) -> dict:
    """Best-effort monthly-EUR reading. Returns {} when nothing parses."""
    m = SALARY_MONTH.search(text or "")
    if m:
        lo, hi = _to_float(m.group(1)), _to_float(m.group(2))
        if lo:
            return {"monthly_min": lo, "monthly_max": hi or lo, "basis": "month",
                    "raw": m.group(0).strip()}
    y = SALARY_YEAR.search(text or "")
    if y:
        lo, hi = _to_float(y.group(1)), _to_float(y.group(2))
        if lo and lo > 12000:
            return {"monthly_min": round(lo / 12, 0),
                    "monthly_max": round((hi or lo) / 12, 0),
                    "basis": "year", "raw": y.group(0).strip()}
    return {}


def max_years_required(text: str) -> int | None:
    """Highest plausible years-of-experience bar stated in the text.

    A range ("3-5 years") reports its LOWER bound: the minimum is the real bar, and
    the top of a range is aspiration. "5+ years" reports 5.
    """
    text = text or ""
    hits = []

    # "5+ years" carries its own meaning — the plus IS the requirement, so it needs no
    # nearby keyword. This is the form that kept slipping through as a bare bullet.
    for m in PLUS_YEARS.finditer(text):
        n = int(m.group(1))
        if 0 < n <= 25:
            hits.append(n)

    # A bare "N years" is ambiguous (tenure, company age, contract length), so it only
    # counts as a bar when an experience word sits nearby.
    for window in YEARS_CONTEXT.finditer(text):
        for m in BARE_YEARS.finditer(window.group(0)):
            n = int(m.group(1))
            if 0 < n <= 25:
                hits.append(n)

    return max(hits) if hits else None


def _mentions(term: str, title: str) -> bool:
    """Whole-word match. Substring matching wrongly rejected 'Logistics Engineer
    Disturbance Management' and would also catch 'Lead Time Analyst'."""
    return re.search(rf"(?<![a-z]){re.escape(term.strip().lower())}(?![a-z])",
                     title, re.I) is not None


def seniority_reject(title: str, rules: dict) -> str | None:
    """Title-based seniority gate. Returns a reject reason, or None to keep.

    Two tiers, because a flat "manager" ban also kills "Management Trainee" — exactly
    the kind of graduate scheme a junior candidate should see. Domain phrases that
    merely contain a seniority word ("disturbance management", "lead time") are
    exempted outright: an over-rejection is invisible to the candidate, which makes
    it worse than a low score.
    """
    t = (title or "").lower()

    for phrase in rules.get("seniority_false_positives", []):
        if phrase.lower() in t:
            # Blank the phrase so its words cannot trip the checks below.
            t = t.replace(phrase.lower(), " ")

    for term in rules.get("reject_seniority_always", []):
        if _mentions(term, t):
            return f"seniority:{term.strip()}"

    if not any(_mentions(j, t) for j in rules.get("junior_markers", [])):
        for term in rules.get("reject_seniority_unless_junior", []):
            if _mentions(term, t):
                return f"seniority:{term.strip()}"
    return None


def screen(job: dict, rules: dict) -> tuple[bool, str, list[str]]:
    """Returns (keep, reject_reason, flags)."""
    title = (job.get("title") or "").lower()
    body = (job.get("body") or "").lower()
    flags: list[str] = []

    for term in rules.get("reject_title_terms", []):
        if term in title:
            return False, f"title_term:{term.strip()}", flags

    senior = seniority_reject(title, rules)
    if senior:
        return False, senior, flags

    if len(body) < rules.get("min_body_chars", 300):
        return False, "body_too_short", flags

    for term in rules.get("flag_body_terms", []):
        if term in body:
            flags.append(term)

    # The candidate's ceiling is `max_years_experience`. Anything above that is a hard
    # reject, not a flag — they cannot apply to it, so it should never reach the digest.
    ceiling = int(rules.get("max_years_experience", 3))
    yrs = max_years_required(body) or max_years_required(job.get("title") or "")
    if yrs is not None:
        if yrs > ceiling:
            return False, f"requires_{yrs}_years", flags
        if yrs == ceiling:
            flags.append(f"asks {yrs} years - at the candidate's ceiling")

    sal = parse_salary(body) or parse_salary(job.get("salary_text") or "")
    if sal:
        job["salary_parsed"] = sal
        floor = rules.get("salary_floor_month")
        label = rules.get("salary_floor_label") or "permit"
        if floor:
            if sal["monthly_max"] < floor:
                flags.append(
                    f"salary ~EUR{int(sal['monthly_max'])}/mo is BELOW the "
                    f"EUR{int(floor)} {label} floor - cannot sponsor"
                )
            else:
                flags.append(f"salary {sal['raw']} clears the {label} floor")

    return True, "", flags
