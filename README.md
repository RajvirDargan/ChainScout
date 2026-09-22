# ChainScout

A daily job-search agent for supply-chain and logistics roles that scans multiple
sources, scores every posting against a configurable candidate profile, deduplicates
against everything it's already seen, and emails a ranked HTML digest — unattended,
on a schedule.

> **Note:** `PROFILE.md` in this repo is a fictional example candidate ("Alex Chen"),
> shipped so the scoring rubric is demonstrated end to end and the agent is runnable
> out of the box. Swap it for your own background before pointing this at a real job
> search.

## What it does

```
collect  →  score  →  render  →  send  →  commit
```

1. **Collect** — pulls postings from the Adzuna API, LinkedIn's public guest search,
   and direct ATS board APIs (Greenhouse, Recruitee), across a configurable list of
   geographies, each with its own priority, title vocabulary and visa rules.
2. **Prescreen** — a fast, deterministic gate (`prescore.py`) rejects postings on
   seniority, years-of-experience ceiling, disqualifying language requirements and
   nationality-restricted roles, before anything reaches an LLM.
3. **Score** — an LLM (Antigravity CLI, with a Gemini API fallback) scores every
   surviving posting 0–100 against the rubric in `PROFILE.md`: role fit, seniority
   fit, language, visa viability per country, and location — batched to stay well
   inside free-tier rate limits.
4. **Render** — builds an HTML digest, split into a top-priority-country section and
   a "beyond" section, with a guaranteed floor of slots for the priority country so
   it never has to out-score the rest of the world for every place.
5. **Send / commit** — delivers over SMTP and only marks postings as sent once the
   send demonstrably succeeded, so a failed send never silently loses a day's roles.

Every stage is idempotent and dedup runs across three layers (canonical URL,
fingerprint, fuzzy company-name matching) so the same posting arriving from three
different sources on the same morning is only ever emailed once.

## Why this exists

Manually re-running the same searches across five country-specific job boards,
several company career pages, and LinkedIn every day — then re-reading postings
already seen yesterday — doesn't scale. This turns that into a ~3-minute unattended
run that degrades gracefully: a source that times out or a country that returns
zero results one day doesn't block the rest, and every metered API call reserves
against a spend ledger first so a bug can't blow through a free-tier quota.

## Design notes worth reading

- **`config.yaml`** is the whole surface for tuning — geographies, per-country title
  vocabulary, visa rules, salary floors, thresholds and API spend caps — with no code
  changes needed. The comments throughout document *why* each value is what it is,
  including several "this looked right but wasn't" corrections found by probing live
  APIs (see `RUNBOOK.md`'s "Notes from setup").
- **`PROFILE.md`** is the scoring rubric, read directly by the LLM prompt in
  `score.py`. It's plain Markdown on purpose — no schema to maintain, and the model
  reads it the same way a person would.
- **`RUNBOOK.md`** is the detailed operator's guide: what each source costs, how the
  launchd scheduling handles a sleeping machine, the dedup logic, and a running list
  of failure modes hit and fixed during development (a JSON-parsing bug that silently
  turned successful LLM batches into retries, DNS-not-ready-yet failures right after
  wake-from-sleep, Adzuna's per-country title vocabulary not being portable, etc.).

## Getting started

```bash
cp .env.example .env        # fill in your own API keys
# edit PROFILE.md with your own background and rubric
# edit config.yaml — at minimum, recipients: and the geographies you care about
python3 -m pytest tests/ -q
python3 src/run.py collect --dry-run   # sanity-check discovery before spending quota
python3 src/run.py daily               # the full chain
```

See `RUNBOOK.md` for the full operator's guide, including the launchd scheduling
setup in `launchd/`.

## Stack

Python, `requests`, `pyyaml`, `jinja2`. No framework, no database beyond a local
SQLite dedup store. LLM calls go through one small interface (`llm.py`) with two
interchangeable backends.
