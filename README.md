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
different sources on the same morning is only ever emailed once. The fuzzy layer is
geography-aware — the same title at a multinational in Germany and in Poland is two
jobs — and an internship is never merged into the full-time role.

### Reliability

A failed or interrupted run costs a retry, never a lost or repeated role:

- **A sent digest is always committed.** If the run dies between sending and marking
  roles as sent, the next run finishes the commit instead of emailing them again.
- **A broken run never looks like a quiet day.** If no source answered, or the scorer
  was down, nothing is sent — no false "No new roles" — and the next hourly tick retries.
- **Retries don't re-spend quota.** Collection runs once per day; a retry reuses it.
- **Failures are loud.** A run that doesn't deliver emails the recipients once that
  day with the log tail, and every digest carries a run-health footer (quota caps hit,
  LinkedIn blocked or its markup changed, jobs left unscored, a stalled Indeed channel).
- **The backlog is bounded.** Already-scored roles are never re-scored, unemailed
  roles expire after 21 days, and scoring has a wall-clock budget.

The details are in `RUNBOOK.md` under "When something goes wrong".

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
  Its "When something goes wrong" section covers how a failed or interrupted run
  recovers without losing or repeating a role, and how failures are surfaced (an
  alert email, and a run-health footer in the digest).

## Getting started

Requires Python 3.9+.

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in your own API keys
# edit PROFILE.md with your own background and rubric
# edit config.yaml — at minimum, recipients: and the geographies you care about
python3 -m pytest tests/ -q
python3 src/run.py collect --dry-run   # sanity-check discovery before spending quota
python3 src/run.py daily               # the full chain
```

See `RUNBOOK.md` for the full operator's guide, including the launchd scheduling
setup in `launchd/`.

## Project layout

```
config.yaml                 every tunable: geographies, titles, caps, thresholds, alerts
PROFILE.md                  candidate profile + 0-100 scoring rubric (read by the scorer)
PROFILE_INDIA_ADDENDUM.md   per-channel rubric override for the Indeed India digest
RUNBOOK.md                  operator's guide: costs, scheduling, dedup, failure handling
src/
  run.py                    stage runner: collect, score, render, send, commit, daily
  adzuna.py  linkedin.py    discovery adapters (Adzuna API, LinkedIn guest search)
  fetch.py                  ATS boards (Greenhouse/Lever/Recruitee), Firecrawl, IND register
  extract.py                job-page fetch + JSON-LD / Gemini / heuristic extraction
  normalize.py              canonical URLs, fingerprints, geography and city matching
  prescore.py               deterministic prescreen: seniority, years, language, salary
  score.py  llm.py          batched LLM scoring; Antigravity CLI with Gemini API fallback
  gemini.py                 structured extraction for pages without JSON-LD
  quota.py                  spend ledger: every metered call reserves against a cap
  store.py                  SQLite seen-store: dedup layers and job status
  render.py  mailer.py      HTML/plain-text digest and SMTP delivery
  indeed.py                 Indeed India channel (records handed over by a Claude task)
templates/digest.html.j2    the email
tests/                      pytest suite
launchd/                    macOS scheduling (daily at 07:30, at login, hourly catch-up)
state/  outbox/  logs/      created at runtime, gitignored
```

## Configuration knobs worth knowing

Everything lives in `config.yaml`, commented in place. The ones added with the
reliability work:

| Key | Default | What it does |
|---|---|---|
| `timezone` | `Europe/Amsterdam` | The run's clock: digest date, the 07:30 guard and the daily quota ledger |
| `alerts.enabled` | `true` | Email the recipients once a day when a run fails to deliver |
| `thresholds.carry_forward_max_days` | `21` | Retire never-emailed roles after this many days in the queue |
| `llm.max_scoring_seconds` | `1800` | Wall-clock budget for scoring; the rest carries forward |
| `sources.linkedin_detail.daily_calls` | `150` | Cap on LinkedIn job-page fetches (paced like the search) |
| `deadlines.label` | `sponsor-filing deadline` | How the countdown in the email header names `sponsor_filing` |
| `geographies[].short_label`, `digest_heading` | — | Chip/subject label and "Beyond …" heading for a geography |

## Testing

```bash
python3 -m pytest tests/ -q
```

The suite covers URL canonicalisation and the dedup layers, the prescreen gates, spend
caps, the source adapters, the scorer and LLM chain, and the daily chain itself
(send/commit recovery, the no-misleading-digest gate, collect order, alerts). It runs
offline: sources, the LLM and SMTP are stubbed.

## Stack

Python 3.9+ (standard-library `zoneinfo`, `sqlite3`, `smtplib`), `requests`, `pyyaml`,
`jinja2`; `lxml` only for parsing the IND sponsor register. No framework, no database
beyond a local SQLite dedup store. LLM calls go through one small interface (`llm.py`)
with two interchangeable backends.
