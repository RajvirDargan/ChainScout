"""Batched job scoring — the step Claude used to do.

Scores in batches of ~10 rather than one call per job. The old one-call-per-job shape
is what drove demand to ~210 LLM calls a day and straight into every rate limit; a
day's scoring is now roughly nine calls.

Model output is untrusted: every field is coerced, range-checked and length-capped
before it reaches the store, and any job the model silently drops is retried rather
than assumed scored.
"""

from __future__ import annotations

import json
import re
import time

MAX_BODY_CHARS = 2000          # scoring reads requirements, not boilerplate
MAX_RATIONALE = 600
MAX_FLAGS = 6
MAX_FLAG_CHARS = 90

SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "role_fit": {"type": "integer"},
                    "seniority_fit": {"type": "integer"},
                    "language": {"type": "integer"},
                    "visa": {"type": "integer"},
                    "location": {"type": "integer"},
                    "years_required": {"type": "integer"},
                    "rationale": {"type": "string"},
                    "flags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "score", "rationale", "flags"],
            },
        }
    },
    "required": ["jobs"],
}

# The Antigravity CLI is an agentic harness: left alone it tries shell/file tools, which
# headless mode auto-denies, and the run comes back SUCCESS with an empty response.
# Everything needed is already in the prompt, so tell it to answer directly.
NO_TOOLS = ("Answer directly from the text below. Do NOT run commands, read or write "
            "files, search the web, or use any tool. Reply with the JSON only.\n\n")

PROMPT_HEAD = NO_TOOLS + """You are scoring job postings for one candidate. Their full profile and the
exact scoring rubric follow. Apply the rubric in section 5 literally — do not invent your
own weighting.

Hard rules that override any score you might otherwise give:
- They can apply to roles asking AT MOST 3 years of experience. More than that scores under 20.
- No senior, lead, principal, head-of, director or management roles. Those score under 20.
- Never repeat any figure listed in the profile's "Do NOT claim" section.

For each job return:
- score: 0-100 overall, per the rubric
- role_fit / seniority_fit / language / visa / location: the per-dimension sub-scores
- years_required: years of experience the posting demands, 0 if unstated
- rationale: TWO sentences — why it fits them, and the one thing to check before applying.
  Write plainly and concretely, referencing their actual experience. No marketing language.
- flags: up to 5 short factual observations (sponsorship, language, seniority, salary, location)

Return every job you were given, keyed by the exact `id` string supplied. Do not omit any.

===== CANDIDATE PROFILE AND RUBRIC =====
{profile}

===== JOBS TO SCORE =====
{jobs}
"""


def _job_block(job: dict) -> str:
    body = re.sub(r"\s+", " ", (job.get("body") or ""))[:MAX_BODY_CHARS]
    flags = "; ".join(job.get("flags") or [])
    return (
        f"--- id: {job['fingerprint']}\n"
        f"title: {job.get('title','')}\n"
        f"company: {job.get('company','')}\n"
        f"location: {job.get('location','')}\n"
        f"geography: {job.get('geo','')}\n"
        f"salary_text: {job.get('salary_text','') or '(not stated)'}\n"
        f"prescreen_flags: {flags or '(none)'}\n"
        f"description: {body}\n"
    )


# The whole of PROFILE.md (~12k chars) was being re-sent with every batch. Only the
# sections the scorer actually applies are needed.
KEEP_SECTIONS = ("## 1.", "## 3.", "## 4.", "## 5.", "## 6.")


def trim_profile(profile: str) -> str:
    """Keep the hard constraints, the capability list, the target roles, the rubric and
    the do-not-claim list. Drop the narrative sections."""
    out, keeping = [], True
    for line in (profile or "").splitlines():
        if line.startswith("## "):
            keeping = line.startswith(KEEP_SECTIONS)
        if keeping:
            out.append(line)
    trimmed = "\n".join(out).strip()
    return trimmed if len(trimmed) > 500 else (profile or "")


def build_prompt(profile: str, jobs: list[dict]) -> str:
    return PROMPT_HEAD.format(profile=trim_profile(profile),
                              jobs="\n".join(_job_block(j) for j in jobs))


def _clean_flags(raw) -> list[str]:
    out = []
    for f in (raw or [])[:MAX_FLAGS]:
        if not isinstance(f, str):
            continue
        f = re.sub(r"\s+", " ", f).strip()[:MAX_FLAG_CHARS]
        if f:
            out.append(f)
    return out


