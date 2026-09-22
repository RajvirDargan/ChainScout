"""Tests for the spend ledger and the free-source adapters.

The ledger exists because one unattended run spent 324 Firecrawl credits against a
1,000/month allowance, so "a cap is never exceeded" is the property that matters most.
"""

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import adzuna  # noqa: E402
import gemini  # noqa: E402
import normalize as nz  # noqa: E402
import quota as quota_mod  # noqa: E402


def make(config):
    return quota_mod.Quota(sqlite3.connect(":memory:"), config, log=lambda *_: None)


# ------------------------------------------------------------------ the hard caps

def test_disabled_source_never_spends():
    q = make({"firecrawl": {"enabled": False, "monthly_credits": 0}})
    assert q.check_and_reserve("firecrawl", 1) is False
    assert q.used("firecrawl", quota_mod.month_key()) == 0


def test_daily_cap_is_never_exceeded():
    q = make({"adzuna": {"enabled": True, "daily_calls": 3}})
    assert [q.check_and_reserve("adzuna") for _ in range(3)] == [True, True, True]
    assert q.check_and_reserve("adzuna") is False
    assert q.used("adzuna", quota_mod.day_key()) == 3


def test_monthly_cap_is_never_exceeded():
    q = make({"adzuna": {"enabled": True, "monthly_calls": 5}})
    assert q.check_and_reserve("adzuna", 4) is True
    assert q.check_and_reserve("adzuna", 2) is False    # would reach 6
    assert q.check_and_reserve("adzuna", 1) is True     # exactly 5 is fine
    assert q.used("adzuna", quota_mod.month_key()) == 5


def test_a_reached_cap_skips_rather_than_raises():
    """A run must degrade to free sources, never die."""
    q = make({"gemini": {"enabled": True, "daily_requests": 1}})
    q.check_and_reserve("gemini")
    assert q.check_and_reserve("gemini") is False   # returns, does not raise


def test_periods_are_independent():
    q = make({"adzuna": {"enabled": True, "daily_calls": 2, "monthly_calls": 100}})
    q.check_and_reserve("adzuna", 2)
    assert q.check_and_reserve("adzuna") is False              # daily exhausted
    yesterday = quota_mod.day_key(date.today() - timedelta(days=1))
    assert q.used("adzuna", yesterday) == 0                    # yesterday untouched
    assert q.used("adzuna", quota_mod.month_key()) == 2        # month still accruing


def test_refund_returns_an_unspent_reservation():
    q = make({"adzuna": {"enabled": True, "daily_calls": 2}})
    q.check_and_reserve("adzuna")
    q.refund("adzuna")
    assert q.used("adzuna", quota_mod.day_key()) == 0


def test_reconcile_corrects_an_estimate_in_both_directions():
    q = make({"firecrawl": {"enabled": True, "monthly_credits": 100}})
    q.check_and_reserve("firecrawl", 2)
    q.reconcile("firecrawl", reserved=2, actual=10)     # cost more than estimated
    assert q.used("firecrawl", quota_mod.month_key()) == 10
    q.reconcile("firecrawl", reserved=10, actual=4)     # cost less
    assert q.used("firecrawl", quota_mod.month_key()) == 4


def test_unknown_source_is_refused():
    q = make({"adzuna": {"enabled": True}})
    assert q.check_and_reserve("something-else") is False


# ------------------------------------------------------------------ adzuna mapping

def test_adzuna_record_maps_to_the_common_job_shape():
    job = adzuna.to_job({
        "redirect_url": "https://www.adzuna.nl/details/1?utm_source=x",
        "title": "Logistics Engineer",
        "company": {"display_name": "Vanderlande Industries B.V."},
        "location": {"display_name": "Veghel, Noord-Brabant"},
        "salary_min": 42000, "salary_max": 52000,
        "created": "2026-08-30T09:00:00Z", "description": "blurb",
    }, "nl")
    assert job["company"] == "Vanderlande Industries B.V."
    assert job["canonical_url"] == "https://adzuna.nl/details/1"   # tracking stripped
    assert job["source"] == "adzuna" and job["geo"] == "nl"


def test_adzuna_predicted_salary_is_labelled_as_an_estimate():
    """A predicted salary must never read as an employer commitment — the EUR 3,122
    IND floor is judged against it."""
    job = adzuna.to_job({
        "redirect_url": "https://x/1", "title": "T",
        "company": {"display_name": "C"}, "location": {"display_name": "Amsterdam"},
        "salary_min": 36000, "salary_is_predicted": "1",
    }, "nl")
    assert "estimate" in job["salary_text"].lower()


def test_adzuna_record_without_company_is_dropped():
    """No company means no IND sponsor check and a broken dedup fingerprint."""
    assert adzuna.to_job({"redirect_url": "u", "title": "t", "company": {}}, "nl") is None


# ------------------------------------------------------------------ gemini guarding

def test_gemini_output_is_treated_as_untrusted():
    out = gemini._validate({
        "is_job_posting": True, "title": "X" * 500, "company": "C" * 500,
        "location": "L", "description": "D", "years_required": "not a number",
    })
    assert len(out["title"]) == 200 and len(out["company"]) == 120
    assert out["years_required"] == 0


def test_gemini_rejects_non_postings():
    assert gemini._validate({"is_job_posting": False, "title": "t", "description": "d"}) is None


