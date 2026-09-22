#!/usr/bin/env python3
"""Daily job agent — stage runner.

  collect   search all sources, drop everything already seen, write today's candidates
  render    turn the scored file into an HTML digest
  commit    mark jobs as sent   <- run ONLY after the email actually went out
  status    show store stats and what is pending

The split exists because Claude does the scoring between collect and render.
commit is separate so a failed send never silently swallows a day's postings.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import adzuna as adzuna_src  # noqa: E402
import extract  # noqa: E402
import fetch  # noqa: E402
import linkedin as linkedin_src  # noqa: E402
import normalize as nz  # noqa: E402
import prescore  # noqa: E402
import gemini as gemini_src  # noqa: E402
import indeed as indeed_src  # noqa: E402
import llm as llm_src  # noqa: E402
import mailer as mailer_src  # noqa: E402
import score as score_src  # noqa: E402
import render  # noqa: E402
from quota import Quota  # noqa: E402
from store import Store  # noqa: E402

CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state"
OUTBOX = ROOT / "outbox"
LOGS = ROOT / "logs"
INDIA_ADDENDUM = ROOT / "PROFILE_INDIA_ADDENDUM.md"


def channel_of(args) -> str:
    return getattr(args, "channel", None) or "main"


def outbox_for(args) -> Path:
    """Each channel keeps its own manifests, so the Indeed digest can never trip the
    main digest's already-sent guard (or the other way round)."""
    return OUTBOX / "indeed" if channel_of(args) == "indeed" else OUTBOX


def run_key(run_date: str, args) -> str:
    """`runs` is keyed by date; the Indeed channel gets its own row per day."""
    return run_date if channel_of(args) == "main" else f"{run_date}#{channel_of(args)}"


def log_for(run_date: str, args) -> "Logger":
    suffix = "" if channel_of(args) == "main" else f"-{channel_of(args)}"
    return Logger(LOGS / f"run-{run_date}{suffix}.log")


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


class Logger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a")

    def __call__(self, msg: str = ""):
        stamp = datetime.now().strftime("%H:%M:%S")
        print(msg, flush=True)
        self.fh.write(f"{stamp} {msg}\n")
        self.fh.flush()


# --------------------------------------------------------------------- collect

