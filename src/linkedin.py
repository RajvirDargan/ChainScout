"""LinkedIn public guest job-search adapter.

Built to cover the United Arab Emirates, which nothing else free reaches: Adzuna has
no `ae` index (confirmed 404), Careerjet's API refuses connections, Bayt / GulfTalent /
NaukriGulf / Indeed AE all return 403 to a plain client, and none of the free ATS
boards carried a single Gulf role (Expeditors 533 postings, Flexport 165 — zero).

This endpoint is the public, unauthenticated one that powers LinkedIn's own
logged-out job search. It returns server-rendered cards with title, company, location
and a real URL, supports paging via `start`, and takes a freshness window via `f_TPR`.
Detail pages carry JSON-LD, so descriptions come free through extract.enrich.

No API key, but it is someone else's service: requests are paced, paged shallowly, and
metered through the quota ledger so a loop cannot hammer it.
"""

from __future__ import annotations

import html as html_mod
import re
import time
from datetime import datetime, timezone

import requests

import normalize as nz

ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_SIZE = 25          # what `start` steps by; the endpoint returns ~10 cards a call

CARD = re.compile(r"<li>(.*?)</li>", re.S)
RE_TITLE = re.compile(r'base-search-card__title">\s*(.*?)\s*</h3>', re.S)
RE_COMPANY = re.compile(
    r'base-search-card__subtitle">\s*(?:<a[^>]*>)?\s*(.*?)\s*(?:</a>)?\s*</h4>', re.S)
RE_LOCATION = re.compile(r'job-search-card__location">\s*(.*?)\s*</span>', re.S)
RE_URL = re.compile(r'href="(https://[^"?]+/jobs/view/[^"?]+)')
RE_DATE = re.compile(r'datetime="([\d-]+)"')

# f_TPR windows the endpoint accepts, in seconds.
FRESHNESS = {1: "r86400", 7: "r604800", 30: "r2592000"}


def _clean(raw: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", raw or ""))).strip()


class LinkedIn:
    def __init__(self, quota=None, log=print, delay: float = 1.5):
        self.quota = quota
        self.log = log
        self.delay = delay
        self.calls_used = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.blocked = False

    def search(self, keywords: str, location: str, days: int = 7,
               pages: int = 2) -> list[dict]:
        if self.blocked:
            return []
        out: list[dict] = []
        for page in range(pages):
            if self.quota is not None and not self.quota.check_and_reserve("linkedin", 1):
                break
            try:
                r = self.session.get(ENDPOINT, timeout=30, params={
                    "keywords": keywords, "location": location,
                    "start": page * PAGE_SIZE,
                    "f_TPR": FRESHNESS.get(days, FRESHNESS[7]),
                })
            except requests.RequestException as exc:
                self.log(f"    ! linkedin network error: {exc}")
                if self.quota is not None:
                    self.quota.refund("linkedin", 1)
                break

            if r.status_code == 429:
                # Back off for the whole run rather than retrying into a harder block.
                self.log("    ! linkedin rate limited — stopping LinkedIn for this run")
                self.blocked = True
                break
            if not r.ok:
                self.log(f"    ! linkedin HTTP {r.status_code}")
                break

            self.calls_used += 1
            cards = CARD.findall(r.text)
            if not cards:
                break
            out.extend(cards)
            if page + 1 < pages:
                time.sleep(self.delay)
        return out


def to_job(card_html: str, geo_id: str) -> dict | None:
    url_m = RE_URL.search(card_html)
    title_m = RE_TITLE.search(card_html)
    if not url_m or not title_m:
        return None

    company_m = RE_COMPANY.search(card_html)
    location_m = RE_LOCATION.search(card_html)

    title = _clean(title_m.group(1))
    company = _clean(company_m.group(1) if company_m else "")
    location = _clean(location_m.group(1) if location_m else "")
    if not title or not company:
        return None

    url = html_mod.unescape(url_m.group(1))
    posted = RE_DATE.search(card_html)

    return {
        "url": url,
        "canonical_url": nz.canonical_url(url),
        "title": title[:200],
        "company": company[:120],
        "location": location[:120],
        "salary_text": "",
        "posted_date": posted.group(1) if posted else "",
        # Cards carry no description; extract.enrich pulls it from the detail page,
        # which does expose JSON-LD.
        "body": "",
        "truncated": True,
        "source": "linkedin",
        "geo": geo_id,
        "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_queries(cfg: dict) -> list[dict]:
    """Only for geographies that opt in.

    Two per-geography overrides, both added 6 Sept 2026 after the India deep sweep:

    `linkedin_titles` — title vocabulary is not portable, exactly as it is not on Adzuna.
    India's junior market advertises by "executive" and "trainee", not "engineer".

    `linkedin_locations` — a list of CITIES rather than the single country name. LinkedIn's
    guest search weights location heavily and caps a query at roughly 20 cards, so one
    country-wide query returns a fraction of what four city-scoped ones do. The 5 Sept
    sweep found 13 of the top 18 India postings this way, none of which the country-level
    Adzuna pass had surfaced.
    """
    lcfg = cfg.get("linkedin", {}) or {}
    default_titles = lcfg.get("titles") or []
    out = []
    for geo in cfg["geographies"]:
        if geo["id"] not in (lcfg.get("geographies") or []):
            continue
        titles = geo.get("linkedin_titles") or default_titles
        locations = (geo.get("linkedin_locations")
                     or [geo.get("linkedin_location") or geo.get("label")])
        for title in titles:
            for location in locations:
                out.append({
                    "keywords": title,
                    "location": location,
                    "geo": geo["id"],
                })
    return out
