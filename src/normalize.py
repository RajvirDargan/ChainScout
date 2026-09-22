"""Turn raw search hits and ATS records into Job dicts, and reject anything
that isn't a single job posting.

The planning searches showed aggregator index pages (nl.indeed.com/q-*-vacatures.html,
nationalevacaturebank.nl/vacatures/functie/*) dominate raw Firecrawl results, so the
rejection rules here are what keep the digest honest.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query params that are pure tracking noise. Anything matching utm_* goes too.
TRACKING_PARAMS = {
    "gh_src", "gh_jid", "trackingid", "trk", "refid", "ref", "source", "src",
    "originalsubdomain", "position", "pagenum", "eboriginal", "savedsearchid",
    "fromsearch", "sessionid", "vjk", "jsa", "from", "advn", "adid", "campaignid",
    "lipi", "licu", "trkinfo", "rsrc", "s_kwcid", "gclid", "fbclid", "mc_cid",
}

# URL shapes that are listing/index pages, never a single posting.
INDEX_URL_PATTERNS = [
    re.compile(r"/q-[^/]*-vacatures\.html", re.I),          # indeed search results
    re.compile(r"/vacatures/functie/", re.I),               # nationalevacaturebank category
    re.compile(r"/vacatures/?$", re.I),
    re.compile(r"/jobs/?$", re.I),
    re.compile(r"/jobs/search", re.I),
    re.compile(r"/job-offers/", re.I),
    re.compile(r"/zoeken", re.I),
    re.compile(r"/search\b", re.I),
    re.compile(r"/browse", re.I),
    re.compile(r"/category/", re.I),
    re.compile(r"/companies/[^/]+/?$", re.I),
    re.compile(r"/jobs-in-", re.I),
    re.compile(r"/[a-z-]+-jobs/?$", re.I),
]

# URL shapes that reliably identify one posting.
SINGLE_URL_PATTERNS = [
    # LinkedIn is /jobs/view/<slug>-<numeric-id>, so a bare \d+ misses every one.
    re.compile(r"/jobs/view/[^/?]*\d{6,}", re.I),            # linkedin
    re.compile(r"/viewjob\?", re.I),                        # indeed detail
    re.compile(r"/job/[^/]+", re.I),
    re.compile(r"/jobs/[^/]*\d{4,}", re.I),
    re.compile(r"/vacature[s]?/[^/]+-\d+", re.I),
    re.compile(r"/offer/[^/]+", re.I),
    re.compile(r"/o/[^/]+", re.I),                          # recruitee
    re.compile(r"/careers?/[^/]+/[^/]+", re.I),
    re.compile(r"myworkdayjobs\.com/.+/job/", re.I),
]

# Titles like "300+ vacatures voor X" are index pages even on a plausible URL.
INDEX_TITLE_PATTERNS = [
    re.compile(r"^\s*\d[\d.,+]*\s*(\+\s*)?(vacature|job|banen|result)", re.I),
    re.compile(r"\b\d[\d.,]*\+?\s+(vacatures|jobs|openings|banen)\b", re.I),
    re.compile(r"^(jobs|vacatures|vacancies)\s+(in|voor|bij)\b", re.I),
]

SENIORITY_NOISE = re.compile(
    r"\b(junior|jr\.?|senior|sr\.?|medior|entry[- ]level|graduate|trainee|intern|stage|"
    r"fulltime|full[- ]time|parttime|part[- ]time|m/v/x|m/v|m/w/d|f/m/d|w/m/d|h/f|"
    r"\d+\s*(uur|hours|hrs)|per\s+week|\(.*?\))\b",
    re.I,
)

CITY_HINTS = [
    "eindhoven", "amsterdam", "rotterdam", "utrecht", "den haag", "the hague",
    "tilburg", "breda", "venlo", "nijmegen", "arnhem", "groningen", "maastricht",
    "helmond", "veldhoven", "almere", "zwolle", "apeldoorn", "amersfoort",
    "dubai", "abu dhabi", "sharjah", "doha", "riyadh",
    "mumbai", "pune", "delhi", "gurgaon", "gurugram", "noida", "bangalore",
    "bengaluru", "hyderabad", "chennai",
]


def canonical_url(url: str) -> str:
    """Strip tracking noise so the same posting from two sources collapses to one key."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    kept = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    path = p.path.rstrip("/") or "/"
    netloc = p.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunparse((p.scheme or "https", netloc, path, "", urlencode(sorted(kept)), ""))


