"""Fetch a job page and pull structured fields out of it.

Three steps, stopping at the first that succeeds:

  1. requests + JSON-LD   free, deterministic, exact
  2. Gemini Flash-Lite    free (1,000/day), for the ~60% of pages with no JSON-LD
  3. heuristics           free, always available, noisier

Plain requests returned HTTP 200 on every job page tested, LinkedIn included, so the
fetch itself never needed a paid service — only the parsing did.
"""

from __future__ import annotations

import re

import requests

import normalize as nz

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,nl;q=0.8"}

STRIP_TAGS = re.compile(r"<(script|style|noscript|svg|head)\b.*?</\1>", re.S | re.I)
TAG = re.compile(r"<[^>]+>")


def fetch_html(url: str, timeout: int = 25) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r.text if r.ok else ""
    except requests.RequestException:
        return ""


def html_to_text(html: str) -> str:
    """Cheap readable-text extraction. Good enough to feed a model or regexes.

    Unescaping comes FIRST: a JSON-LD description arrives as escaped markup
    (&lt;strong&gt;), so stripping tags before unescaping leaves literal <strong>
    and <br> in the job body.
    """
    if not html:
        return ""
    import html as html_mod
    text = html
    for _ in range(3):
        unescaped = html_mod.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = STRIP_TAGS.sub(" ", text)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", "\n", text, flags=re.I)
    text = TAG.sub(" ", text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def enrich(job: dict, gemini=None, min_chars: int = 300, log=print) -> tuple[dict, str]:
    """Fill in job['body'] and any missing fields. Returns (job, method_used)."""
    url = job.get("url") or job.get("canonical_url") or ""
    if not url:
        return job, "no_url"

    html = fetch_html(url)
    if not html:
        return job, "fetch_failed"

    # --- 1. JSON-LD: free and exact.
    ld = nz.extract_jsonld_jobposting(html)
    if ld:
        desc = html_to_text(str(ld.get("description") or ""))
        if len(desc) >= min_chars:
            job["body"] = desc[:12000]
            job.setdefault("salary_text", "")
            if not job.get("salary_text"):
                job["salary_text"] = nz._jsonld_salary(ld)
            if not job.get("location"):
                job["location"] = nz._jsonld_location(ld)[:120]
            if not job.get("company"):
                job["company"] = (nz._jsonld_company(ld) or job.get("company", ""))[:120]
            return job, "jsonld"

    text = html_to_text(html)

    # --- 2. Gemini, only for pages JSON-LD could not handle.
    if gemini is not None and gemini.available() and len(text) > 200:
        fields = gemini.extract(text)
        if fields == gemini.NOT_A_POSTING:
            # Trusted verdict. Returning no body means prescreen drops this rather
            # than scoring whatever navigation text the page happened to contain.
            return job, "not_a_posting"
        if fields:
            job["body"] = fields["description"][:12000]
            for key in ("company", "location", "salary_text"):
                if fields.get(key) and not job.get(key):
                    job[key] = fields[key]
            if fields.get("language_requirement"):
                job.setdefault("flags", []).append(
                    f"language: {fields['language_requirement'][:80]}")
            if fields.get("years_required"):
                job["years_required"] = fields["years_required"]
            return job, "gemini"

    # --- 3. Heuristics. Never leaves the job without a body if the page had text.
    if len(text) >= min_chars:
        job["body"] = text[:12000]
        return job, "heuristic"

    return job, "too_short"