def stage_collect(args) -> int:
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    log = Logger(LOGS / f"run-{run_date}.log")
    log(f"=== collect {run_date} {'(DRY RUN)' if args.dry_run else ''}")

    env = fetch.load_env(ROOT / ".env")
    api_key = env.get("FIRECRAWL_API_KEY", "")


    store = Store(STATE / "seen.sqlite")
    dedup = cfg["dedup"]
    rules = cfg["prescreen"]

    # Prescreen rules are global except the salary floor, which is per-country: the
    # Flanders under-30 rate and the Irish GEP floor bind, Germany's sec. 18b has no
    # statutory minimum at all. Merge each geography's floor into its own copy of the
    # rules so prescore.screen() stays config-driven rather than testing geo ids.
    geo_rules = {}
    for g in cfg["geographies"]:
        gr = dict(rules)
        if g.get("salary_floor_month"):
            gr["salary_floor_month"] = g["salary_floor_month"]
            gr["salary_floor_label"] = g.get("salary_floor_label") or g["label"]
        geo_rules[g["id"]] = gr

    raw_hits: list[dict] = []
    rejects: dict[str, int] = {}
    per_source: dict[str, int] = {}

    quota = Quota(store.conn, cfg.get("sources", {}), log=log)

    adz = adzuna_src.Adzuna(env.get("ADZUNA_APP_ID", ""), env.get("ADZUNA_APP_KEY", ""),
                            quota=quota, log=log)
    li = linkedin_src.LinkedIn(quota=quota, log=log,
                               delay=cfg.get("linkedin", {}).get("delay_seconds", 1.5))
    gem = gemini_src.Gemini(
        env.get("GEMINI_API_KEY", ""), quota=quota, log=log,
        model=cfg.get("sources", {}).get("gemini", {}).get("model", gemini_src.MODEL))
    if not adz.available():
        log("  ! No Adzuna credentials in .env — discovery is ATS-only this run.")
    if not gem.available():
        log("  ! No Gemini key in .env — pages without JSON-LD fall back to heuristics.")
    have_key = bool(api_key) and not api_key.startswith("fc-your-key")
    fc = (fetch.Firecrawl(api_key, cfg["firecrawl"], log=log, quota=quota)
          if have_key and quota.enabled("firecrawl") else None)
    if have_key and not quota.enabled("firecrawl"):
        log("  Firecrawl is disabled in config.yaml (sources.firecrawl.enabled) — "
            "no credits will be spent.")

    def note_reject(reason: str):
        rejects[reason] = rejects.get(reason, 0) + 1

    # ---- ATS boards first: exact, free, and they carry most of the signal.
    kws = [k.lower() for k in cfg["ats"].get("title_keywords", [])]
    wanted_geos = {g["id"] for g in cfg["geographies"]}
    geo_visa_mode = {g["id"]: g.get("visa_mode") for g in cfg["geographies"]}
    display = cfg["ats"].get("display_names", {}) or {}
    for board, entries in cfg["ats"].items():
        if board == "title_keywords" or not entries:
            continue
        fetcher = fetch.ATS_FETCHERS.get(board)
        if not fetcher:
            continue
        for slug in entries:
            try:
                records = fetcher(slug)
            except Exception as exc:  # noqa: BLE001
                log(f"  ! {board}/{slug}: {exc}")
                continue
            kept = 0
            for rec in records:
                if not rec.get("url"):
                    continue
                if not fetch.ats_title_matches(rec["title"], kws):
                    note_reject("ats_title_mismatch")
                    continue
                # These boards are global; keep only the geographies being searched.
                geo = nz.classify_geo(rec.get("location", ""))
                if geo not in wanted_geos:
                    note_reject("ats_out_of_geography")
                    continue
                raw_hits.append(
                    nz.from_ats_record(rec, display.get(slug, slug), board, geo))
                kept += 1
            per_source[f"{board}:{slug}"] = kept
            log(f"  {board}/{slug}: {len(records)} postings, {kept} in scope")

    # ---- Adzuna: the broad-market discovery layer that replaced Firecrawl search.
    if adz.available() and quota.enabled("adzuna"):
        queries = adzuna_src.build_queries(cfg)
        max_days = cfg.get("adzuna", {}).get("max_days_old", 7)
        log(f"  Adzuna: {len(queries)} calls (50 results each)")
        for i, q in enumerate(queries, 1):
            recs = adz.search(q["what"], q["country"], max_days_old=max_days)
            kept = 0
            for rec in recs:
                job = adzuna_src.to_job(rec, q["geo"])
                if job is None:
                    note_reject("adzuna_incomplete_record")
                    continue
                # Adzuna relevance is loose — a "logistics engineer" query returns
                # "Recruitment Manager" and "Software Engineer - Java". The same title
                # gate the ATS layer uses keeps ~35% and drops the rest.
                if not fetch.ats_title_matches(job["title"], kws):
                    note_reject("adzuna_title_mismatch")
                    continue
                # Adzuna's own geography can still disagree with the posting text.
                stated = nz.classify_geo(job["location"])
                if stated and stated != q["geo"]:
                    note_reject(f"adzuna_geo_mismatch:{stated}")
                    continue
                raw_hits.append(job)
                kept += 1
            per_source[f"adzuna:{q['geo']}"] = per_source.get(f"adzuna:{q['geo']}", 0) + kept
            log(f"  [{i}/{len(queries)}] {q['geo']} {q['what'][:44]!r} -> "
                f"{len(recs)} results, {kept} kept")
        log(f"  Adzuna calls used: {adz.calls_used}")

    # ---- Fill in bodies the sources left thin: bol.com stubs its description to
    # "<p>x</p>", and Adzuna truncates. Free fetch + JSON-LD first, Gemini only for
    # pages that carry no JSON-LD. This used to be a paid Firecrawl scrape.
    # ---- LinkedIn guest search: the only free source that reaches the Gulf.
    if quota.enabled("linkedin"):
        lqueries = linkedin_src.build_queries(cfg)
        lcfg = cfg.get("linkedin", {})
        if lqueries:
            log(f"  LinkedIn: {len(lqueries)} queries x {lcfg.get('pages_per_query',2)} pages")
        for i, q in enumerate(lqueries, 1):
            cards = li.search(q["keywords"], q["location"],
                              days=lcfg.get("days", 7),
                              pages=lcfg.get("pages_per_query", 2))
            kept = 0
            for card in cards:
                job = linkedin_src.to_job(card, q["geo"])
                if job is None:
                    note_reject("linkedin_unparsable_card")
                    continue
                if not fetch.ats_title_matches(job["title"], kws):
                    note_reject("linkedin_title_mismatch")
                    continue
                stated = nz.classify_geo(job["location"])
                if stated and stated != q["geo"]:
                    note_reject(f"linkedin_geo_mismatch:{stated}")
                    continue
                raw_hits.append(job)
                kept += 1
            per_source[f"linkedin:{q['geo']}"] = per_source.get(f"linkedin:{q['geo']}", 0) + kept
            log(f"  [{i}/{len(lqueries)}] {q['geo']} {q['keywords'][:34]!r} -> "
                f"{len(cards)} cards, {kept} kept")
        if li.calls_used:
            log(f"  LinkedIn calls used: {li.calls_used}")

    # Truncated sources count as thin regardless of length: Adzuna always returns
    # exactly 500 characters ending in an ellipsis, which would otherwise sail past
    # the min_body_chars gate and get scored as if it were the full posting.
    thin = [j for j in raw_hits
            if j.get("url")
            and (j.get("truncated") or len(j.get("body", "")) < rules["min_body_chars"])]
    if thin:
        log(f"  enriching {len(thin)} thin postings (free fetch -> jsonld -> gemini)")
        methods: dict[str, int] = {}
        for job in thin:
            job, method = extract.enrich(job, gemini=gem,
                                         min_chars=rules["min_body_chars"], log=log)
            methods[method] = methods.get(method, 0) + 1
        log("    " + ", ".join(f"{k}={v}" for k, v in sorted(methods.items())))

    # ---- Firecrawl discovery.
    if fc:
        weekly = args.weekly or date.fromisoformat(run_date).weekday() == 0
        queries = fetch.build_queries(cfg, weekly)
        log(f"  Firecrawl: {len(queries)} queries ({'weekly' if weekly else 'daily'} freshness)")
        for i, q in enumerate(queries, 1):
            if args.limit_queries and i > args.limit_queries:
                log(f"  (stopping at --limit-queries {args.limit_queries})")
                break
            try:
                hits = fc.search(q["query"], location=q["location"],
                                 include_domains=q["include_domains"], tbs=q["tbs"])
            except PermissionError as exc:
                log(f"  ! {exc}")
                break
            kept = 0
            for hit in hits:
                job, reason = nz.from_search_hit(hit, q["geo"], rules["min_body_chars"])
                if job is None:
                    note_reject(reason)
                    continue
                raw_hits.append(job)
                kept += 1
            per_source[f"fc:{q['geo']}"] = per_source.get(f"fc:{q['geo']}", 0) + kept
            log(f"  [{i}/{len(queries)}] {q['geo']} {q['query'][:52]!r} -> "
                f"{len(hits)} hits, {kept} postings")
        log(f"  Firecrawl credits used: {fc.credits_used}")

    log("  quota:")
    for line in quota.summary():
        log(line)

    # ---- IND recognised-sponsor register (best effort).
    # Only fetched if some geography actually declares visa_mode: ind_sponsor. After the
    # Netherlands was dropped nothing does, so this skips a ~12.9k-row HTML table parse
    # on every run. The code stays live for the day NL (or another register) comes back.
    ind_cache = STATE / "ind_sponsors.csv"
    ind_index = None
    if "ind_sponsor" in set(geo_visa_mode.values()):
        fetch.refresh_ind_register(cfg["ind_register"], ind_cache, log=log)
        ind_index = fetch.load_ind_index(ind_cache)
        if ind_index:
            log(f"  IND register: {ind_index.get('__count__')} recognised sponsors loaded")

    # ---- Dedup + prescreen.
    candidates, seen_this_run = [], set()
    dupes_in_run = already_known = screened_out = 0

    for job in raw_hits:
        if job["canonical_url"] in seen_this_run:
            dupes_in_run += 1
            continue
        seen_this_run.add(job["canonical_url"])

        existing = store.is_known(job, dedup["fuzzy_ratio"], dedup["lookback_days"])
        if existing:
            already_known += 1
            if not args.dry_run:
                store.touch(existing["fingerprint"])
            continue

        keep, reason, flags = prescore.screen(job, geo_rules.get(job["geo"], rules))
        if not keep:
            screened_out += 1
            note_reject(reason)
            continue

        if geo_visa_mode.get(job["geo"]) == "ind_sponsor":
            sponsor = fetch.is_recognised_sponsor(
                job["company"], ind_index, cfg["ind_register"]["match_ratio"])
            job["sponsor"] = sponsor
            if sponsor is True:
                flags.append("IND recognised sponsor")
            elif sponsor == "group":
                flags.append("IND sponsor at group level - verify the hiring entity")
            elif sponsor is False:
                flags.append("not on the IND sponsor register")
            else:
                flags.append("sponsor status unknown")

        job["flags"] = flags
        job["fingerprint"] = nz.fingerprint(job["company"], job["title"], job["location"])
        if not args.dry_run:
            store.insert_new(job)
        candidates.append(job)

    # A cold start sweeps a 7-day window and can yield hundreds. Cap what goes for
    # scoring so the scheduled run stays tractable.
    #
    # This takes a ROUND-ROBIN across geographies, not the configured priority order.
    # Strict priority starves the low-priority end permanently: Germany alone yields
    # ~165 candidates against a cap of 60, so a priority sort gave Germany all 60 slots
    # and Belgium, Ireland, Poland, the Gulf and India zero — every run, forever, since
    # dropped candidates are deleted from the store and re-collected identically the
    # next morning. Priority still breaks ties *within* a round, so Germany keeps first
    # pick; it just no longer takes the whole table.
    cap = cfg.get("thresholds", {}).get("max_candidates_per_run", 0)
    if cap and len(candidates) > cap:
        prio = {g["id"]: g.get("priority", 9) for g in cfg["geographies"]}
        by_geo: dict[str, list] = {}
        for j in candidates:
            by_geo.setdefault(j.get("geo") or "", []).append(j)
        # Best score first is not available yet (scoring happens after this), so keep
        # each geography's collection order — freshest-first from the sources.
        queues = [by_geo[g] for g in sorted(by_geo, key=lambda g: (prio.get(g, 9), g))]
        picked = []
        while len(picked) < cap and any(queues):
            for q in queues:
                if not q:
                    continue
                picked.append(q.pop(0))
                if len(picked) >= cap:
                    break
        kept_ids = {id(j) for j in picked}
        dropped = [j for j in candidates if id(j) not in kept_ids]
        spread = ", ".join(
            f"{g}:{sum(1 for j in picked if j.get('geo') == g)}"
            for g in sorted({j.get("geo") for j in picked}, key=lambda g: prio.get(g, 9)))
        log(f"  capping {len(candidates)} candidates to {cap} for this run "
            f"(round-robin: {spread})")
        candidates = picked
        for j in dropped:
            store.conn.execute("DELETE FROM jobs WHERE fingerprint = ?", (j["fingerprint"],))
        store.conn.commit()

    new_this_run = len(candidates)

    # Roles collected on an earlier run that never made it into an email.
    carried = []
    if not args.dry_run:
        # Re-screen carried-forward jobs against the CURRENT rules. They were stored
        # under whatever rules existed on the day they were found, so without this a
        # tightened ceiling never applies retroactively and stale roles keep costing
        # LLM calls (20 of them on 4 Sept) before being caught downstream.
        dropped_stale = 0
        for job in store.unsent_carry_forward({c["fingerprint"] for c in candidates},
                                              skip_source=indeed_src.SOURCE):
            keep, reason, flags = prescore.screen(
                job, geo_rules.get(job.get("geo"), rules))
            if not keep:
                note_reject(f"carried_restaged:{reason}")
                store.conn.execute(
                    "UPDATE jobs SET status='below_threshold' WHERE fingerprint = ?",
                    (job["fingerprint"],))
                dropped_stale += 1
                continue
            job["flags"] = flags or job.get("flags") or []
            carried.append(job)
        store.conn.commit()
        if dropped_stale:
            log(f"  re-screened carry-forwards: dropped {dropped_stale} "
                f"under the current rules")
        candidates.extend(carried)

    log("")
    log(f"  collected            {len(raw_hits)}")
    log(f"  duplicate in-run     {dupes_in_run}")
    log(f"  already in store     {already_known}")
    log(f"  screened out         {screened_out}")
    log(f"  NEW this run         {new_this_run}")
    log(f"  carried forward      {len(carried)}   (collected earlier, never emailed)")
    log(f"  TO SCORE             {len(candidates)}")
    if rejects:
        log("  rejects: " + ", ".join(f"{k}={v}" for k, v in
                                      sorted(rejects.items(), key=lambda x: -x[1])))

    if not args.dry_run:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        out = OUTBOX / f"{run_date}_candidates.json"
        out.write_text(json.dumps({
            "run_date": run_date,
            "scanned": len(raw_hits),
            "already_known": already_known,
            "screened_out": screened_out,
            "sources_line": ", ".join(f"{k} {v}" for k, v in per_source.items() if v),
            "candidates": candidates,
        }, indent=2, ensure_ascii=False))
        store.record_run(run_date, len(raw_hits), len(candidates), 0)
        log(f"  wrote {out}")

    store.close()
    return 0


