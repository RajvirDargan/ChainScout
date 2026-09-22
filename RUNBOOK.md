# job_agent runbook

Daily sweep for logistics-family roles, scored against the candidate profile in
`PROFILE.md`, emailed to whoever is listed in `config.yaml` under `recipients:`. One
digest a day, and a role is never sent twice.

**Geographies widened once the Netherlands alone stopped being enough runway.** The
Netherlands remains **priority 1 and the preferred outcome**; Germany, Belgium,
Ireland and Poland were added beneath it against a permit-expiry deadline (see
`PROFILE.md` §1). Order: **NL > Germany > Belgium > Ireland > Poland > Dubai/ME >
India**.

**The digest has two top-level sections**, Netherlands then "Beyond the Netherlands".
NL also holds a guaranteed floor of email slots (`thresholds.min_priority_geo_slots`,
10 of 20) so it never has to out-score the rest of the world for every place — but slots
above that floor are still won on score alone, which is the rule that stops a 58-point
NL role displacing a 78-point one.

## Layout

```
config.yaml   sources, caps, queries, thresholds, ATS boards  <- edit this, not the code
PROFILE.md    the CV, constraints and the 0-100 rubric        <- the scorer reads this
.env          ADZUNA_APP_ID/KEY, GEMINI_API_KEY  (gitignored)
src/          adzuna, extract, gemini, quota, fetch, normalize, store, prescore, render, run
state/        seen.sqlite (dedup memory + spend ledger) + ind_sponsors.csv
outbox/       <date>_candidates.json -> _scored.json -> _digest.html
logs/         run-<date>.log
```

## Sources and what they cost

| Stage | Source | Cost |
|---|---|---|
| Discovery (NL, DE, BE, PL, India) | Adzuna API | free, 28 calls/day of ~1,000/month, 50 results each |
| Discovery (IE, UAE, **India**) | LinkedIn public guest search | free, no key, 64 calls/day |
| Discovery | ATS boards (Greenhouse/Recruitee) | free, unlimited |
| Extraction | requests + JSON-LD | free |
| Extraction | LLM chain (no-JSON-LD pages only) | plan-backed |
| Scoring | LLM chain, batched ~10 jobs per call | plan-backed |
| Sending | Gmail SMTP + app password | free |
| *(disabled)* | Firecrawl | **off** — cost 324 credits in one run |

Every metered call reserves against the ledger in `state/seen.sqlite` first. A cap that
is reached makes the caller **skip**, so the run degrades to free sources instead of
failing. Caps live under `sources:` in `config.yaml`.

**India runs on both sources since 6 Sept 2026.** Adzuna reaches India fine, but it
under-indexes the junior and internship market: the 5 Sept deep sweep found 13 of the
top 18 India postings on LinkedIn and only 5 on Adzuna, and Adzuna returned 9 results
for "supply chain intern" against LinkedIn's 20 per city. India therefore gets
`linkedin_titles` (its own vocabulary — "executive", "intern", not "engineer") and
`linkedin_locations` (four cities, because the guest endpoint caps a query near 20
cards and weights location hard). Both overrides are per-geography and live in
`config.yaml`; any geography can use them.

**Firecrawl is off** (`sources.firecrawl.enabled: false`, `monthly_credits: 0`). The
adapter still works; set `enabled: true` and a credit cap to bring it back for a gap
only it can fill. Never re-enable `firecrawl.scrape` — billing is per scraped *result*,
which is what turned 2-credit queries into 10-credit ones.

## How it runs now

**Claude is no longer involved.** A launchd agent on this Mac fires one command daily;
scoring runs on the Antigravity CLI (falling back to the Gemini API), and the digest is
sent over SMTP by the agent itself.

```bash
launchctl list | grep jobagent                              # is it loaded?
launchctl kickstart -k gui/$(id -u)/com.example.jobagent    # run it right now
launchctl bootout   gui/$(id -u)/com.example.jobagent       # stop it
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.jobagent.plist
```

