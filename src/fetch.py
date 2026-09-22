"""Source adapters: Firecrawl /v2/search, direct ATS boards, IND sponsor register."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def load_env(env_path: Path) -> dict:
    """Read KEY=value pairs from .env. The key never leaves this process."""
    env = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("FIRECRAWL_API_KEY",):
        if k in os.environ and os.environ[k]:
            env[k] = os.environ[k]
    return env


# --------------------------------------------------------------------- firecrawl

class Firecrawl:
    """Metered. Every call reserves against the quota ledger first; when the ledger
    says no, the call is skipped and the run continues on the free sources."""

    ESTIMATED_SEARCH_CREDITS = 2   # measured: 2 without scrapeOptions, 10 with

    def __init__(self, api_key: str, cfg: dict, log=print, quota=None):
        self.api_key = api_key
        self.base = cfg.get("api_base", "https://api.firecrawl.dev").rstrip("/")
        self.cfg = cfg
        self.log = log
        self.quota = quota
        self.credits_used = 0
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def search(self, query: str, location: str | None = None,
               include_domains: list[str] | None = None, tbs: str | None = None,
               limit: int | None = None) -> list[dict]:
        payload: dict = {
            "query": query,
            "limit": limit or self.cfg.get("results_per_query", 10),
            "sources": [{"type": "web"}],
        }
        if location:
            payload["location"] = location
        if include_domains:
            payload["includeDomains"] = include_domains
        if tbs:
            payload["tbs"] = tbs
        # Off by default — see config.yaml. This is what made queries cost 10 credits.
        if self.cfg.get("scrape", False):
            payload["scrapeOptions"] = {
                "formats": ["markdown", "rawHtml"],
                "onlyMainContent": True,
                "blockAds": True,
                "timeout": 25000,
            }

        est = self.ESTIMATED_SEARCH_CREDITS * (5 if self.cfg.get("scrape", False) else 1)
        if self.quota is not None and not self.quota.check_and_reserve("firecrawl", est):
            return []

        for attempt in range(3):
            try:
                r = self.session.post(
                    f"{self.base}/v2/search", json=payload,
                    timeout=self.cfg.get("timeout_seconds", 120),
                )
            except requests.RequestException as exc:
                self.log(f"    ! network error: {exc}")
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** (attempt + 2)))
                self.log(f"    ! rate limited, sleeping {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (401, 403):
                raise PermissionError(
                    f"Firecrawl rejected the API key ({r.status_code}). "
                    "Check FIRECRAWL_API_KEY in job_agent/.env"
                )
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if not r.ok:
                self.log(f"    ! HTTP {r.status_code}: {r.text[:200]}")
                return []

            data = r.json()
            actual = int(data.get("creditsUsed") or 0)
            self.credits_used += actual
            if self.quota is not None:
                self.quota.reconcile("firecrawl", est, actual)
            web = (data.get("data") or {}).get("web") or []
            return web if isinstance(web, list) else []

        self.log("    ! giving up on this query after 3 attempts")
        return []


    def scrape(self, url: str) -> str:
        """Fetch one page as markdown. Used to enrich ATS records whose board stubs
        out the description (bol.com returns a literal '<p>x</p>')."""
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
            "blockAds": True,
            "timeout": 25000,
        }
        if self.quota is not None and not self.quota.check_and_reserve("firecrawl", 1):
            return ""

        for attempt in range(2):
            try:
                r = self.session.post(f"{self.base}/v2/scrape", json=payload, timeout=60)
            except requests.RequestException:
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 5)))
                continue
            if not r.ok:
                return ""
            body = r.json()
            self.credits_used += int(body.get("creditsUsed") or 1)
            return (body.get("data") or {}).get("markdown") or ""
        return ""


def build_queries(cfg: dict, weekly: bool) -> list[dict]:
    """Cross titles x geographies into a bounded query list."""
    fc = cfg["firecrawl"]
    tbs = fc["freshness_weekly"] if weekly else fc["freshness"]
    titles = cfg["titles"]["core"] + (cfg["titles"]["adjacent"] if weekly else
                                      cfg["titles"]["adjacent"][:4])
    queries = []
    for geo in cfg["geographies"]:
        anchor = geo["extra_terms"][0]
        for title in titles:
            queries.append({
                "query": f'"{title}" {anchor} vacancy apply',
                "geo": geo["id"],
                "location": geo.get("firecrawl_location"),
                "include_domains": (geo.get("include_domains")
                                    if geo.get("use_include_domains") else None),
                "tbs": tbs,
            })
    cap = fc.get("max_queries_per_run", 40)
    # Interleave geographies so a cap never starves Dubai/India entirely.
    by_geo: dict[str, list] = {}
    for q in queries:
        by_geo.setdefault(q["geo"], []).append(q)
    interleaved = []
    idx = 0
    while len(interleaved) < len(queries):
        added = False
        for geo_id in by_geo:
            if idx < len(by_geo[geo_id]):
                interleaved.append(by_geo[geo_id][idx])
                added = True
        if not added:
            break
        idx += 1
    return interleaved[:cap]


# --------------------------------------------------------------------------- ATS

def _get_json(url: str, timeout: int = 25):
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": UA,
                                                        "Accept": "application/json"})
        if r.ok:
            return r.json()
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return None


def fetch_greenhouse(token: str) -> list[dict]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in (data or {}).get("jobs", []):
        out.append({
            "title": j.get("title", ""),
            "url": j.get("absolute_url", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "posted": j.get("updated_at", ""),
            "body": j.get("content", ""),
        })
    return out


def fetch_lever(slug: str) -> list[dict]:
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in (data or []):
        cats = j.get("categories") or {}
        out.append({
            "title": j.get("text", ""),
            "url": j.get("hostedUrl", ""),
            "location": cats.get("location", ""),
            "posted": datetime.fromtimestamp(
                (j.get("createdAt") or 0) / 1000, tz=timezone.utc).isoformat()
                if j.get("createdAt") else "",
            "body": (j.get("descriptionPlain") or "") + " " +
                    " ".join((l.get("text") or "") for l in (j.get("lists") or [])),
        })
    return out


def fetch_recruitee(slug: str) -> list[dict]:
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in (data or {}).get("offers", []):
        out.append({
            "title": j.get("title", ""),
            "url": j.get("careers_url") or j.get("careers_apply_url", ""),
            "location": ", ".join(x for x in [j.get("city"), j.get("country")] if x),
            "posted": j.get("published_at", ""),
            "body": (j.get("description") or "") + " " + (j.get("requirements") or ""),
        })
    return out


ATS_FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "recruitee": fetch_recruitee,
}


def ats_title_matches(title: str, keywords: list[str]) -> bool:
    t = (title or "").lower()
    return any(k in t for k in keywords)


# ------------------------------------------------------------- IND sponsor register

def refresh_ind_register(cfg: dict, cache: Path, log=print) -> bool:
    """Cache the public recognised-sponsor list.

    The register is served as one large inline HTML table (~12.9k rows), not as a
    downloadable file, so we parse the table directly. Best effort by design: a
    failure must never hide real jobs, it only makes sponsor status 'unknown'.
    """
    if cache.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            cache.stat().st_mtime, tz=timezone.utc)
        if age < timedelta(days=cfg.get("refresh_days", 7)):
            return True
    try:
        from lxml import html as LH

        r = requests.get(cfg["url"], timeout=90, headers={"User-Agent": UA})
        r.raise_for_status()
        doc = LH.fromstring(r.text)

        rows = []
        for table in doc.xpath("//table"):
            for tr in table.xpath(".//tr"):
                # The organisation name is a row-header <th>; only the KVK number
                # is a <td>. Selecting both keeps the pair together.
                cells = [c.text_content().strip() for c in tr.xpath("./th|./td")]
                if len(cells) >= 2 and cells[0]:
                    name = cells[0].strip().strip('"').strip()
                    kvk = re.sub(r"\D", "", cells[1])
                    if len(name) > 2 and name.lower() != "organisation":
                        rows.append((name, kvk))

        rows = list(dict.fromkeys(rows))
        if len(rows) < 1000:
            log(f"    ! IND register looked wrong ({len(rows)} rows) - not caching")
            return False

        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["organisation", "kvk"])
            w.writerows(rows)
        log(f"    IND register cached: {len(rows)} recognised sponsors")
        return True
    except Exception as exc:  # noqa: BLE001 - best effort by design
        log(f"    ! IND register refresh failed ({exc}) - sponsor status will be 'unknown'")
        return False


def load_ind_index(cache: Path) -> dict:
    """Build a first-token index so matching stays O(bucket), not O(12,900), per job."""
    if not cache.exists():
        return {}
    import normalize as nz

    index: dict[str, list[str]] = {}
    count = 0
    with cache.open() as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if not row or not row[0]:
                continue
            norm = nz.normalize_company(row[0])
            if not norm:
                continue
            count += 1
            index.setdefault(norm.split()[0], []).append(norm)
    index["__count__"] = count
    return index


# Head tokens too generic to imply a corporate group ("Nederlandse ...", "European ...").
GENERIC_HEADS = {
    "de", "het", "van", "der", "den", "the", "new", "first", "next", "one", "my",
    "nederlandse", "nederland", "dutch", "european", "euro", "international", "global",
    "royal", "koninklijke", "stichting", "vereniging", "gemeente", "universiteit",
    "university", "academisch", "medisch", "algemene", "centrale", "national",
    "smart", "digital", "data", "tech", "cloud", "green", "blue", "red",
}


def is_recognised_sponsor(company: str, index: dict, ratio: float = 0.88):
    """Returns True | "group" | False | None.

    None means 'we have no register loaded' — never 'not a sponsor'.
    "group" means a sister legal entity is registered but this exact one wasn't found:
    DSV and Rhenus each register several B.V.s, and the hiring entity may be any of them.
    """
    if not index:
        return None
    import normalize as nz

    needle = nz.normalize_company(company)
    if not needle:
        return None
    head = needle.split()[0]

    # Exact or containment on the same first token handles almost everything:
    # "Vanderlande" vs "Vanderlande Industries B.V.".
    for cand in index.get(head, []):
        if needle == cand or needle in cand or cand in needle:
            return True

    # Fuzzy only within the same first-token bucket, plus buckets whose head token
    # is itself a near-miss (spelling drift in the first word).
    buckets = list(index.get(head, []))
    if len(head) >= 4:
        for key, names in index.items():
            if key in ("__count__", head) or abs(len(key) - len(head)) > 2:
                continue
            if SequenceMatcher(None, head, key).ratio() >= 0.9:
                buckets.extend(names)
    for cand in buckets:
        if SequenceMatcher(None, needle, cand).ratio() >= ratio:
            return True

    # Distinctive brand token shared with a registered entity -> same group.
    if len(head) >= 3 and head not in GENERIC_HEADS and index.get(head):
        return "group"
    return False