# ---------------------------------------------------------------------- render

def stage_render(args) -> int:
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    log = log_for(run_date, args)

    scored_path = Path(args.scored) if args.scored else outbox_for(args) / f"{run_date}_scored.json"
    if not scored_path.exists():
        log(f"! no scored file at {scored_path}")
        return 1
    payload = json.loads(scored_path.read_text())
    scored = payload.get("jobs", payload if isinstance(payload, list) else [])

    cand_path = outbox_for(args) / f"{run_date}_candidates.json"
    meta = {"run_date": run_date, "is_test": args.test, "channel": channel_of(args)}
    if cand_path.exists():
        cand = json.loads(cand_path.read_text())
        meta["scanned"] = cand.get("scanned", 0)
        meta["sources_line"] = cand.get("sources_line", "")
    # Which model actually scored today, so a backend fallback is visible in the digest.
    meta["llm_usage"] = payload.get("llm_usage", "") if isinstance(payload, dict) else ""

    subject, html, text, rendered_fps = render.build(scored, cfg, meta)

    (outbox_for(args) / f"{run_date}_digest.html").write_text(html)
    (outbox_for(args) / f"{run_date}_digest.txt").write_text(text)
    # Accept either the old single `recipient` or the current `recipients` list.
    recipients = cfg.get("recipients") or [cfg.get("recipient")]
    recipients = [r for r in recipients if r]

    (outbox_for(args) / f"{run_date}_email.json").write_text(json.dumps({
        "to": recipients,
        "subject": subject,
        "html_path": str(outbox_for(args) / f"{run_date}_digest.html"),
        "text_path": str(outbox_for(args) / f"{run_date}_digest.txt"),
        # Exactly the jobs in the email — NOT everything above the threshold, which
        # would bury the ones max_jobs_email truncated away.
        "fingerprints": rendered_fps,
        # Scored well enough to email but cut by max_jobs_email. These must stay
        # queued, not be written off as below-threshold.
        "deferred_fingerprints": [
            j["fingerprint"] for j in scored
            if j.get("fingerprint") and j["fingerprint"] not in set(rendered_fps)
            and int(j.get("score") or 0) >= cfg["thresholds"]["watchlist"]],
        "all_fingerprints": [j["fingerprint"] for j in scored if j.get("fingerprint")],
    }, indent=2))

    # Persist scores so a below-threshold job is never re-scored on a later day.
    if not args.test:
        store = Store(STATE / "seen.sqlite")
        for j in scored:
            if j.get("fingerprint"):
                store.record_score(j["fingerprint"], int(j.get("score") or 0),
                                   j.get("rationale", ""), "; ".join(j.get("flags") or []))
        store.close()

    log(f"  subject: {subject}")
    log(f"  digest:  {outbox_for(args) / f'{run_date}_digest.html'}")
    print(f"\nSUBJECT: {subject}")
    return 0