def test_gemini_is_unavailable_without_a_key():
    assert gemini.Gemini("").available() is False
    assert gemini.Gemini("your-gemini-key").available() is False


# --------------------------------------------------- the email/commit contract

def test_commit_manifest_only_covers_jobs_actually_in_the_email():
    """max_jobs_email truncates the list. Anything cut must reappear tomorrow, so it
    must never be handed to commit as 'sent'."""
    import render
    cfg = {"thresholds": {"apply_now": 70, "worth_look": 55, "watchlist": 45,
                          "max_jobs_email": 3},
           "deadlines": {"sponsor_filing": "2026-09-15"}}
    scored = [{"fingerprint": f"fp{i}", "url": "u", "title": f"T{i}", "company": "C",
               "location": "Amsterdam", "geo": "nl", "score": 90 - i, "rationale": "r",
               "flags": [], "posted_date": ""} for i in range(10)]
    _, _, _, rendered = render.build(scored, cfg, {"run_date": "2026-08-31"})
    assert len(rendered) == 3
    assert rendered == ["fp0", "fp1", "fp2"]


def test_a_high_scoring_foreign_role_is_not_crowded_out_by_lower_nl_ones():
    """NL-first is a display rule. Applying it to selection let 58-point NL roles
    push out a 78-point India role."""
    import render
    cfg = {"thresholds": {"apply_now": 70, "worth_look": 55, "watchlist": 45,
                          "max_jobs_email": 3},
           "deadlines": {"sponsor_filing": "2026-09-15"}}
    scored = [
        {"fingerprint": "nl1", "score": 58, "geo": "nl", "title": "NL low", "company": "C",
         "location": "Amsterdam", "url": "u", "rationale": "", "flags": [], "posted_date": ""},
        {"fingerprint": "nl2", "score": 57, "geo": "nl", "title": "NL low 2", "company": "C",
         "location": "Amsterdam", "url": "u", "rationale": "", "flags": [], "posted_date": ""},
        {"fingerprint": "nl3", "score": 56, "geo": "nl", "title": "NL low 3", "company": "C",
         "location": "Amsterdam", "url": "u", "rationale": "", "flags": [], "posted_date": ""},
        {"fingerprint": "in1", "score": 78, "geo": "india", "title": "India high", "company": "C",
         "location": "Pune", "url": "u", "rationale": "", "flags": [], "posted_date": ""},
    ]
    _, _, _, rendered = render.build(scored, cfg, {"run_date": "2026-08-31"})
    assert "in1" in rendered, "the highest-scoring role must always make the email"


# ------------------------------------------------------------- linkedin adapter

LI_CARD = '''<li><div class="base-card">
<a class="base-card__full-link" href="https://ae.linkedin.com/jobs/view/logistics-officer-at-jd-com-4460000001?refId=xyz&amp;trk=guest">
<span class="sr-only">Logistics Officer</span></a>
<h3 class="base-search-card__title">
  Logistics &amp; Shipment Coordinator
</h3>
<h4 class="base-search-card__subtitle"><a href="x">JD.COM</a></h4>
<span class="job-search-card__location">Dubai, Dubai, United Arab Emirates</span>
<time class="job-search-card__listdate" datetime="2026-08-30"></time>
</div></li>'''


def test_linkedin_card_parses_into_the_common_job_shape():
    import linkedin
    job = linkedin.to_job(LI_CARD, "uae")
    assert job["title"] == "Logistics & Shipment Coordinator"   # entities decoded
    assert job["company"] == "JD.COM"
    assert job["location"] == "Dubai, Dubai, United Arab Emirates"
    assert job["posted_date"] == "2026-08-30"
    assert job["geo"] == "uae" and job["source"] == "linkedin"
    assert "refId" not in job["canonical_url"]                  # tracking stripped
    assert job["truncated"] is True                             # forces enrichment


def test_linkedin_card_without_a_job_url_is_dropped():
    import linkedin
    assert linkedin.to_job("<li><h3>Just a heading</h3></li>", "uae") is None


def test_linkedin_location_classifies_as_uae():
    assert nz.classify_geo("Dubai, Dubai, United Arab Emirates") == "uae"
    assert nz.classify_geo("Abu Dhabi Emirate, United Arab Emirates") == "uae"


def test_high_scorers_cut_by_the_email_cap_are_deferred_not_buried():
    """A job scoring 80 that simply didn't fit in today's 20 must stay queued.
    Marking it below_threshold would bury it permanently."""
    import json, tempfile, sqlite3
    from pathlib import Path
    from store import Store

    store = Store(Path(tempfile.mkdtemp()) / "seen.sqlite")
    fps = []
    for i in range(5):
        fps.append(store.insert_new({
            "canonical_url": f"u{i}", "url": f"u{i}", "company": f"Co{i}",
            "title": f"Logistics Engineer {i}", "location": "Amsterdam",
            "source": "adzuna", "geo": "nl", "body": "b"}))

    emailed, deferred, low = fps[:2], fps[2:4], fps[4:]
    spoken_for = set(emailed) | set(deferred)
    below = [f for f in fps if f not in spoken_for]

    store.mark_sent(emailed, "2026-08-31")
    store.mark_below_threshold(below)

    stats = store.stats()
    assert stats.get("sent") == 2
    assert stats.get("below_threshold") == 1
    assert stats.get("new") == 2, "deferred jobs must remain 'new'"
    assert {c["fingerprint"] for c in store.unsent_carry_forward(set())} == set(deferred)
