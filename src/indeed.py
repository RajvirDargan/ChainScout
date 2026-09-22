"""Indeed India — records handed over by the Claude scheduled task.

The Indeed connector only exists inside a Claude session, so discovery happens there
(task `indeed-india-daily`) and lands here as JSON. Everything after that — dedup,
prescreen, scoring, the digest, the send — is the agent's own pipeline, so an Indeed
role is scored on exactly the same rubric as every other source.

Record shape (one per posting; `description` only in the details file):
    {"job_id", "title", "company", "location", "posted", "url", "description"}
"""

from __future__ import annotations

from datetime import datetime, timezone

import normalize as nz

SOURCE = "indeed"
GEO = "india"

# Detail fetches are the scarce resource: the connector locks out after ~22 fast calls.
MAX_DETAILS = 12

# The user's stated preference: Delhi NCR first, then Mumbai, then the rest of India.
NCR = ("gurugram", "gurgaon", "delhi", "noida", "faridabad", "ghaziabad", "manesar")
MUMBAI = ("mumbai", "navi mumbai", "thane", "vashi", "powai")


def city_rank(location: str) -> int:
    loc = (location or "").lower()
    if any(c in loc for c in NCR):
        return 0
    if any(c in loc for c in MUMBAI):
        return 1
    if "remote" in loc:
        return 2
    return 3


def to_job(rec: dict) -> dict | None:
    title = (rec.get("title") or "").strip()
    company = (rec.get("company") or "").strip()
    url = (rec.get("url") or "").strip()
    if not title or not company or not url:
        return None
    body = (rec.get("description") or "").strip()
    return {
        "url": url,
        "canonical_url": nz.canonical_url(url),
        "title": title[:200],
        "company": company[:120],
        "location": (rec.get("location") or "").strip()[:120],
        "salary_text": (rec.get("salary") or "").strip()
                       if (rec.get("salary") or "").strip().upper() not in ("N/A", "NONE") else "",
        "posted_date": (rec.get("posted") or "").strip(),
        "body": body,
        "truncated": False,
        "source": SOURCE,
        "geo": GEO,
        "indeed_job_id": rec.get("job_id", ""),
        "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