# ---------------------------------------------------------------------- commit

def stage_commit(args) -> int:
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    log = log_for(run_date, args)

    email_path = outbox_for(args) / f"{run_date}_email.json"
    if not email_path.exists():
        log(f"! no email manifest at {email_path} — did render run?")
        return 1
    manifest = json.loads(email_path.read_text())

    if not args.message_id:
        log("! commit refused: pass --message-id from the Gmail send result.")
        log("  Nothing is marked as sent unless the email demonstrably went out.")
        return 2

    store = Store(STATE / "seen.sqlite")
    emailed = manifest.get("fingerprints", [])
    everything = manifest.get("all_fingerprints", [])
    deferred = manifest.get("deferred_fingerprints", [])

    # Three outcomes, not two. A job cut by max_jobs_email scored well — it is
    # deferred, and stays 'new' so it carries into tomorrow's digest. Only genuinely
    # low scorers are written off.
    spoken_for = set(emailed) | set(deferred)
    below = [fp for fp in everything if fp not in spoken_for]

    store.mark_sent(emailed, run_date)
    store.mark_below_threshold(below)
    store.record_run(run_key(run_date, args), 0, len(everything), len(emailed),
                     notes=f"gmail:{args.message_id}")
    stats = store.stats()
    store.close()

    log(f"  marked sent: {len(emailed)}  ·  deferred to tomorrow: {len(deferred)}"
        f"  ·  below threshold: {len(below)}")
    log(f"  store now: {stats}")
    print(f"committed {len(emailed)} sent, {len(deferred)} deferred, "
          f"{len(below)} below threshold")
    return 0