def normalize_title(title: str) -> str:
    t = SENIORITY_NOISE.sub(" ", title or "")
    t = re.sub(r"[^a-z0-9 ]+", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def normalize_company(company: str) -> str:
    c = re.sub(
        r"\b(b\.?v\.?|n\.?v\.?|gmbh|ltd|limited|inc|llc|s\.?a\.?|group|holding|"
        r"nederland|netherlands|europe|international|careers)\b",
        " ",
        (company or "").lower(),
    )
    c = re.sub(r"[^a-z0-9 ]+", " ", c)
    return re.sub(r"\s+", " ", c).strip()


def normalize_city(location: str) -> str:
    loc = (location or "").lower()
    for city in CITY_HINTS:
        if city in loc:
            return city
    return re.sub(r"[^a-z ]+", " ", loc).strip()[:40]


def fingerprint(company: str, title: str, location: str) -> str:
    key = f"{normalize_company(company)}|{normalize_title(title)}|{normalize_city(location)}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def looks_like_index(url: str, title: str = "") -> bool:
    for pat in INDEX_URL_PATTERNS:
        if pat.search(url):
            return True
    for pat in INDEX_TITLE_PATTERNS:
        if pat.search(title or ""):
            return True
    return False


def looks_like_posting(url: str) -> bool:
    return any(pat.search(url) for pat in SINGLE_URL_PATTERNS)


def extract_jsonld_jobposting(html: str) -> dict | None:
    """A JSON-LD JobPosting block is the one reliable 'this is a single job' signal."""
    if not html:
        return None
    for match in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for node in _walk_jsonld(data):
            types = node.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if any(str(t).lower() == "jobposting" for t in types):
                return node
    return None


def _walk_jsonld(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_jsonld(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)


def _jsonld_company(node: dict) -> str:
    org = node.get("hiringOrganization")
    if isinstance(org, dict):
        return str(org.get("name") or "").strip()
    if isinstance(org, str):
        return org.strip()
    return ""


def _jsonld_location(node: dict) -> str:
    loc = node.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                     addr.get("addressCountry")]
            parts = [str(p) for p in parts if isinstance(p, (str, int))]
            return ", ".join(parts)
        if isinstance(addr, str):
            return addr
    if node.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return ""


def _jsonld_salary(node: dict) -> str:
    sal = node.get("baseSalary")
    if not isinstance(sal, dict):
        return ""
    val = sal.get("value")
    cur = sal.get("currency") or ""
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        unit = val.get("unitText") or ""
        single = val.get("value")
        if lo and hi:
            return f"{cur} {lo}-{hi} {unit}".strip()
        if single:
            return f"{cur} {single} {unit}".strip()
    return ""


def _guess_company_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if "recruitee.com" in host or "myworkdayjobs.com" in host:
        return host.split(".")[0]
    generic = {"linkedin.com", "indeed.com", "nl.indeed.com", "nationalevacaturebank.nl",
               "jobbird.com", "magnet.me", "naukri.com", "bayt.com", "gulftalent.com",
               "foundit.in", "instahyre.com", "naukrigulf.com", "jobgether.com",
               "jobleads.com", "werkzoeken.nl"}
    if host in generic:
        return ""
    return host.split(".")[0]


def _strip_markdown(text: str) -> str:
    text = re.sub(r"!?\[[^\]]*\]\([^)]*\)", " ", text or "")
    text = re.sub(r"[#*_>`|-]{2,}", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def from_search_hit(hit: dict, geo_id: str, min_body_chars: int) -> tuple[dict | None, str]:
    """Build a Job from a Firecrawl search hit. Returns (job, reject_reason)."""
    url = (hit.get("url") or "").strip()
    title = (hit.get("title") or "").strip()
    if not url:
        return None, "no_url"
    if looks_like_index(url, title):
        return None, "index_page"

    markdown = hit.get("markdown") or ""
    html = hit.get("html") or hit.get("rawHtml") or ""
    ld = extract_jsonld_jobposting(html)

    if not ld and not looks_like_posting(url):
        return None, "not_a_posting_url"

    body = _strip_markdown(markdown) or _strip_markdown(hit.get("description") or "")
    if ld and ld.get("description"):
        ld_body = _strip_markdown(re.sub(r"<[^>]+>", " ", str(ld["description"])))
        if len(ld_body) > len(body):
            body = ld_body

    if len(body) < min_body_chars:
        return None, "body_too_short"

    job_title = (ld.get("title") if ld else "") or title
    job_title = re.sub(r"\s*[-|·—]\s*(LinkedIn|Indeed|Jobgether|JobLeads|Myworkdayjobs\.com).*$",
                       "", job_title, flags=re.I).strip()
    company = (_jsonld_company(ld) if ld else "") or _guess_company_from_url(url)
    location = (_jsonld_location(ld) if ld else "") or ""

    if not company:
        return None, "no_company"

    # A JSON-LD JobPosting block is proof this is one job. Without it we are relying on
    # a URL pattern, which also matches careers landing pages and news articles
    # ("ISRO Exam Date 2026 Out..."). Those never carry a real location, so demand one.
    if not ld and not location.strip():
        return None, "no_jsonld_and_no_location"

    # The query's geography is only a hint — a "Netherlands" query happily returns
    # US and UK roles. Trust the posting's own location when it states one.
    if location and not LOCATION_FLEXIBLE.search(location):
        stated_geo = classify_geo(location)
        if stated_geo != geo_id:
            return None, f"geo_mismatch:{stated_geo or 'out_of_scope'}"

    return {
        "url": url,
        "canonical_url": canonical_url(url),
        "title": job_title[:200],
        "company": company[:120],
        "location": location[:120],
        "salary_text": _jsonld_salary(ld) if ld else "",
        "posted_date": (ld.get("datePosted") if ld else "") or hit.get("publishedDate") or "",
        "body": body[:12000],
        "source": urlparse(url).netloc.lower().replace("www.", ""),
        "geo": geo_id,
        "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, ""


# Location strings -> geography id. Anything unmatched is outside the search and dropped.
# Both local-language and English spellings, and both accented and unaccented forms —
# Adzuna returns "Koln" as often as "Koln"/"Cologne", and a missed hint here shows up
# as an `ats_out_of_geography` / `*_geo_mismatch` reject rather than as an error.
GEO_LOCATION_HINTS = {
    "nl": ["netherlands", "nederland", "holland", "amsterdam", "rotterdam", "utrecht",
           "eindhoven", "den haag", "the hague", "tilburg", "breda", "venlo", "nijmegen",
           "arnhem", "groningen", "maastricht", "helmond", "veldhoven", "almere",
           "zwolle", "apeldoorn", "amersfoort", "veghel", "hoofddorp", "schiphol",
           "waalwijk", "roosendaal", "moerdijk", "born", "tiel", "zaltbommel"],
    "de": ["germany", "deutschland", "berlin", "munich", "munchen", "munchen",
           "hamburg", "frankfurt", "cologne", "koln", "koeln", "dusseldorf",
           "duesseldorf", "stuttgart", "leipzig", "dortmund", "essen", "bremen",
           "hannover", "nurnberg", "nuremberg", "mannheim", "karlsruhe", "bonn",
           "duisburg", "bielefeld", "munster", "augsburg", "aachen", "moenchengladbach",
           "monchengladbach", "wiesbaden", "darmstadt", "regensburg", "ingolstadt"],
    "be": ["belgium", "belgie", "belgique", "belgien", "antwerp", "antwerpen", "anvers",
           "brussels", "brussel", "bruxelles", "ghent", "gent", "gand", "bruges",
           "brugge", "leuven", "louvain", "liege", "luik", "charleroi", "namur",
           "mechelen", "hasselt", "kortrijk", "zaventem", "vilvoorde", "wavre",
           "sint-niklaas", "aalst", "roeselare", "genk", "oostende", "ostend",
           "turnhout", "dendermonde", "lokeren", "beveren", "willebroek", "grimbergen",
           "machelen", "steenokkerzeel", "halle-vilvoorde", "waregem", "tienen",
           "vlaams-brabant", "oost-vlaanderen", "west-vlaanderen", "antwerpen-provincie",
           "limburg", "henegouwen", "hainaut", "brabant wallon"],
    "ie": ["ireland", "eire", "dublin", "cork", "limerick", "galway", "shannon",
           "waterford", "athlone", "sligo", "dundalk", "drogheda", "kilkenny",
           "swords", "blanchardstown", "leixlip", "little island"],
    "pl": ["poland", "polska", "krakow", "cracow", "wroclaw", "gdansk", "warsaw",
           "warszawa", "poznan", "katowice", "lodz", "szczecin", "bydgoszcz", "lublin",
           "gdynia", "sopot", "tricity", "trojmiasto", "rzeszow", "torun", "gliwice",
           "bielsko-biala", "opole", "bydgoszcz", "bialystok", "czestochowa", "radom",
           "kielce", "zabrze", "sosnowiec", "olsztyn", "zielona gora",
           # voivodeships — Adzuna appends these after the city
           "malopolskie", "mazowieckie", "dolnoslaskie", "pomorskie", "slaskie",
           "wielkopolskie", "lodzkie", "lubelskie", "podkarpackie", "zachodniopomorskie",
           "kujawsko-pomorskie", "warminsko-mazurskie", "swietokrzyskie", "opolskie",
           "podlaskie", "lubuskie"],
    "uae": ["united arab emirates", "uae", "dubai", "abu dhabi", "sharjah", "ajman",
            "qatar", "doha", "saudi", "riyadh", "jeddah", "bahrain", "kuwait", "oman"],
    # Widened 6 Sept 2026 when India joined LinkedIn discovery. LinkedIn labels a card
    # with a district or a bare state — "Thane, Maharashtra", "Bengaluru East, Karnataka",
    # "Kochi, Ernakulam" — none of which the city list caught, so those postings were
    # being dropped as out of scope. Same fix as the Polish voivodeships above.
    "india": ["india", "mumbai", "navi mumbai", "thane", "pune", "delhi", "new delhi",
              "ncr", "gurgaon", "gurugram", "noida", "greater noida", "faridabad",
              "ghaziabad", "bangalore", "bengaluru", "hyderabad", "chennai", "kolkata",
              "ahmedabad", "coimbatore", "kochi", "cochin", "ernakulam", "indore",
              "jaipur", "lucknow", "nagpur", "vadodara", "surat", "visakhapatnam",
              "mysore", "mysuru", "bhubaneswar", "guwahati", "chandigarh", "mohali",
              "ludhiana", "amritsar", "kanpur", "patna", "bhopal", "raipur",
              "thiruvananthapuram", "trivandrum",
              # states — LinkedIn appends these, and often gives nothing else
              "maharashtra", "karnataka", "telangana", "tamil nadu", "haryana",
              "uttar pradesh", "gujarat", "west bengal", "kerala", "punjab",
              "rajasthan", "madhya pradesh", "andhra pradesh", "odisha", "bihar",
              "jharkhand", "uttarakhand", "goa", "chhattisgarh", "assam"],
}


# Location-flexible postings state no usable city, so they get the benefit of the doubt
# rather than being dropped as out of scope.
LOCATION_FLEXIBLE = re.compile(
    r"\b(remote|hybrid|anywhere|flexible|europe|emea|multiple locations|various)\b", re.I)


# Letters that unicodedata.normalize does NOT decompose — they are distinct letters,
# not accented forms, so NFKD leaves them alone and a hint like "wroclaw" never matches
# "Wroclaw". Handled explicitly before folding.
_LETTER_FOLD = str.maketrans({
    "\u0142": "l", "\u0141": "l",   # Polish l with stroke
    "\u00f8": "o", "\u00d8": "o",   # Danish/Norwegian o with stroke
    "\u00df": "ss",                  # German sharp s
    "\u00e6": "ae", "\u00c6": "ae",
    "\u0111": "d", "\u0110": "d",
})


def fold(text: str) -> str:
    """Lowercase and strip diacritics, so "Krakow" == "Krakow" and "Koln" == "Koln"."""
    folded = (text or "").translate(_LETTER_FOLD)
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def classify_geo(location: str) -> str | None:
    """Map a free-text location onto a configured geography, or None if out of scope."""
    loc = fold(location)
    if not loc:
        return None
    for geo_id, hints in GEO_LOCATION_HINTS.items():
        for hint in hints:
            if re.search(rf"\b{re.escape(fold(hint))}\b", loc):
                return geo_id
    return None


def from_ats_record(rec: dict, company: str, board: str, geo_id: str = "nl") -> dict:
    # ATS bodies arrive HTML-escaped (&lt;li&gt;), so unescape before stripping tags
    # or every tag survives as literal text. Greenhouse double-escapes, so
    # "&amp;nbsp;" needs a second pass — repeat until the string settles.
    raw = rec.get("body") or ""
    for _ in range(3):
        unescaped = html_mod.unescape(raw)
        if unescaped == raw:
            break
        raw = unescaped
    body = _strip_markdown(re.sub(r"<[^>]+>", " ", raw))
    url = rec.get("url", "")
    return {
        "url": url,
        "canonical_url": canonical_url(url),
        "title": (rec.get("title") or "")[:200],
        "company": company[:120],
        "location": (rec.get("location") or "")[:120],
        "salary_text": rec.get("salary") or "",
        "posted_date": rec.get("posted") or "",
        "body": body[:12000],
        "source": f"ats:{board}",
        "geo": geo_id,
        "seen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
