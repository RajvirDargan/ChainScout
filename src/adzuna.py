"""Adzuna discovery adapter — the replacement for Firecrawl search.

One call returns up to 50 structured postings for 1 quota unit, against a free
allowance of roughly 1,000 calls a month. Firecrawl returned 10 results for 10
credits against 1,000 a month, which is where the overspend came from.

Crucially Adzuna populates company and location, which the IND sponsor check and the
dedup fingerprint both depend on. (EURES was rejected for returning employer: null.)
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

import normalize as nz

BASE = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

# Adzuna country codes keyed by our geography ids. Probed live 2 Sept 2026 with the
# real key: de (9,526 logistics hits), be (1,133), pl (2,068), at (358) and in all
# return 200; "ae" and "ie" and "cz" all return 404 — Adzuna has no index for them.
# The Gulf and Ireland therefore come from src/linkedin.py instead.
# A geography absent from this map is simply skipped, so adding one here is what
# actually turns it on — listing it in config.yaml alone does nothing.
GEO_COUNTRY = {"nl": "nl", "de": "de", "be": "be", "pl": "pl", "india": "in"}


class Adzuna:
    def __init__(self, app_id: str, app_key: str, quota=None, log=print):
        self.app_id = app_id
        self.app_key = app_key
        self.quota = quota
        self.log = log
        self.calls_used = 0
        self.session = requests.Session()
        self.dead_countries: set[str] = set()

    def available(self) -> bool:
        return bool(self.app_id and self.app_key) and not self.app_id.startswith("your-")

    def search(self, what: str, country: str, where: str | None = None,
               max_days_old: int = 7, results: int = 50) -> list[dict]:
        """One title per call. `what` is AND-matched, which keeps a two-word title
        tight; `what_or` across several titles returned thousands of unrelated hits."""
        if not self.available() or country in self.dead_countries:
            return []
        if self.quota is not None and not self.quota.check_and_reserve("adzuna", 1):
            return []

        params = {
            "app_id": self.app_id, "app_key": self.app_key,
            "results_per_page": min(results, 50),
            "what": what, "max_days_old": max_days_old,
            "sort_by": "date", "content-type": "application/json",
        }
        if where:
            params["where"] = where

        try:
            r = self.session.get(BASE.format(country=country, page=1),
                                 params=params, timeout=45)
        except requests.RequestException as exc:
            self.log(f"    ! adzuna network error: {exc}")
            if self.quota is not None:
                self.quota.refund("adzuna", 1)
            return []

        if r.status_code == 401:
            self.log("    ! adzuna rejected the credentials — check ADZUNA_APP_ID/KEY in .env")
            self.dead_countries.add(country)
            return []
        if r.status_code == 404:
            self.log(f"    ! adzuna has no '{country}' index — skipping that geography")
            self.dead_countries.add(country)
            return []
        if not r.ok:
            self.log(f"    ! adzuna HTTP {r.status_code}: {r.text[:140]}")
            return []

        self.calls_used += 1
        try:
            return r.json().get("results") or []
        except ValueError:
            return []


def to_job(rec: dict, geo_id: str) -> dict | None:
    """Adzuna result -> the same Job shape every other source produces."""
    url = rec.get("redirect_url") or ""
    title = (rec.get("title") or "").strip()
    if not url or not title:
        return None

    company = ((rec.get("company") or {}).get("display_name") or "").strip()
    location = ((rec.get("location") or {}).get("display_name") or "").strip()
    if not company:
        return None

    lo, hi = rec.get("salary_min"), rec.get("salary_max")
    salary = ""
    if lo:
        salary = (f"EUR {int(lo):,}-{int(hi):,}/yr" if hi and hi != lo
                  else f"EUR {int(lo):,}/yr")
        if rec.get("salary_is_predicted") in ("1", 1, True):
            salary += " (Adzuna estimate, not stated by the employer)"

    return {
        "url": url,
        "canonical_url": nz.canonical_url(url),
        "title": title[:200],
        "company": company[:120],
        "location": location[:120],
        "salary_text": salary,
        "posted_date": rec.get("created") or "",
        # Adzuna hard-truncates description at 500 chars with an ellipsis, so this is
        # only a placeholder until extract.enrich fetches the real page. Kept so a
        # failed fetch still leaves something rather than nothing to score.
        "body": (rec.get("description") or "").strip(),
        "truncated": True,
        "source": "adzuna",
        "geo": geo_id,
        "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_queries(cfg: dict) -> list[dict]:
    """One call per (title x geography), priority-1 geographies getting the full list.

    A geography may override the title list entirely with `adzuna_titles`, because job
    title vocabulary is not portable. Probed live 2 Sept 2026: on the Polish index
    "logistics engineer" and "supply chain analyst" both return 0, while "logistics
    specialist" returns 9 and "order to cash" returns 43 — the shared-service-centre
    belt advertises by process name and "specialist", not "engineer"/"analyst". Running
    the default list against Poland spent 5 calls to collect 14 candidates.
    """
    acfg = cfg.get("adzuna", {})
    primary = acfg.get("titles") or []
    secondary = acfg.get("titles_secondary") or primary
    out = []
    for geo in sorted(cfg["geographies"], key=lambda g: g.get("priority", 9)):
        country = GEO_COUNTRY.get(geo["id"])
        if not country:
            continue
        titles = (geo.get("adzuna_titles")
                  or (primary if geo.get("priority", 9) == 1 else secondary))
        for title in titles:
            out.append({"what": title, "country": country, "geo": geo["id"]})
    return out