# ----------------------------------------------------------------------- score

def stage_score(args) -> int:
    """Score today's candidates with the LLM chain. This is the step Claude used to do."""
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    log = log_for(run_date, args)
    log(f"=== score {run_date}{' (DRY RUN)' if args.dry_run else ''}")

    cand_path = outbox_for(args) / f"{run_date}_candidates.json"
    if not cand_path.exists():
        log(f"! no candidates at {cand_path} — run collect first")
        return 1
    candidates = json.loads(cand_path.read_text()).get("candidates", [])
    if args.limit:
        candidates = candidates[: args.limit]
    if not candidates:
        log("  nothing to score")
        (outbox_for(args) / f"{run_date}_scored.json").write_text(
            json.dumps({"run_date": run_date, "jobs": []}, indent=2))
        return 0

    profile = (ROOT / "PROFILE.md").read_text()
    if channel_of(args) == "indeed" and INDIA_ADDENDUM.exists():
        # Same rubric; only the India location band changes for this digest.
        profile += "\n\n" + INDIA_ADDENDUM.read_text()
    store = Store(STATE / "seen.sqlite")
    quota = Quota(store.conn, cfg.get("sources", {}), log=log)
    env = fetch.load_env(ROOT / ".env")
    chain = llm_src.build(cfg, env, quota=quota, log=log)

    scored, unscored = score_src.score_jobs(
        candidates, profile, chain,
        batch_size=cfg.get("llm", {}).get("batch_size", 10), log=log,
        max_years=int(cfg["prescreen"].get("max_years_experience", 3)),
        watchlist=int(cfg["thresholds"]["watchlist"]))

    log(f"  scored {len(scored)} | unscored {len(unscored)} (carry forward)")
    log(f"  LLM usage: {chain.summary()}")
    if scored:
        top = sorted(scored, key=lambda j: -j["score"])[:5]
        for j in top:
            log(f"    {j['score']:3} {j['title'][:44]:44} | {j['company'][:22]}")
        ceiling = int(cfg["prescreen"].get("max_years_experience", 3))
        over = [j for j in scored if (j.get("years_required") or 0) > ceiling]
        if over:
            # Not necessarily a prescreen miss: the model reads years the regex cannot,
            # and either way apply_ceiling has already pushed these below the threshold.
            log(f"  {len(over)} job(s) state more than {ceiling} years — "
                f"clamped below the email threshold")

    if args.dry_run:
        log("  dry run — nothing written")
        if scored:
            print(json.dumps(scored[0], indent=2)[:900])
        store.close()
        return 0

    (outbox_for(args) / f"{run_date}_scored.json").write_text(json.dumps(
        {"run_date": run_date, "llm_usage": chain.summary(), "jobs": scored},
        indent=2, ensure_ascii=False))
    log(f"  wrote {outbox_for(args) / f'{run_date}_scored.json'}")
    store.close()
    return 0


# ------------------------------------------------------------------------ send