**Schedule: three triggers, one idempotent job.** The Mac is often asleep or off at 07:30,
so delivery does not depend on it being awake at that moment:

| Trigger | Covers |
|---|---|
| `StartCalendarInterval` 07:30 | the normal case |
| `RunAtLoad` | login / reboot — the Mac having been powered **off**, which launchd's own catch-up handles worst |
| `StartInterval` hourly | waking from sleep at any hour |

Firing often is safe because `daily` exits immediately unless there is work to do:

1. **already sent today** — if `outbox/<date>_email.json` has a `sent_message_id`, it no-ops.
2. **too early** — before `schedule.earliest_hour:minute` (07:30) it waits, so an hourly
   tick at 03:00 does not deliver a digest in the middle of the night.
3. **already running** — a PID lock at `state/daily.lock` stops the hourly tick re-entering
   a run in progress. A lock left by a killed run is detected as stale and cleared, so it can
   never block future digests.

`python3 src/run.py daily --force` bypasses guards 1 and 2 (never the lock) for manual runs.

So: if you open the Mac at 09:15, the digest goes out within the hour. If at 06:00, it waits
until 07:30.
launchd output lands in `logs/launchd-out.log` / `logs/launchd-err.log`; the run's own
log is `logs/run-<date>.log`. The plist source of truth is `launchd/com.jobagent.plist`
(edit the placeholder paths for your machine, then copy it to `~/Library/LaunchAgents/`).

`daily` checks the Gmail credentials **before** collecting, so a run that could not
deliver costs no Adzuna, LinkedIn or LLM quota — it exits 2 and says why.

## Daily sequence

```bash
cd job_agent   # the repo root
python3 src/run.py daily          # the whole chain, what launchd runs

# or step by step:
python3 src/run.py collect        # -> outbox/<date>_candidates.json
python3 src/run.py score          # -> outbox/<date>_scored.json   (LLM)
python3 src/run.py render         # -> outbox/<date>_digest.html + _email.json
python3 src/run.py send           # SMTP; prints and stores the Message-ID
python3 src/run.py commit --message-id <id>
```

`score --dry-run --limit 10` scores one batch and writes nothing — use it to check the
backend and the per-dimension breakdown before trusting a change.

`commit` is deliberately separate and refuses to run without a Gmail message id.
Nothing is marked as sent unless the email demonstrably went out, so a failed send
means those roles simply reappear in tomorrow's digest rather than vanishing.

## The scored file

`render` expects `outbox/<date>_scored.json`:

```json
{"jobs": [
  {"fingerprint": "<copied verbatim from the candidate>",
   "url": "...", "title": "...", "company": "...", "location": "...",
   "geo": "nl", "source": "...", "posted_date": "...",
   "score": 78,
   "rationale": "Two sentences: why it fits, and the one thing to check.",
   "flags": ["IND recognised sponsor", "salary EUR 3.400 clears the IND floor"]}
]}
```

Every candidate must appear, including low scorers — anything omitted is never
recorded as below-threshold and will be re-scored tomorrow. Carry the candidate's
`flags` through and add scoring observations to them.

## Dedup

Three layers in `store.py`, because one posting appears on LinkedIn, Indeed and the
company's own board on the same morning:

1. canonical URL — tracking params stripped
2. fingerprint — `sha1(company | title | city)`, all normalised
3. fuzzy — identical title with one company name containing the other
   ("Vanderlande" / "Vanderlande Industries"), plus a 0.92 ratio fallback

Below-threshold jobs are stored with `status='below_threshold'` so they are never
re-scored. To deliberately resurface something:

```bash
sqlite3 state/seen.sqlite "UPDATE jobs SET status='new', sent_on=NULL WHERE company LIKE '%Some Company%';"
```

## Checks

