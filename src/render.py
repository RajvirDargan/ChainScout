"""Render the scored batch into an HTML digest plus a plain-text alternative."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

# Running order and email chips. Mirrors the `priority` values in config.yaml —
# permit-route viability, not market size. Rewired 2 Sept 2026 when NL was dropped.
GEO_ORDER = {"nl": 0, "de": 1, "be": 2, "ie": 3, "pl": 4, "uae": 5, "india": 6}
GEO_LABEL = {"nl": "NL", "de": "Germany", "be": "Belgium", "ie": "Ireland",
             "pl": "Poland", "uae": "Dubai/ME", "india": "India"}

# The digest splits into two top-level sections. NL is the preferred outcome and gets
# its own block above everything else; the rest are the widened net.
PRIORITY_GEO = "nl"

BAD_FLAG_HINTS = ("below", "cannot sponsor", "no visa sponsorship", "vloeiend",
                  "dutch is required", "dutch speaking", "nederlands sprekend",
                  "must have eu work", "eu passport", "asks ", "not on the ind",
                  "deutsch", "german language", "fluent german", "francais",
                  "français", "polski", "polish language", "fluent polish",
                  "labour market needs test", "lmnt")
# Checked before BAD, so "no visa barrier" is not caught by the sponsorship hint.
GOOD_FLAG_HINTS = ("clears the", "recognised sponsor", "english", "sponsorship offered",
                   "no visa barrier", "no visa needed", "visa sponsorship offered")


def flag_severity(text: str) -> str:
    low = text.lower()
    if any(h in low for h in GOOD_FLAG_HINTS):
        return "good"
    if any(h in low for h in BAD_FLAG_HINTS):
        return "bad"
    return "neutral"


def humanize_date(raw: str) -> str:
    if not raw:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw[:len(datetime.now().strftime(fmt))], fmt)
            days = (date.today() - dt.date()).days
            if days <= 0:
                return "posted today"
            if days == 1:
                return "posted yesterday"
            if days < 30:
                return f"posted {days}d ago"
            return dt.strftime("posted %d %b")
        except ValueError:
            continue
    return ""


def _display_key(job: dict):
    """Within a section, order by permit-route viability: Germany (sec. 18b has no
    salary floor) before Belgium, then Ireland, Poland, the Gulf and India."""
    return (GEO_ORDER.get(job.get("geo") or "", 9), -int(job.get("score") or 0))


def _selection_key(job: dict):
    """Which jobs make the email at all: best score wins, regardless of geography.

    Selecting by geography instead meant 20 Dutch slots filled up and a 78-scoring
    Ford customs role in India was cut while 58-scoring NL roles got in. Geography
    decides the running order, not who is in the room. This still holds after the
    2 Sept rewire: Germany is priority 1, but a 78-point Polish role beats a 58-point
    German one into the email.
    """
    return -int(job.get("score") or 0)


def prepare(jobs: list[dict]) -> list[dict]:
    out = []
    for j in sorted(jobs, key=_display_key):
        flags = [{"text": f, "severity": flag_severity(f)} for f in (j.get("flags") or [])]
        # Every geography is chipped now. The old rule skipped the chip for NL because
        # NL was the unstated default; with six countries in play the label always earns
        # its place.
        if j.get("geo"):
            flags.insert(0, {"text": GEO_LABEL.get(j["geo"], j["geo"]), "severity": "neutral"})
        out.append({**j, "flags": flags, "posted_human": humanize_date(j.get("posted_date", ""))})
    return out


def build(scored: list[dict], cfg: dict, meta: dict) -> tuple[str, str, str, list[str]]:
    """Returns (subject, html, plaintext, rendered_fingerprints).

    The fingerprint list is what actually appears in the email. commit() marks only
    these as sent — scoring above the threshold is not enough, because max_jobs_email
    truncates the list and anything cut must come back tomorrow, not be buried."""
    th = cfg["thresholds"]
    eligible = [j for j in scored if int(j.get("score") or 0) >= th["watchlist"]]

    # Pick on merit, then order for reading — with one exception. The Netherlands is
    # the preferred outcome, so it gets a guaranteed floor of slots rather than having
    # to out-score the rest of the world for every place. Everything above that floor
    # is still won on score alone, which is what stops a 58-point NL role displacing a
    # 78-point one (the failure this selection rule was written for in the first place).
    cap = th["max_jobs_email"]
    reserved = min(th.get("min_priority_geo_slots", 0), cap)
    by_score = sorted(eligible, key=_selection_key)
    selected = [j for j in by_score if j.get("geo") == PRIORITY_GEO][:reserved]
    picked = {id(j) for j in selected}
    selected += [j for j in by_score if id(j) not in picked][: cap - len(selected)]
    selected = sorted(selected, key=_selection_key)
    jobs = prepare(selected)

    def bands(pool):
        return (
            [j for j in pool if j["score"] >= th["apply_now"]],
            [j for j in pool if th["worth_look"] <= j["score"] < th["apply_now"]],
            [j for j in pool if th["watchlist"] <= j["score"] < th["worth_look"]],
        )

    nl_jobs = [j for j in jobs if j.get("geo") == PRIORITY_GEO]
    other_jobs = [j for j in jobs if j.get("geo") != PRIORITY_GEO]
    nl_apply, nl_worth, nl_watch = bands(nl_jobs)
    other_apply, other_worth, other_watch = bands(other_jobs)
    apply_now, worth_look, watchlist = bands(jobs)

    filing = datetime.strptime(str(cfg["deadlines"]["sponsor_filing"]), "%Y-%m-%d").date()
    days_to_filing = (filing - date.today()).days
    run_date = meta.get("run_date") or date.today().isoformat()

    ctx = {
        "run_date": run_date,
        "run_date_human": datetime.strptime(run_date, "%Y-%m-%d").strftime("%A %d %B %Y"),
        "total_new": len(apply_now) + len(worth_look) + len(watchlist),
        "apply_now": apply_now,
        "worth_look": worth_look,
        "watchlist": watchlist,
        # Two top-level sections: the Netherlands, then everywhere else.
        "nl_apply": nl_apply, "nl_worth": nl_worth, "nl_watch": nl_watch,
        "nl_total": len(nl_jobs),
        "other_apply": other_apply, "other_worth": other_worth, "other_watch": other_watch,
        "other_total": len(other_jobs),
        "days_to_filing": days_to_filing,
        "scanned": meta.get("scanned", 0),
        "sources_line": meta.get("sources_line", ""),
        "llm_usage": meta.get("llm_usage", ""),
        "watchlist_threshold": th["watchlist"],
        "is_test": meta.get("is_test", False),
        "channel": meta.get("channel") or "main",
    }

    env = Environment(
        loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("digest.html.j2").render(**ctx)

    indeed = ctx["channel"] == "indeed"
    name = "Indeed India" if indeed else "Job Agent"
    prefix = f"[{name} TEST] " if ctx["is_test"] else f"[{name}] "
    if ctx["total_new"] == 0:
        subject = f"{prefix}No new roles · {datetime.strptime(run_date, '%Y-%m-%d').strftime('%a %d %b')}"
    else:
        bits = [f"{ctx['total_new']} new"]
        if apply_now:
            bits.append(f"{len(apply_now)} apply-now")
        if not indeed:
            bits.append(f"NL {ctx['nl_total']} / elsewhere {ctx['other_total']}")
        subject = (f"{prefix}{' — '.join(bits)} · "
                   f"{datetime.strptime(run_date, '%Y-%m-%d').strftime('%a %d %b')}")

    rendered = [j["fingerprint"] for j in (apply_now + worth_look + watchlist)
                if j.get("fingerprint")]
    return subject, html, _plaintext(ctx), rendered


def _plaintext(ctx: dict) -> str:
    lines = [f"{ctx['total_new']} new roles — {ctx['run_date_human']}",
             ("Indeed · India only — scored on the same rubric as the daily digest"
              if ctx["channel"] == "indeed" else
              f"{ctx['days_to_filing']} days to the 15 Sept sponsor-filing deadline"), ""]
    if ctx["is_test"]:
        lines += ["TEST RUN — nothing marked as sent; these reappear in the first real digest.", ""]
    groups = (
        ("NETHERLANDS", ctx["nl_total"],
         (("APPLY NOW", ctx["nl_apply"]), ("WORTH A LOOK", ctx["nl_worth"]),
          ("WATCHLIST", ctx["nl_watch"]))),
        ("INDIA — VIA INDEED" if ctx["channel"] == "indeed" else "BEYOND THE NETHERLANDS",
         ctx["other_total"],
         (("APPLY NOW", ctx["other_apply"]), ("WORTH A LOOK", ctx["other_worth"]),
          ("WATCHLIST", ctx["other_watch"]))),
    )
    for group_name, group_total, bands in groups:
        if not group_total:
            continue
        lines += ["=" * 60, f"{group_name} ({group_total})", "=" * 60, ""]
        for heading, group in bands:
            if not group:
                continue
            lines.append(f"--- {heading} ({len(group)}) ---")
            for j in group:
                loc = f", {j['location']}" if j.get("location") else ""
                lines.append(f"[{j['score']}] {j['title']} — {j['company']}{loc}")
                if j.get("rationale") and heading != "WATCHLIST":
                    lines.append(f"      {j['rationale']}")
                for f in j.get("flags", []):
                    if f["severity"] == "bad":
                        lines.append(f"      ! {f['text']}")
                lines.append(f"      {j['url']}")
                lines.append("")
    if ctx["total_new"] == 0:
        lines.append(f"No new roles cleared the bar. {ctx['scanned']} postings scanned.")
    lines += ["", f"{ctx['scanned']} scanned · {ctx['sources_line']}"]
    if ctx.get("llm_usage"):
        lines.append(f"scored by {ctx['llm_usage']}")
    lines += [
              "job_agent · ~/Downloads/10_job_applications/job_agent"]
    return "\n".join(lines)