def stage_send(args) -> int:
    """Send the rendered digest over SMTP. Prints the Message-ID for commit."""
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    log = log_for(run_date, args)

    email_path = outbox_for(args) / f"{run_date}_email.json"
    if not email_path.exists():
        log(f"! no email manifest at {email_path} — run render first")
        return 1
    manifest = json.loads(email_path.read_text())
    html = Path(manifest["html_path"]).read_text()
    text = Path(manifest["text_path"]).read_text()

    try:
        message_id = mailer_src.send(
            fetch.load_env(ROOT / ".env"), manifest["to"], manifest["subject"],
            text, html, log=log)
    except mailer_src.MailerError as exc:
        log(f"! send failed: {exc}")
        return 2

    # Persist the real id so `daily` (and a later manual commit) can prove the send
    # happened. Without this the chain would have to invent one, which is exactly the
    # hole the --message-id contract exists to close.
    manifest["sent_message_id"] = message_id
    manifest["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    email_path.write_text(json.dumps(manifest, indent=2))

    log(f"  Message-ID: {message_id}")
    print(message_id)
    return 0


# ----------------------------------------------------------------------- daily

def lock_holder(lock: Path) -> int | None:
    """PID of a live run holding the lock, else None (clearing a stale lock).

    A lock left behind by a killed run must never block tomorrow's digest forever,
    so a PID that is not running is treated as no lock at all.
    """
    if not lock.exists():
        return None
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        lock.unlink(missing_ok=True)
        return None
    try:
        os.kill(pid, 0)          # signal 0 tests existence without touching the process
    except OSError:
        lock.unlink(missing_ok=True)   # stale: the run that wrote it is gone
        return None
    return pid


def wait_for_network(log: "Logger", host: str, timeout: int, interval: int) -> bool:
    """Block until `host` resolves, or `timeout` seconds pass.

    07:30 is almost always right as the Mac wakes from overnight sleep, before Wi-Fi
    has reconnected — DNS lookups fail for the first several minutes of the run,
    and since NL is always queried first in the source loop, it absorbs the brunt
    of it every time. Returns False (never raises) on timeout so a longer real
    outage still degrades gracefully instead of blocking the digest forever.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        try:
            socket.gethostbyname(host)
            if attempt:
                log(f"network ready ({host} resolved after {attempt} retries)")
            return True
        except OSError:
            pass
        if time.monotonic() >= deadline:
            log(f"! network still unresolvable after {timeout}s — proceeding anyway")
            return False
        attempt += 1
        log(f"waiting for network ({host} unresolvable) — retry in {interval}s")
        time.sleep(interval)


def stage_daily(args) -> int:
    """collect -> score -> render -> send -> commit. What launchd invokes.

    Any failure stops the chain before commit, so the jobs stay queued for tomorrow
    rather than being marked as sent.
    """
    run_date = args.date or date.today().isoformat()
    cfg_all = load_config()
    sched = cfg_all.get("schedule", {}) or {}

    # --- Guard 1: already delivered today. Makes the whole run idempotent, which is
    # what lets several triggers (07:30, login, hourly catch-up) share one entry point
    # without ever sending twice.
    email_path = OUTBOX / f"{run_date}_email.json"
    if email_path.exists() and not args.force:
        try:
            if json.loads(email_path.read_text()).get("sent_message_id"):
                print(f"digest for {run_date} already sent — nothing to do")
                return 0
        except (ValueError, OSError):
            pass

    # --- Guard 2: too early. A catch-up tick at 03:00 should wait, not deliver a
    # digest in the middle of the night.
    if not args.force:
        now = datetime.now()
        earliest = now.replace(hour=int(sched.get("earliest_hour", 7)),
                               minute=int(sched.get("earliest_minute", 30)),
                               second=0, microsecond=0)
        if now < earliest and args.date is None:
            print(f"before {earliest:%H:%M} — waiting")
            return 0

    # --- Guard 3: one run at a time. A ~4 minute run must not be re-entered by the
    # hourly catch-up.
    lock = STATE / "daily.lock"
    holder = lock_holder(lock)
    if holder is not None:
        print(f"a run is already in progress (pid {holder})")
        return 0
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))

    try:
        return _daily_stages(args, run_date)
    finally:
        lock.unlink(missing_ok=True)


def _daily_stages(args, run_date: str) -> int:
    log = Logger(LOGS / f"run-{run_date}.log")
    log(f"########## daily run {run_date}")

    net_cfg = (load_config().get("network", {}) or {})
    wait_for_network(
        log,
        host=net_cfg.get("check_host", "api.adzuna.com"),
        timeout=int(net_cfg.get("timeout_seconds", 150)),
        interval=int(net_cfg.get("interval_seconds", 10)),
    )

    # Check we can actually deliver BEFORE spending Adzuna, LinkedIn and LLM quota.
    # A scheduled run that collects and scores but cannot send is pure waste.
    try:
        mailer_src.credentials(fetch.load_env(ROOT / ".env"))
    except mailer_src.MailerError as exc:
        log(f"! cannot send, so not starting: {exc}")
        return 2

    class A:
        pass

    for name, stage, extra in (
        ("collect", stage_collect, {"dry_run": False, "weekly": False,
                                    "limit_queries": 0}),
        ("score", stage_score, {"dry_run": False, "limit": 0}),
        ("render", stage_render, {"scored": None, "test": False}),
    ):
        a = A()
        a.date = run_date
        for k, v in extra.items():
            setattr(a, k, v)
        rc = stage(a)
        if rc != 0:
            log(f"! {name} failed (rc={rc}) — stopping before send; nothing committed")
            return rc

    a = A()
    a.date = run_date
    rc = stage_send(a)
    if rc != 0:
        log("! send failed — nothing committed, roles carry into tomorrow")
        return rc

    manifest = json.loads((OUTBOX / f"{run_date}_email.json").read_text())
    message_id = manifest.get("sent_message_id")
    if not message_id:
        log("! send reported success but wrote no Message-ID — refusing to commit")
        return 3

    a = A()
    a.date = run_date
    a.message_id = message_id
    rc = stage_commit(a)
    log("########## daily run complete" if rc == 0 else "! commit failed")
    return rc


# ---------------------------------------------------------------------- indeed

def _indeed_records(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data.get("jobs", data) if isinstance(data, dict) else data


def _india_rules(cfg: dict) -> dict:
    rules = dict(cfg["prescreen"])
    for g in cfg["geographies"]:
        if g["id"] == indeed_src.GEO and g.get("salary_floor_month"):
            rules["salary_floor_month"] = g["salary_floor_month"]
    return rules


def stage_indeed_plan(args) -> int:
    """Which search results are worth a detail fetch. Prints a JSON list of job ids.

    Detail calls are the scarce resource (the connector locks out after ~22 fast
    calls), so only roles that are new to the store and pass the title gates get one.
    Ordered Delhi NCR -> Mumbai -> remote -> elsewhere, then as listed."""
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    args.channel = "indeed"
    log = log_for(run_date, args)
    log(f"=== indeed-plan {run_date}")
    rules = _india_rules(cfg)
    kws = [k.lower() for k in cfg["ats"].get("title_keywords", [])]
    store = Store(STATE / "seen.sqlite")
    dedup = cfg["dedup"]

    seen_keys, picked, reasons = set(), [], {}
    records = _indeed_records(args.raw)
    for rec in records:
        job = indeed_src.to_job(rec)
        why = None
        if job is None:
            why = "incomplete"
        else:
            key = nz.fingerprint(job["company"], job["title"], job["location"])
            if key in seen_keys:
                why = "duplicate_in_run"
            else:
                seen_keys.add(key)
                if not fetch.ats_title_matches(job["title"], kws):
                    why = "title_mismatch"
                elif any(t in job["title"].lower() for t in rules.get("reject_title_terms", [])):
                    why = "title_term"
                elif prescore.seniority_reject(job["title"].lower(), rules):
                    why = "seniority"
                elif store.is_known(job, dedup["fuzzy_ratio"], dedup["lookback_days"]):
                    why = "already_in_store"
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        picked.append(job)
    store.close()

    picked.sort(key=lambda j: indeed_src.city_rank(j["location"]))  # stable
    chosen = picked[: args.max]
    log(f"  {len(records)} search results -> {len(picked)} new & in scope, "
        f"{len(chosen)} to fetch")
    if reasons:
        log("  skipped: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    for j in chosen:
        log(f"    {j['indeed_job_id']:14} {j['title'][:48]:48} | {j['company'][:22]} | {j['location'][:24]}")
    print(json.dumps([j["indeed_job_id"] for j in chosen]))
    return 0


def stage_indeed_ingest(args) -> int:
    """Prescreen + store the fetched Indeed postings; write the channel's candidates."""
    cfg = load_config()
    run_date = args.date or date.today().isoformat()
    args.channel = "indeed"
    log = log_for(run_date, args)
    log(f"=== indeed-ingest {run_date}")
    rules = _india_rules(cfg)
    store = Store(STATE / "seen.sqlite")
    dedup = cfg["dedup"]

    scanned = 0
    if args.raw and Path(args.raw).exists():
        scanned = len(_indeed_records(args.raw))

    candidates, rejects, known = [], {}, 0
    for rec in _indeed_records(args.details):
        job = indeed_src.to_job(rec)
        if job is None:
            rejects["incomplete"] = rejects.get("incomplete", 0) + 1
            continue
        existing = store.is_known(job, dedup["fuzzy_ratio"], dedup["lookback_days"])
        if existing:
            known += 1
            store.touch(existing["fingerprint"])
            continue
        keep, reason, flags = prescore.screen(job, rules)
        if not keep:
            rejects[reason] = rejects.get(reason, 0) + 1
            continue
        job["flags"] = flags
        job["fingerprint"] = nz.fingerprint(job["company"], job["title"], job["location"])
        store.insert_new(job)
        candidates.append(job)

    carried = store.unsent_carry_forward({c["fingerprint"] for c in candidates},
                                         only_source=indeed_src.SOURCE)
    candidates.extend(carried)
    store.record_run(run_key(run_date, args), scanned, len(candidates), 0)
    store.close()

    ob = outbox_for(args)
    ob.mkdir(parents=True, exist_ok=True)
    (ob / f"{run_date}_candidates.json").write_text(json.dumps({
        "run_date": run_date,
        "scanned": scanned,
        "already_known": known,
        "screened_out": sum(rejects.values()),
        "sources_line": f"indeed {len(candidates) - len(carried)}",
        "candidates": candidates,
    }, indent=2, ensure_ascii=False))
    log(f"  new {len(candidates) - len(carried)} · carried {len(carried)} · "
        f"already known {known} · screened out {sum(rejects.values())}")
    if rejects:
        log("  rejects: " + ", ".join(f"{k}={v}" for k, v in sorted(rejects.items())))
    return 0


def stage_indeed_daily(args) -> int:
    """score -> render -> send -> commit for the Indeed India channel.

    A separate email from the 07:30 digest, with its own manifest, lock and
    already-sent guard. Commit still only follows a real Message-ID."""
    run_date = args.date or date.today().isoformat()
    args.channel = "indeed"
    ob = outbox_for(args)
    email_path = ob / f"{run_date}_email.json"
    if email_path.exists() and not args.force:
        try:
            if json.loads(email_path.read_text()).get("sent_message_id"):
                print(f"indeed digest for {run_date} already sent — nothing to do")
                return 0
        except (ValueError, OSError):
            pass
    if not (ob / f"{run_date}_candidates.json").exists():
        print("no indeed candidates for today — run indeed-ingest first")
        return 1

    lock = STATE / "indeed.lock"
    holder = lock_holder(lock)
    if holder is not None:
        print(f"an indeed run is already in progress (pid {holder})")
        return 0
    lock.write_text(str(os.getpid()))
    log = log_for(run_date, args)
    try:
        try:
            mailer_src.credentials(fetch.load_env(ROOT / ".env"))
        except mailer_src.MailerError as exc:
            log(f"! cannot send, so not starting: {exc}")
            return 2

        class A:
            pass

        def mk(**kw):
            a = A()
            a.date, a.channel = run_date, "indeed"
            for k, v in kw.items():
                setattr(a, k, v)
            return a

        for name, stage, a in (
            ("score", stage_score, mk(dry_run=False, limit=0)),
            ("render", stage_render, mk(scored=None, test=False)),
            ("send", stage_send, mk()),
        ):
            rc = stage(a)
            if rc != 0:
                log(f"! {name} failed (rc={rc}) — nothing committed")
                return rc
        message_id = json.loads(email_path.read_text()).get("sent_message_id")
        if not message_id:
            log("! send reported success but wrote no Message-ID — refusing to commit")
            return 3
        rc = stage_commit(mk(message_id=message_id))
        log("########## indeed run complete" if rc == 0 else "! commit failed")
        return rc
    finally:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------- status

def stage_status(args) -> int:
    store = Store(STATE / "seen.sqlite")
    print("store:", store.stats())
    rows = store.conn.execute(
        "SELECT run_date, collected, new_jobs, sent FROM runs ORDER BY run_date DESC LIMIT 10"
    ).fetchall()
    print("\nrecent runs:")
    for r in rows:
        print(f"  {r['run_date']}  collected={r['collected']:<4} "
              f"new={r['new_jobs']:<4} sent={r['sent']}")
    pending = store.pending_unsent()
    if pending:
        print(f"\n{len(pending)} scored but unsent:")
        for p in pending[:15]:
            print(f"  [{p['score'] or '--'}] {p['title']} — {p['company']}")
    store.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="stage", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--dry-run", action="store_true",
                   help="search and report, but write nothing to the store")
    c.add_argument("--weekly", action="store_true", help="widen freshness to the last week")
    c.add_argument("--limit-queries", type=int, default=0)
    c.add_argument("--date")
    c.set_defaults(func=stage_collect)

    r = sub.add_parser("render")
    r.add_argument("--scored")
    r.add_argument("--test", action="store_true",
                   help="banner the email as a test and do not persist scores")
    r.add_argument("--date")
    r.add_argument("--channel", choices=["main", "indeed"], default="main")
    r.set_defaults(func=stage_render)

    m = sub.add_parser("commit")
    m.add_argument("--message-id", required=False,
                   help="Gmail message id proving the send succeeded")
    m.add_argument("--date")
    m.add_argument("--channel", choices=["main", "indeed"], default="main")
    m.set_defaults(func=stage_commit)

    sc = sub.add_parser("score")
    sc.add_argument("--dry-run", action="store_true",
                    help="score and report, but write nothing")
    sc.add_argument("--limit", type=int, default=0, help="only score the first N candidates")
    sc.add_argument("--date")
    sc.add_argument("--channel", choices=["main", "indeed"], default="main")
    sc.set_defaults(func=stage_score)

    sd = sub.add_parser("send")
    sd.add_argument("--date")
    sd.add_argument("--channel", choices=["main", "indeed"], default="main")
    sd.set_defaults(func=stage_send)

    dy = sub.add_parser("daily", help="collect -> score -> render -> send -> commit")
    dy.add_argument("--force", action="store_true",
                    help="ignore the already-sent, too-early and lock guards")
    dy.add_argument("--date")
    dy.set_defaults(func=stage_daily)

    ip = sub.add_parser("indeed-plan", help="pick Indeed search results worth a detail fetch")
    ip.add_argument("raw", help="JSON list of search-result records from the connector")
    ip.add_argument("--max", type=int, default=indeed_src.MAX_DETAILS)
    ip.add_argument("--date")
    ip.set_defaults(func=stage_indeed_plan)

    ii = sub.add_parser("indeed-ingest", help="prescreen + store fetched Indeed postings")
    ii.add_argument("details", help="JSON list of detail records (with description)")
    ii.add_argument("--raw", help="the day's raw search file, for the scanned count")
    ii.add_argument("--date")
    ii.set_defaults(func=stage_indeed_ingest)

    idy = sub.add_parser("indeed-daily", help="score -> render -> send -> commit (Indeed India)")
    idy.add_argument("--force", action="store_true", help="ignore the already-sent guard")
    idy.add_argument("--date")
    idy.set_defaults(func=stage_indeed_daily)

    s = sub.add_parser("status")
    s.set_defaults(func=stage_status)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