```bash
python3 -m pytest tests/ -q          # 36 tests: dedup, prescreen, spend caps, adapters
python3 src/run.py collect --dry-run # search + report, writes nothing
python3 src/run.py status            # store stats and recent runs
```

Every `collect` prints a `quota:` block showing spend against each cap. Check it if a
run looks thin — an exhausted cap shows up there rather than as an error.

Running `collect` twice must report `NEW to score 0` the second time — that is the
direct proof of the no-duplicates guarantee.

## Pausing

The scheduled task is `daily-job-scan`. Ask Claude to disable it, or delete
`~/.claude/scheduled-tasks/daily-job-scan/`. Scheduled tasks only fire while the
Claude app is open; if it is closed at 07:30 the run happens at next launch.

## Notes from setup

- The Greenhouse tokens `byd` and `action` resolve to unrelated US companies
  (BYD Pasadena CA, Action Idaho) — not BYD Europe or Action NL. Check posting
  locations before adding a board.
- bol.com's board stubs the description field to `<p>x</p>`; those postings are
  scraped individually via Firecrawl (`enrich_thin_ats`).
- Adzuna call budget: NL 10 (priority 1 gets the full `adzuna.titles` list) + DE 5 +
  BE 5 + PL 5 + India 3 = **28/day = 840/month**, against caps of 30/day and 900/month.
  Germany and India carry `adzuna_titles` overrides to hold that budget. Adding a fifth Adzuna geography needs the caps raised, not just a config
  line. Note `collect --dry-run` **does** reserve quota — it makes the same live calls,
  it just writes nothing. Two dry runs in a day will exhaust the budget.
- **Job title vocabulary is not portable, so a geography can override the title list**
  with `adzuna_titles`. Probed live: on the Polish index "logistics engineer",
  "supply chain analyst" and "supply chain planner" all return **0**, while "logistics
  specialist" returns 9 and "order to cash" 43 — the Krakow/Wroclaw shared-service belt
  advertises by process name and "specialist". Belgium is similar (default list scored
  1/2/2; the override scores 19/13/39/10/8). Check a new geography's vocabulary against
  the API before trusting the default list, or it will silently collect nothing.
- Adzuna truncates descriptions, so `src/extract.py` fetches the real page. Plain
  requests returned HTTP 200 on every job page tested, LinkedIn included.
- Only about 40% of job pages carry JSON-LD. The rest go to Gemini; without a Gemini
  key they fall back to heuristics, which get a body but no company or location.
- **Adzuna has no UAE index and no Ireland index** — `ae`, `ie` and `cz` all return 404,
  probed live 2 Sept 2026. Working indexes that day: `de` (9,526 logistics hits), `be`
  (1,133), `pl` (2,068), `at` (358), `in`, `nl`. The Gulf **and Ireland** are covered by
  `src/linkedin.py` instead, using LinkedIn's public logged-out job search. Everything
  else free was checked and failed: Careerjet's API refuses connections, Bayt /
  GulfTalent / NaukriGulf / Indeed AE all return 403, Gemini grounding returns 429 on
  this key, and not one of the free ATS boards carried a single Gulf role (Expeditors
  533 postings, Flexport 165 — zero).
- LinkedIn is someone else's service with no API contract: calls are paced 1.5s apart,
  paged only two deep, capped in the ledger, and a 429 stops LinkedIn for the whole run.
  If the card markup changes, `to_job` returns None and the run continues without it.
- Gulf postings are frequently **"UAE Nationals only" / Emiratisation** roles. For a
  non-national candidate those are a categorical bar, so they are rejected in
  `prescreen`. `saudisation` / `saudization` were added 2 Sept 2026: Saudi Arabia
  enforces 70%
  Saudisation across twelve procurement professions from 31 May 2026, four of them
  logistics roles. Local-language requirements (German, French, Polish) are **flags**,
  not rejects — the scorer decides, since many postings list the language as "a plus".