def _clamp(value, lo, hi, default=0) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def parse_batch(payload, wanted: dict[str, dict]) -> dict[str, dict]:
    """Validate a batch response. Returns {fingerprint: scored_fields} for valid rows only.

    Rows for ids we did not ask about are discarded — the caller decides what to do about
    anything missing, which is what stops a dropped job from being silently treated as scored.
    """
    if isinstance(payload, dict):
        rows = payload.get("jobs")
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = None
    if not isinstance(rows, list):
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        fp = row.get("id")
        if not isinstance(fp, str) or fp not in wanted:
            continue
        rationale = row.get("rationale")
        rationale = re.sub(r"\s+", " ", rationale).strip()[:MAX_RATIONALE] \
            if isinstance(rationale, str) else ""
        if not rationale:
            continue
        out[fp] = {
            "score": _clamp(row.get("score"), 0, 100),
            "rationale": rationale,
            "flags": _clean_flags(row.get("flags")),
            "breakdown": {
                "role_fit": _clamp(row.get("role_fit"), 0, 40),
                "seniority_fit": _clamp(row.get("seniority_fit"), 0, 15),
                "language": _clamp(row.get("language"), 0, 15),
                "visa": _clamp(row.get("visa"), 0, 20),
                "location": _clamp(row.get("location"), 0, 10),
            },
            "years_required": _clamp(row.get("years_required"), 0, 25),
        }
    return out


def apply_ceiling(job: dict, max_years: int, watchlist: int) -> dict:
    """Force a role above the experience ceiling below the emailing threshold.

    Belt and braces over the prescreen regex AND the model's own score: flash-lite
    extracted years_required=5 correctly but still scored that job 42, one point under
    the watchlist cut. The stated requirement decides, not the model's generosity.
    """
    yrs = job.get("years_required") or 0
    if yrs <= max_years:
        return job
    # Flag it whether or not the score needed clamping, so the reason is always visible.
    flag = f"asks {yrs} years - above his {max_years}-year ceiling"
    if flag not in job["flags"]:
        job["flags"].insert(0, flag)
    if job.get("score", 0) >= watchlist:
        job["score"] = watchlist - 1
    return job


def score_jobs(jobs: list[dict], profile: str, chain, batch_size: int = 10,
               log=print, max_years: int = 3, watchlist: int = 45
               ) -> tuple[list[dict], list[dict]]:
    """Score every job. Returns (scored, unscored).

    Unscored jobs are NOT failures to be written off — the caller leaves them queued so
    they carry into tomorrow, the same rule that protects a failed send.
    """
    scored: dict[str, dict] = {}
    t_start = time.time()
    queue = list(jobs)
    size = max(1, batch_size)

    while queue and size >= 1:
        pending = [j for j in queue if j["fingerprint"] not in scored]
        if not pending:
            break
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        log(f"    scoring {len(pending)} job(s) in {len(batches)} batch(es) of {size}")
        progressed = False

        for i, batch in enumerate(batches, 1):
            wanted = {j["fingerprint"]: j for j in batch}
            t0 = time.time()
            payload = chain.complete(build_prompt(profile, batch), SCHEMA)
            got = parse_batch(payload, wanted)
            elapsed = time.time() - t0
            if got:
                progressed = True
                scored.update(got)
            missing = [fp for fp in wanted if fp not in got]
            # Per-batch progress: without this an unattended run is a black box for
            # minutes at a time, which is how a 20-minute stall went unnoticed.
            log(f"      batch {i}/{len(batches)}  {len(got)}/{len(batch)} scored  "
                f"{elapsed:5.1f}s" + (f"  ({len(missing)} missing)" if missing else ""))

        if all(j["fingerprint"] in scored for j in queue):
            break
        if not progressed and size == 1:
            break            # the model cannot handle these at all; stop burning quota
        size = 1 if size <= 2 else max(1, size // 3)

    total_elapsed = time.time() - t_start
    log(f"    scoring finished in {total_elapsed:.0f}s")

    out, unscored = [], []
    for job in jobs:
        fields = scored.get(job["fingerprint"])
        if fields is None:
            unscored.append(job)
            continue
        merged = list(job.get("flags") or [])
        for f in fields["flags"]:
            if f not in merged:
                merged.append(f)
        out.append(apply_ceiling({
            **{k: job.get(k) for k in ("fingerprint", "url", "title", "company",
                                       "location", "geo", "source", "posted_date")},
            "score": fields["score"],
            "rationale": fields["rationale"],
            "flags": merged,
            "breakdown": fields["breakdown"],
            "years_required": fields["years_required"],
        }, max_years, watchlist))
    return out, unscored
