"""Gemini 2.5 Flash-Lite structured extraction.

Used only where free deterministic parsing fails: 3 of 5 real job pages tested carried
no JSON-LD (Johnson & Johnson, LinkedIn, Greenhouse), and those are exactly the pages
Firecrawl used to be paid to parse. Free tier is generous on the flash-lite line; capped further by our own ledger.

Model output is untrusted input: every field is type-checked and length-capped here,
and the caller keeps its own URL rather than anything the model returns.
"""

from __future__ import annotations

import json
import re

import requests

# Verified live 31 Aug 2026. The 2.x flash models are retired for new keys, so this
# is read from config rather than hardcoded at the call site — see config.yaml.
MODEL = "gemini-3.5-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Only fields that inform scoring. Deliberately no URL field — see module docstring.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_posting": {"type": "boolean"},
        "title": {"type": "string"},
        "company": {"type": "string"},
        "location": {"type": "string"},
        "salary_text": {"type": "string"},
        "language_requirement": {"type": "string"},
        "years_required": {"type": "integer"},
        "description": {"type": "string"},
    },
    "required": ["is_job_posting", "title", "company", "location", "description"],
}

PROMPT = """Extract the job posting from this page text.

Rules:
- Set is_job_posting false if this is a search results page, a careers landing page,
  a news article, or anything that is not one specific job vacancy.
- Copy values from the text. Do not infer, translate or invent anything.
- salary_text: quote the pay verbatim as written, or "" if absent.
- language_requirement: quote what the posting says about required languages, or "".
- years_required: minimum years of experience demanded; 0 if not stated.
- description: the responsibilities and requirements, plain text, no markup.

PAGE TEXT:
"""

MAX_INPUT_CHARS = 30000
FIELD_CAPS = {"title": 200, "company": 120, "location": 120,
              "salary_text": 200, "language_requirement": 300, "description": 12000}


class Gemini:
    def __init__(self, api_key: str, quota=None, log=print, model: str = MODEL):
        self.api_key = api_key
        self.model = model
        self.quota = quota
        self.log = log
        self.requests_used = 0
        self.session = requests.Session()

    def available(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("your-")

    NOT_A_POSTING = "not_a_posting"

    def extract(self, page_text: str) -> dict | str | None:
        """dict on success, NOT_A_POSTING when the model says the page has no vacancy,
        None when unavailable, over quota, or erroring.

        The NOT_A_POSTING verdict matters: without it the caller falls back to
        heuristics and stores page furniture as the job description."""
        if not self.available() or not page_text.strip():
            return None
        if self.quota is not None and not self.quota.check_and_reserve("gemini", 1):
            return None

        payload = {
            "contents": [{"parts": [{"text": PROMPT + page_text[:MAX_INPUT_CHARS]}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
                "temperature": 0,
            },
        }
        try:
            r = self.session.post(
                ENDPOINT.format(model=self.model),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=payload, timeout=60,
            )
        except requests.RequestException as exc:
            self.log(f"    ! gemini network error: {exc}")
            if self.quota is not None:
                self.quota.refund("gemini", 1)
            return None

        if r.status_code == 429:
            self.log("    ! gemini rate limited — falling back to heuristics")
            return None
        if not r.ok:
            self.log(f"    ! gemini HTTP {r.status_code}: {r.text[:160]}")
            return None

        self.requests_used += 1
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            self.log(f"    ! gemini returned unusable output: {exc}")
            return None

        if isinstance(data, dict) and not data.get("is_job_posting"):
            return self.NOT_A_POSTING
        return _validate(data)


def _validate(data: dict) -> dict | None:
    """Coerce and cap everything. The model is a data source, not a trusted caller."""
    if not isinstance(data, dict) or not data.get("is_job_posting"):
        return None

    out: dict = {}
    for field, cap in FIELD_CAPS.items():
        value = data.get(field, "")
        if not isinstance(value, str):
            value = "" if value is None else str(value)
        out[field] = re.sub(r"\s+", " ", value).strip()[:cap]

    if not out["title"] or not out["description"]:
        return None

    years = data.get("years_required", 0)
    try:
        years = int(years)
    except (TypeError, ValueError):
        years = 0
    out["years_required"] = years if 0 <= years <= 50 else 0
    return out