- **The candidate cap round-robins across geographies.** A strict priority sort starved
  everything below the top geography: Germany alone yields ~165 candidates against
  `max_candidates_per_run: 60`, so priority order gave Germany all 60 slots and the
  other five zero — permanently, since dropped candidates are deleted and re-collected
  identically the next morning. Priority still breaks ties within a round.
- **Location matching folds accents** (`normalize.fold`). Adzuna returns "Krakow" with
  the accent and "Wroclaw" with a stroked l; unaccented hints never matched them, so
  real postings were dropped as `ats_out_of_geography`. Polish l-with-stroke does not
  decompose under NFKD, so it is translated explicitly.
- The IND register is one inline HTML table of ~12.9k rows with the organisation
  name in a `<th>`, not a downloadable file. A failed fetch yields "sponsor status
  unknown" and never scores a job down. **It is no longer fetched**: the lookup is
  gated on a geography declaring `visa_mode: ind_sponsor`, and none does since NL was
  dropped. The code stays live for the day a register-backed geography returns.
- **Salary floors are per-geography** (`salary_floor_month` in `config.yaml`), merged
  into the prescreen rules by `run.py` before `prescore.screen()`. Belgium 3,260.80
  (Flanders under-30) and Ireland 3,050.42 (GEP) bind; **Germany deliberately has
  none** — sec. 18b(1) has no statutory minimum under 45, so scoring a German role
  against the Blue Card figure would penalise the best route the candidate has. A
  geography with no floor skips the check rather than defaulting to zero.
- Corporate groups register per legal entity, so "DSV Solutions" is reported as a
  group-level match against "DSV Road B.V." rather than a flat yes or no.


## The LLM chain

`src/llm.py` exposes one interface with two backends, chosen in `config.yaml` under `llm`:

1. **Antigravity CLI** (`agy`, preferred) — headless
   `agy -p … --output-format json --json-schema …`, authenticated from the cached keyring
   session of a one-time interactive `agy` login. Draws on the Antigravity plan.
2. **Gemini API** (fallback) — rotates `model_preference`, because no single free-tier
   model sustained the load.

If `agy` is missing or not logged in, the run says so and falls back. If both fail, the
affected jobs stay unscored and **carry forward to tomorrow**. `_scored.json` records the
usage mix and the digest footer prints it, so a fallback is never silent.

Setup, once:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy        # complete the browser sign-in; headless runs reuse the cached session
```

**Scoring is batched at ~10 jobs per call.** One call per job is what pushed demand to
~210 calls/day and into every rate limit; a day is now roughly a dozen calls. Any job the
model drops from a batch is retried in a smaller batch, then alone, then left unscored.

### Why not gemini-3.8-flash
Measured 3 Sept 2026: the API reports `quotaValue: "20"` requests/day on the free tier
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). It is deliberately absent from
`model_preference`. If billing is enabled on the project, put it at the front of that list.

## Seniority ceiling

The candidate can apply to at most **3 years** of required experience and no senior
or management roles. Enforced in `prescore.py` before anything reaches the LLM:

- `max_years_experience: 3` in config — above that is a hard reject, not a flag.
- Two-tier titles: `reject_seniority_always` (senior, lead, principal, head of, …) and
  `reject_seniority_unless_junior` (manager, supervisor), so *Management Trainee* survives.
- `seniority_false_positives` exempts domain phrases — "Disturbance Management",
  "Lead Time", "Inventory Management" are vocabulary, not rank. An over-rejection is
  invisible to the candidate, which makes it worse than a low score.
- "Executive" is deliberately NOT a reject term: in Gulf and Indian markets it usually
  denotes a junior individual contributor.

Applied to the 82-job backlog this rejected **35 (43%)** — the 5-year roles and the
Customs/Logistics Manager titles that had been reaching the inbox.


## Performance notes (learned the hard way, 4 Sept 2026)

A run that should take ~3 minutes once took 20+ without finishing. Causes, in order of size:

1. **`_extract_json` stripped markdown fences before attempting a parse.** The agy envelope
   is valid JSON whose `response` field *contains* a fenced string, so the fence regex matched
   the inner content and destroyed the outer object. Successful batches were recorded as
   failures, triggering a 10 -> 3 -> 1 retry cascade and dropping work onto the rate-limited
   API. **Parse first; strip fences only as a fallback.** Regression-tested.
2. **Read `structured_output`**, not `response`. agy already parses the schema-constrained
   answer; `response` arrives fenced and as a bare array rather than `{"jobs": [...]}`.
3. **Reasoning effort dominates.** Measured on one 10-job batch:
   `-low` 16s / 0 thinking tokens · `-medium` 50-78s / 11k-22k · `-high` far worse.
   `-low` gives the same seniority judgement (scored a 5-year role 19 vs medium's 15), so it
   is the default. Swap in `config.yaml` if you want more reasoning.
4. **Run agy from a neutral cwd.** It is a coding agent: pointed at the project it ingests
   workspace context (78s vs 50s for an identical call). `llm.py` uses `tempfile.gettempdir()`.
5. **Log every batch.** Without per-batch timing an unattended stall is invisible.

Result: **152 jobs in 211s**, 16 batches, zero fallbacks, zero unscored.

If a batch ever fails to parse again, the raw envelope is saved to `logs/agy-unparsable-*.json`
rather than discarded — inspect that first.

## The experience ceiling has two guards

`prescore.py` rejects on the stated years before any LLM call, and `score.apply_ceiling()`
then forces anything the model reports above `max_years_experience` below the watchlist
threshold, with a flag naming the reason. The second guard exists because a model can extract
`years_required: 5` correctly and still score the job 42 — one point under the cut.

**Carried-forward jobs are re-screened against the current rules** on every collect, so
tightening a rule applies retroactively to the queue instead of only to new finds.

## Indeed India channel (added 19 Sept 2026)

A **second, separate email** — subject `[Indeed India] …` — to the same recipients. The
Indeed connector only exists inside a Claude session, so discovery runs as the Claude
desktop scheduled task **`indeed-india-daily`** (09:07 daily; it runs only while the Claude
app is open and catches up on next launch). Everything after discovery is this agent.

```
task: 8 search_jobs calls -> outbox/indeed/<date>_raw.json
python3 src/run.py indeed-plan outbox/indeed/<date>_raw.json     # prints <=12 ids worth a detail fetch
task: get_job_details per id -> outbox/indeed/<date>_details.json
python3 src/run.py indeed-ingest outbox/indeed/<date>_details.json --raw outbox/indeed/<date>_raw.json
python3 src/run.py indeed-daily                                   # score -> render -> send -> commit
```

- **Same rubric.** `score --channel indeed` reads PROFILE.md plus `PROFILE_INDIA_ADDENDUM.md`,
  which only swaps the India location band (Delhi NCR 9–10, Mumbai 7–8, remote 7, else 4–6)
  and notes the candidate's home base for relocation-cost purposes. The main digest
  never reads the addendum.
- **Never sent twice, across both emails.** Dedup is store-wide (`seen.sqlite`): anything the
  07:30 digest has seen is skipped before a detail call is spent, and vice versa.
- **Never leaks.** `unsent_carry_forward` filters by source — main `collect` skips
  `source='indeed'`, the Indeed ingest takes only it.
- Channel files live in `outbox/indeed/`, logs in `logs/run-<date>-indeed.log`, run rows in
  `runs` as `<date>#indeed`, lock `state/indeed.lock`. `score`/`render`/`send`/`commit`
  take `--channel indeed` for manual reruns.
- **Connector rate limit:** ~22 rapid calls then a lockout that every retry extends. The
  task budgets 8 searches + ≤12 details and waits once (120 s) before giving up.
